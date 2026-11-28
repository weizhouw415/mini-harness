from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
from hashlib import sha256
from pathlib import Path
from time import time
from typing import Annotated
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, StringConstraints, model_validator

from harness.channels.feishu_progress import progress_card
from harness.channels.feishu_transport import FeishuCardUnavailable, FeishuReplyUnavailable, FeishuTransportError
from harness.storage.sqlite import Conflict, NotFound, RunStore


logger = logging.getLogger(__name__)
AppID = Annotated[str, StringConstraints(strip_whitespace=True, max_length=128, pattern=r"^(cli_[A-Za-z0-9_]+)?$")]
OpenID = Annotated[str, StringConstraints(strip_whitespace=True, max_length=128, pattern=r"^ou_[A-Za-z0-9_]+$")]
MAX_TASK_LENGTH = 16_000
REPLY_CHARS = 5_000
PROGRESS_INTERVAL = 3
APPROVAL_COMMAND = re.compile(r"/(approve|deny)\s+([a-f0-9]{12})", re.IGNORECASE)


def markdown_card(text: str, buttons: list[dict] | None = None) -> dict:
    return {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": text}, *(buttons or [])]}}


def approval_code(run_id: str, call_id: str) -> str:
    return sha256(f"{run_id}:{call_id}".encode()).hexdigest()[:12]


def approval_cards(run: dict) -> list[dict]:
    approval = run["approval"]
    arguments = json.dumps(approval["arguments"], ensure_ascii=False, indent=2)
    call_id = approval["call_id"]
    code = approval_code(run["id"], call_id)
    reason = approval.get("reason")
    reason_text = f"{reason}\n\n" if reason else ""
    text = (f"**等待审批 · {approval['tool']}**\n\n{reason_text}```json\n{arguments}\n```\n\n"
            f"- `/approve {code}` 同意本次\n"
            f"- `/deny {code}` 拒绝本次")
    buttons = [
        {"tag": "button", "type": style, "text": {"tag": "plain_text", "content": label}, "behaviors": [
            {"type": "callback", "value": {"run_id": run["id"], "call_id": call_id,
                                           "approved": approved}}
        ]}
        for approved, label, style in [(True, "同意本次", "primary"), (False, "拒绝", "danger")]
    ]
    parts = list(markdown_parts(text))
    return [markdown_card(part, buttons if index == len(parts) - 1 else None)
            for index, part in enumerate(parts)]


def markdown_parts(text: str):
    fence = ""
    opening = ""
    part = ""
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)", line) if len(line) < 128 else None
        original_line = line
        while line:
            if len(part) + len(line) <= REPLY_CHARS:
                part += line
                break
            if part and part != opening:
                yield part + ("\n" + fence if fence else "")
                part = opening
            else:
                room = REPLY_CHARS - len(part)
                yield part + line[:room] + ("\n" + fence if fence else "")
                part, line = opening, line[room:]
        if marker:
            delimiter, suffix = marker.groups()
            if not fence:
                fence, opening = delimiter, original_line.rstrip("\r\n") + "\n"
            elif delimiter.startswith(fence) and not suffix.strip():
                fence = opening = ""
    if part:
        yield part


class FeishuSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool
    app_id: AppID
    app_secret: SecretStr | None = Field(default=None, max_length=512)
    allowed_open_ids: list[OpenID] = Field(max_length=100)

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_approval_settings(cls, data):
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if key not in {"approval_mode", "approval_buttons"}}
        return data


