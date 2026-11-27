from __future__ import annotations

import asyncio
import json
import logging
import re
from base64 import b64encode
from collections.abc import Awaitable, Callable
from time import monotonic
from urllib.parse import parse_qs, urlsplit

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException


log = logging.getLogger(__name__)
socket_log = log.getChild("websocket")
socket_log.setLevel(logging.CRITICAL)  # Debug frames and the signed upgrade URL are private.
FEISHU_URL = "https://open.feishu.cn"
MAX_EVENT_BYTES = 1024 * 1024
MESSAGE_ID = re.compile(r"om_[a-zA-Z0-9_-]{1,128}")
OPEN_ID = re.compile(r"ou_[a-zA-Z0-9_-]{1,128}")


class FeishuTransportError(RuntimeError):
    """An error with a display-safe message for the settings UI."""

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


class FeishuReplyUnavailable(FeishuTransportError):
    """Feishu rejected the source message as a reply target."""


def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def _direct_text(payload: bytes, app_id: str) -> tuple[str, str, str] | None:
    data = _object(json.loads(payload))
    header, event = _object(data.get("header")), _object(data.get("event"))
    if header.get("event_type") != "im.message.receive_v1" or header.get("app_id") != app_id:
        return None
    sender, message = _object(event.get("sender")), _object(event.get("message"))
    if (sender.get("sender_type") != "user" or message.get("chat_type") != "p2p"
            or message.get("message_type") != "text"):
        return None
    message_id, open_id = message.get("message_id"), _object(sender.get("sender_id")).get("open_id")
    if (not isinstance(message_id, str) or not MESSAGE_ID.fullmatch(message_id)
            or not isinstance(open_id, str) or not OPEN_ID.fullmatch(open_id)):
        return None
    raw = message.get("content")
    text = _object(json.loads(raw)).get("text") if isinstance(raw, str) else None
    if isinstance(text, str) and text.strip():
        return message_id, open_id, text
    return None


def _ping_interval(config: object) -> int:
    interval = _object(config).get("PingInterval")
    return interval if type(interval) is int and 1 <= interval <= 600 else 120