class FeishuInbox:
    def __init__(self, store: RunStore):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS feishu_inbox (
                    run_id TEXT PRIMARY KEY,
                    app_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    task TEXT NOT NULL,
                    reply_key TEXT,
                    reply_parts INTEGER NOT NULL DEFAULT 0,
                    finished INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(app_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS feishu_inbox_sender ON feishu_inbox(app_id, sender);
                CREATE INDEX IF NOT EXISTS feishu_inbox_pending ON feishu_inbox(app_id, sender) WHERE finished = 0;
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(feishu_inbox)")}
            for name, definition in (("reaction_id", "TEXT"), ("progress_message_id", "TEXT"),
                                     ("progress_seq", "INTEGER NOT NULL DEFAULT 0"),
                                     ("progress_at", "REAL NOT NULL DEFAULT 0")):
                if name not in columns:
                    db.execute(f"ALTER TABLE feishu_inbox ADD COLUMN {name} {definition}")

    def enqueue(self, app_id: str, message_id: str, sender: str, task: str):
        with self.store.connect() as db:
            db.execute("""
                INSERT INTO feishu_inbox(run_id, app_id, message_id, sender, task) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(app_id, message_id) DO NOTHING
            """, (uuid4().hex, app_id, message_id, sender, task))

    def pending(self, app_id: str) -> list[dict]:
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("""
                SELECT * FROM feishu_inbox WHERE rowid IN (
                    SELECT min(rowid) FROM feishu_inbox WHERE app_id = ? AND finished = 0 GROUP BY sender
                )
            """, (app_id,))]

    def reaction_updates(self, app_id: str, allowed: frozenset[str]) -> list[dict]:
        placeholders = ",".join("?" for _ in allowed)
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(f"""
                SELECT run_id, sender, message_id, reaction_id, finished FROM feishu_inbox
                WHERE app_id = ? AND ((finished = 0 AND reaction_id IS NULL AND sender IN ({placeholders}))
                    OR (finished = 1 AND reaction_id <> ''))
                ORDER BY finished, rowid
                LIMIT 100
            """, (app_id, *allowed))]

    def reaction_saved(self, run_id: str, reaction_id: str):
        with self.store.connect() as db:
            db.execute("UPDATE feishu_inbox SET reaction_id = ? WHERE run_id = ?", (reaction_id, run_id))

    def progress_events(self, run_id: str, after: int) -> list[dict]:
        with self.store.connect() as db:
            rows = db.execute("""
                SELECT seq, type,
                    CASE type
                        WHEN 'tool_result' THEN json_object(
                            'call_id', json_extract(data, '$.call_id'),
                            'result', json_object('error', json_extract(data, '$.result.error'),
                                                  'exit_code', json_extract(data, '$.result.exit_code'),
                                                  'outcome_unknown', json_extract(data, '$.result.outcome_unknown')))
                        WHEN 'status' THEN json_object('status', json_extract(data, '$.status'))
                        ELSE data
                    END AS data
                FROM events
                WHERE run_id = ?1 AND type IN ('tool_call', 'tool_result', 'status')
                    AND EXISTS (SELECT 1 FROM events WHERE run_id = ?1 AND seq > ?2
                                AND type IN ('tool_call', 'tool_result', 'status'))
                ORDER BY seq
            """, (run_id, after)).fetchall()
        return [{**row, "data": json.loads(row["data"])} for row in rows]

    def progress_saved(self, run_id: str, seq: int, message_id: str):
        with self.store.connect() as db:
            db.execute("""
                UPDATE feishu_inbox SET progress_seq = ?, progress_at = ?, progress_message_id = ? WHERE run_id = ?
            """, (seq, time(), message_id, run_id))

    def owns_run(self, app_id: str, sender: str, run_id: str) -> bool:
        with self.store.connect() as db:
            return db.execute("SELECT 1 FROM feishu_inbox WHERE app_id = ? AND sender = ? AND run_id = ?",
                              (app_id, sender, run_id)).fetchone() is not None

    def waiting_approval(self, app_id: str, sender: str):
        with self.store.connect() as db:
            return db.execute("""
                SELECT runs.id, runs.approval FROM feishu_inbox JOIN runs ON runs.id = feishu_inbox.run_id
                WHERE app_id = ? AND sender = ? AND runs.status = 'awaiting_approval' AND finished = 0
            """, (app_id, sender)).fetchone()

    def session_for(self, app_id: str, sender: str) -> str | None:
        with self.store.connect() as db:
            row = db.execute("""
                SELECT runs.session_id FROM feishu_inbox JOIN runs ON runs.id = feishu_inbox.run_id
                WHERE app_id = ? AND sender = ? ORDER BY feishu_inbox.rowid DESC LIMIT 1
            """, (app_id, sender)).fetchone()
        return row["session_id"] if row else None

    def delivered(self, run_id: str, key: str, parts: int, *, finished: bool):
        with self.store.connect() as db:
            db.execute("""
                UPDATE feishu_inbox
                SET reply_key = ?, reply_parts = ?, finished = ?, task = CASE WHEN ? THEN '' ELSE task END
                WHERE run_id = ?
            """, (key, parts, finished, finished, run_id))


class FeishuService:
    def __init__(self, store: RunStore, start_run, transport_factory, decide):
        self.store = store
        self.start_run = start_run
        self.transport_factory = transport_factory
        self.decide = decide
        self.inbox = FeishuInbox(store)
        self.path = store.directory / "feishu.json"
        try:
            self.config = FeishuSettings.model_validate_json(self.path.read_bytes())
        except FileNotFoundError:
            self.config = FeishuSettings(enabled=False, app_id="", allowed_open_ids=[])
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._connection_error: str | None = None
        self._delivery_error: str | None = None

    def status(self) -> dict:
        config = self.config
        error = self._connection_error or self._delivery_error
        status = "disabled"
        if config.enabled:
            status = "error" if error else "connected" if self._connected else "connecting"
        return {"enabled": config.enabled, "app_id": config.app_id,
                "has_app_secret": bool(config.app_secret), "allowed_open_ids": config.allowed_open_ids,
                "approval_mode": "auto", "approval_buttons": True,
                "status": status, "error": error}

    async def save(self, config: FeishuSettings, *, expected: FeishuSettings | None = None):
        async with self._lock:
            if expected is not None and self.config is not expected:
                raise Conflict("飞书配置已更改，请重新生成二维码。")
            secret = config.app_secret
            if secret is None and config.app_id == self.config.app_id:
                secret = self.config.app_secret
            if secret is not None and not secret.get_secret_value().strip():
                secret = None
            config = config.model_copy(update={"app_secret": secret,
                                               "allowed_open_ids": sorted(set(config.allowed_open_ids))})
            if config.enabled and not (config.app_id and secret and config.allowed_open_ids):
                raise ValueError("启用飞书需要 App ID、App Secret 和至少一个允许的用户 Open ID。")
            data = config.model_dump(exclude={"app_secret"})
            data["app_secret"] = secret.get_secret_value().strip() if secret else None
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".feishu-", delete=False) as file:
                path = Path(file.name)
                try:
                    json.dump(data, file, ensure_ascii=False)
                    file.close()
                    path.replace(self.path)
                finally:
                    path.unlink(missing_ok=True)
            await self.stop()
            self.config = FeishuSettings.model_validate(data)
            self.start()

    def start(self):
        if self.config.enabled and self._task is None:
            self._task = asyncio.create_task(self._run(self.config), name="feishu")

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._connected = False
        self._connection_error = None
        self._delivery_error = None

    async def _run(self, config: FeishuSettings):
        allowed = frozenset(config.allowed_open_ids)
        retry_delay = 1
        reaction_wake = asyncio.Event()

        def on_action(sender, value):
            run_id, call_id, approved = (value.get(key) for key in ("run_id", "call_id", "approved"))
            if (
                self.config is not config or not isinstance(sender, str) or sender not in allowed
                or not isinstance(run_id, str) or not isinstance(call_id, str) or type(approved) is not bool
                or value.get("scope", "once") != "once"
                or not self.inbox.owns_run(config.app_id, sender, run_id)
            ):
                return {"toast": {"type": "error", "content": "你无权审批此任务。"}}
            try:
                self.decide(run_id, call_id, approved)
            except (NotFound, Conflict):
                return {"toast": {"type": "info", "content": "此审批已处理或已失效。"}}
            message = "已同意执行" if approved else "已拒绝执行"
            return {"toast": {"type": "success", "content": message},
                    "card": {"type": "raw", "data": markdown_card(message)}}

        def acknowledge(message_id: str, outcome: dict):
            reply = asyncio.create_task(self._ack_approval(transport, message_id, outcome))
            approval_replies.add(reply)
            reply.add_done_callback(approval_replies.discard)

        async def receive(message_id: str, sender: str, text: str):
            if self.config is config and sender in allowed and message_id and (task := text.strip()):
                if re.fullmatch(r"/(?:approval\s+manual|approve-session(?:\s+.*)?)", task, flags=re.IGNORECASE):
                    acknowledge(message_id, {"toast": {"content": "飞书使用自动审批，高风险操作需要逐次确认。"}})
                    return
                if command := APPROVAL_COMMAND.fullmatch(task):
                    pending = self.inbox.waiting_approval(config.app_id, sender)
                    value = {}
                    if pending:
                        call_id = json.loads(pending["approval"])["call_id"]
                        if approval_code(pending["id"], call_id) == command[2].lower():
                            value = {"run_id": pending["id"], "call_id": call_id,
                                     "approved": command[1].lower() == "approve"}
                    outcome = on_action(sender, value) if value else {
                        "toast": {"content": "此审批已处理或已失效。"}}
                    acknowledge(message_id, outcome)
                    return
                self.inbox.enqueue(config.app_id, message_id, sender, task[:MAX_TASK_LENGTH + 1])
                reaction_wake.set()

        def set_connected(connected: bool):
            nonlocal retry_delay
            if self.config is config:
                self._connected = connected
                if connected:
                    retry_delay = 1
                    self._connection_error = None

        while True:
            tasks = []
            approval_replies = set()
            try:
                assert config.app_secret is not None
                transport = self.transport_factory(config.app_id, config.app_secret.get_secret_value())
                tasks = [asyncio.create_task(transport.run(receive, set_connected, on_action=on_action)),
                         asyncio.create_task(self._deliver(config.app_id, allowed, transport)),
                         asyncio.create_task(self._reactions(config.app_id, allowed, transport, reaction_wake))]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                self._connection_error = "飞书长连接已断开，正在重连。"
            except Exception as error:
                logger.warning("Feishu connection failed (%s)", type(error).__name__)
                self._connection_error = (str(error) if isinstance(error, FeishuTransportError)
                                          else "飞书连接失败。请检查应用凭证、长连接设置及网络。")
            finally:
                self._connected = False
                tasks.extend(approval_replies)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)

    async def _ack_approval(self, transport, message_id: str, outcome: dict):
        try:
            await transport.reply(message_id, outcome["toast"]["content"],
                                  uuid5(NAMESPACE_URL, f"mini-harness/feishu/{message_id}/approval").hex)
        except FeishuTransportError:
            logger.warning("Could not send Feishu approval acknowledgement")

    async def _reactions(self, app_id: str, allowed: frozenset[str], transport, wake: asyncio.Event):
        while True:
            wake.clear()
            if self._connected:
                for item in self.inbox.reaction_updates(app_id, allowed):
                    if item["finished"]:
                        try:
                            await transport.remove_typing(item["message_id"], item["reaction_id"])
                        except FeishuTransportError:
                            continue
                        self.inbox.reaction_saved(item["run_id"], "")
                    else:
                        try:
                            reaction_id = await transport.add_typing(item["message_id"])
                        except FeishuTransportError as error:
                            logger.warning("Could not add Feishu typing reaction (%s)", error.code)
                            reaction_id = ""
                        self.inbox.reaction_saved(item["run_id"], reaction_id)
            try:
                await asyncio.wait_for(wake.wait(), timeout=1)
            except TimeoutError:
                pass

    async def _deliver(self, app_id: str, allowed: frozenset[str], transport):
        while True:
            if self._connected:
                pending = [item for item in self.inbox.pending(app_id) if item["sender"] in allowed]
                results = await asyncio.gather(*(self._process(item, transport) for item in pending),
                                               return_exceptions=True)
                error = next((result for result in results if isinstance(result, Exception)), None)
                if error:
                    logger.warning("Feishu message processing failed (%s)", type(error).__name__)
                    self._delivery_error = (str(error) if isinstance(error, FeishuTransportError)
                                            else "飞书任务处理失败，正在重试。")
                else:
                    self._delivery_error = None
            await asyncio.sleep(5 if self._delivery_error else 1)

    async def _process(self, item: dict, transport):
        run_id = item["run_id"]
        if len(item["task"]) > MAX_TASK_LENGTH:
            if item["reaction_id"] is None:
                return
            await self._reply(item, transport, "too-long", "消息超过 16,000 个字符，请拆分后重发。", finished=True)
            return
        try:
            run = self.store.get(run_id)
        except NotFound:
            try:
                # The inbox assigns the Run ID before submission so a restart can recover
                # an accepted Run without starting another for the same Feishu message.
                session_id = self.inbox.session_for(item["app_id"], item["sender"])
                self.start_run(item["task"], session_id, run_id)
            except Conflict:
                # A prior run in this session may still be active.
                pass
            return

        status = run["status"]
        terminal = status in {"completed", "failed", "cancelled"}
        if terminal or status in {"running", "awaiting_approval", "paused"}:
            await self._progress(item, transport, status, flush=terminal)
        if status == "awaiting_approval":
            await self._reply(item, transport, f"approval-once:{run['approval']['call_id']}",
                              "任务正在等待审批。", cards=approval_cards(run))
        elif status == "paused":
            await self._reply(item, transport, f"paused:{run['updated_at']}",
                              "任务已暂停。请在 Mini Harness 网页打开对应会话，恢复或取消任务。")
        elif terminal:
            if item["reaction_id"] is None:
                return
            text = {"completed": run["output"] or "任务已完成。", "failed": "任务失败，详情可在 Mini Harness 网页查看。",
                    "cancelled": "任务已取消。"}[status]
            await self._reply(item, transport, status, text, finished=True, markdown=status == "completed")

    async def _progress(self, item: dict, transport, status: str, *, flush: bool):
        message_id = item["progress_message_id"]
        if message_id == "" or (not flush and time() - item["progress_at"] < PROGRESS_INTERVAL):
            return
        run_id = item["run_id"]
        events = self.inbox.progress_events(run_id, item["progress_seq"])
        if not events:
            return
        # A status transition may have landed after _process read the run.
        status = next((event["data"]["status"] for event in reversed(events) if event["type"] == "status"), status)
        card = progress_card(events, status)
        if card is None:
            return
        seq = events[-1]["seq"]
        if message_id is None:
            # A lost reply response leaves the card ID unknown. Reserve the single
            # send before calling Feishu so a restart cannot create another card.
            self.inbox.progress_saved(run_id, seq, "")
            try:
                message_id = await transport.reply(
                    item["message_id"], "", uuid5(NAMESPACE_URL, f"mini-harness/feishu/{run_id}/progress-card").hex,
                    card=card,
                )
            except FeishuTransportError as error:
                logger.warning("Could not send Feishu tool progress (%s)", error.code)
                return
            if message_id:
                self.inbox.progress_saved(run_id, seq, message_id)
            return

        try:
            await transport.update_card(message_id, card)
        except FeishuCardUnavailable:
            message_id = ""
        except FeishuTransportError as error:
            logger.warning("Could not update Feishu tool progress (%s)", error.code)
            if not flush:
                seq = item["progress_seq"]
        self.inbox.progress_saved(run_id, seq, message_id)

    async def _reply(self, item: dict, transport, key: str, text: str, *, finished: bool = False,
                     markdown: bool = False, cards: list[dict] | None = None):
        run_id = item["run_id"]
        parts = list(markdown_parts(text)) if markdown else [
            text[index:index + REPLY_CHARS] for index in range(0, len(text), REPLY_CHARS)
        ]
        if cards is not None:
            parts = [card["body"]["elements"][0]["content"] for card in cards]
        sent = item["reply_parts"] if item["reply_key"] == key else 0
        for index in range(sent, len(parts)):
            reply_id = uuid5(NAMESPACE_URL, f"mini-harness/feishu/{run_id}/{key}/{index}").hex
            try:
                content = cards[index] if cards is not None else (markdown_card(parts[index]) if markdown else None)
                await transport.reply(item["message_id"], parts[index], reply_id, card=content)
            except FeishuReplyUnavailable:
                # A recalled or deleted source message cannot receive more replies.
                self.inbox.delivered(run_id, key, len(parts), finished=finished)
                return
            self.inbox.delivered(run_id, key, index + 1, finished=finished and index + 1 == len(parts))