class FeishuTransport:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self._app_secret = app_secret
        self._tenant_token = ""
        self._token_expiry = 0.0
        self._token_lock = asyncio.Lock()

    async def _post(self, http: httpx.AsyncClient, path: str, operation: str, *, method="POST", **kwargs) -> dict:
        try:
            response = await http.request(method, path, **kwargs)
            payload = _object(response.json())
        except (httpx.HTTPError, ValueError):
            raise FeishuTransportError(f"{operation}失败，请检查飞书网络连接。") from None
        code = payload.get("code")
        if not response.is_success or type(code) is not int or code != 0:
            code = code if type(code) is int and code != 0 else response.status_code
            raise FeishuTransportError(f"{operation}失败（飞书错误码 {code}）。", code=code)
        return payload

    async def run(self, receive: Callable[[str, str, str], Awaitable[None]],
                  set_connected: Callable[[bool], None], *, on_action=None) -> None:
        # Reuse the SDK's wire format. Its client owns a blocking event loop and
        # the high-level async channel acknowledges messages before dispatch.
        try:
            from lark_channel.ws.pb.pbbp2_pb2 import Frame
            from lark_channel.ws.pb.google.protobuf.message import DecodeError
        except ImportError:
            raise FeishuTransportError("缺少飞书 SDK，请安装 mini-harness 的 feishu 扩展。") from None

        async with httpx.AsyncClient(base_url=FEISHU_URL, timeout=10) as http:
            payload = await self._post(http, "/callback/ws/endpoint", "获取飞书长连接地址",
                                       headers={"locale": "zh"},
                                       json={"AppID": self.app_id, "AppSecret": self._app_secret})
        endpoint = _object(payload.get("data"))
        url = endpoint.get("URL")
        try:
            parsed = urlsplit(url if isinstance(url, str) else "")
            service_id = int(parse_qs(parsed.query)["service_id"][0])
            host = parsed.hostname or ""
            if (parsed.scheme != "wss" or (host != "feishu.cn" and not host.endswith(".feishu.cn"))
                    or parsed.username is not None or parsed.password is not None):
                raise ValueError
        except (ValueError, KeyError):
            raise FeishuTransportError("飞书返回了无效的长连接地址。") from None

        interval = _ping_interval(endpoint.get("ClientConfig"))
        fragments: dict[str, tuple[float, list[bytes | None]]] = {}
        try:
            async with connect(url, open_timeout=10, close_timeout=3, max_size=MAX_EVENT_BYTES,
                               logger=socket_log) as ws:
                set_connected(True)
                next_ping = 0.0
                while True:
                    now = monotonic()
                    if now >= next_ping:
                        ping = Frame(SeqID=0, LogID=0, service=service_id, method=0)
                        ping.headers.add(key="type", value="ping")
                        await ws.send(ping.SerializeToString())
                        next_ping = now + interval
                    try:
                        async with asyncio.timeout(max(0, next_ping - monotonic())):
                            raw = await ws.recv()
                    except TimeoutError:
                        continue
                    if not isinstance(raw, bytes):
                        continue
                    frame = Frame()
                    frame.ParseFromString(raw)
                    headers = {item.key: item.value for item in frame.headers}
                    if frame.method == 0 and headers.get("type") == "pong" and frame.payload:
                        interval = _ping_interval(json.loads(frame.payload))
                        next_ping = min(next_ping, monotonic() + interval)
                    elif frame.method == 1 and headers.get("type") in {"event", "card"}:
                        await self._handle_event(ws, frame, headers, fragments, receive, on_action)
        except (OSError, ValueError, TimeoutError, WebSocketException, DecodeError):
            raise FeishuTransportError("飞书长连接已中断，请检查网络、应用凭证和事件订阅。") from None
        finally:
            set_connected(False)

    async def _handle_event(self, ws, frame, headers, fragments, receive, on_action) -> None:
        started = monotonic()
        response = {"code": 200}
        try:
            payload = self._event_payload(frame.payload, headers, fragments, started)
            if payload is None:
                return
            message = _direct_text(payload, self.app_id)
            if message is not None:
                await receive(*message)
            elif on_action:
                data = _object(json.loads(payload))
                header, event = _object(data.get("header")), _object(data.get("event"))
                if header.get("app_id") == self.app_id and header.get("event_type") == "card.action.trigger":
                    sender = _object(event.get("operator")).get("open_id")
                    value = _object(_object(event.get("action")).get("value"))
                    result = on_action(sender, value)
                    response["data"] = b64encode(json.dumps(result, ensure_ascii=False).encode()).decode()
        except Exception:
            log.warning("Could not accept Feishu event")
            response = {"code": 500}
        frame.headers.add(key="biz_rt", value=str(int((monotonic() - started) * 1000)))
        frame.payload = json.dumps(response).encode()
        await ws.send(frame.SerializeToString())

    @staticmethod
    def _event_payload(payload: bytes, headers, fragments, now: float) -> bytes | None:
        total = int(headers.get("sum", "1"))
        if total == 1:
            return payload
        index, frame_id = int(headers["seq"]), headers["message_id"]
        if not 1 < total <= 64 or not 0 <= index < total:
            raise ValueError("Invalid Feishu frame fragments")
        for old_id, (expires, _) in tuple(fragments.items()):
            if expires <= now:
                del fragments[old_id]
        if len(fragments) >= 16 and frame_id not in fragments:
            raise ValueError("Too many incomplete Feishu frames")
        _, parts = fragments.setdefault(frame_id, (now + 5, [None] * total))
        if len(parts) != total:
            raise ValueError("Mismatched Feishu frame fragments")
        parts[index] = payload
        if sum(len(part) for part in parts if part is not None) > MAX_EVENT_BYTES:
            del fragments[frame_id]
            raise ValueError("Feishu event exceeds size limit")
        if None in parts:
            return None
        del fragments[frame_id]
        return b"".join(part for part in parts if part is not None)

    async def _message_request(self, message_id: str, suffix: str, operation: str, **kwargs) -> dict:
        if not MESSAGE_ID.fullmatch(message_id):
            raise FeishuTransportError("无效的飞书消息 ID。")
        async with httpx.AsyncClient(base_url=FEISHU_URL, timeout=10) as http:
            async with self._token_lock:
                if monotonic() >= self._token_expiry:
                    result = await self._post(http, "/open-apis/auth/v3/tenant_access_token/internal",
                                              "获取飞书应用访问令牌",
                                              json={"app_id": self.app_id, "app_secret": self._app_secret})
                    token, expire = result.get("tenant_access_token"), result.get("expire")
                    if not isinstance(token, str) or not token or type(expire) is not int or expire <= 0:
                        raise FeishuTransportError("飞书返回了无效的应用访问令牌。")
                    self._tenant_token = token
                    self._token_expiry = monotonic() + max(0, expire - 60)
                token = self._tenant_token
            return await self._post(http, f"/open-apis/im/v1/messages/{message_id}/{suffix}", operation,
                                    headers={"Authorization": f"Bearer {token}"}, **kwargs)

    async def reply(self, message_id: str, text: str, uuid: str, *, card: dict | None = None) -> None:
        payload = {"msg_type": "interactive" if card is not None else "text", "uuid": uuid,
                   "content": json.dumps(card if card is not None else {"text": text}, ensure_ascii=False)}
        try:
            await self._message_request(message_id, "reply", "回复飞书消息", json=payload)
        except FeishuTransportError as error:
            if error.code in {230011, 230054, 230111}:
                raise FeishuReplyUnavailable(str(error), code=error.code) from None
            if card is None or error.code != 230099 or not text:
                raise
            await self.reply(message_id, text, uuid + "-text")

    async def add_typing(self, message_id: str) -> str:
        result = await self._message_request(message_id, "reactions", "添加飞书消息表情",
                                             json={"reaction_type": {"emoji_type": "Typing"}})
        reaction = _object(result.get("data")).get("reaction_id")
        if not isinstance(reaction, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", reaction):
            raise FeishuTransportError("飞书返回了无效的表情 ID。")
        return reaction

    async def remove_typing(self, message_id: str, reaction_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", reaction_id):
            raise FeishuTransportError("无效的飞书表情 ID。")
        try:
            await self._message_request(message_id, f"reactions/{reaction_id}", "移除飞书消息表情", method="DELETE")
        except FeishuTransportError as error:
            if error.code not in {230110, 231003, 231004, 231005, 231011, 404}:
                raise
