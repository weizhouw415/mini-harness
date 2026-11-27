import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from harness.api.server import create_app
from harness.channels.feishu import approval_code
from harness.channels.feishu_transport import FeishuReplyUnavailable
from harness.config import Settings
from tests.harness_support import eventually, tool_call
from tests.test_approvals import API_TOKEN, ApprovalWorker


SETTINGS_URL = "/v1/settings/feishu"
APP_SECRET = "test-app-secret"
ALLOWED = ["ou_alice", "ou_bob"]
CONFIG = {"enabled": True, "app_id": "cli_test1", "app_secret": APP_SECRET, "allowed_open_ids": ALLOWED}


class FakeFeishuTransport:
    def __init__(self, app_id, app_secret):
        self.credentials = app_id, app_secret
        self.replies = []
        self.recalled = set()
        self.stopped = False

    async def run(self, receive, set_connected, *, on_action):
        self.receive = receive
        self.on_action = on_action
        self.typing = set()
        self.cards = []
        set_connected(True)
        try:
            await asyncio.Future()
        finally:
            self.stopped = True

    async def reply(self, message_id, text, uuid, *, card=None):
        if message_id in self.recalled:
            raise FeishuReplyUnavailable("原消息已撤回")
        self.replies.append((message_id, text, uuid))
        if card:
            self.cards.append((message_id, card["body"]["elements"]))

    async def add_typing(self, message_id):
        self.typing.add(message_id)
        return message_id

    async def remove_typing(self, message_id, reaction_id):
        self.typing.discard(message_id)


class BrowserWorker(ApprovalWorker):
    async def acquire_target(self, worker_target, run_id, workspace_id, *, on_reserved=None):
        return await self.acquire(worker_target, run_id, workspace_id, on_reserved=on_reserved)


class FeishuModel:
    async def complete(self, messages, tools):
        last = messages[-1]
        if last["role"] == "tool":
            return {"role": "assistant", "content": "首条回复"}
        if last["content"] == "第一条消息":
            return tool_call("feishu-tool", "bash", {"command": "printf hello"})
        return {"role": "assistant", "content": f"收到 {last['content']}"}


class FeishuTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.settings = Settings(data_dir=Path(directory), api_token=API_TOKEN)
        self.worker = BrowserWorker()
        self.transports = []

    def app(self):
        def transport_factory(app_id, app_secret):
            transport = FakeFeishuTransport(app_id, app_secret)
            self.transports.append(transport)
            return transport

        return create_app(self.settings, llm=FeishuModel(), worker_manager=self.worker,
                          feishu_transport_factory=transport_factory)

    @staticmethod
    def client(app, *, authorized=True):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
            headers={"Authorization": f"Bearer {API_TOKEN}"} if authorized else {},
        )

    async def connected(self, client):
        async def read_status():
            config = (await client.get(SETTINGS_URL)).raise_for_status().json()
            return config if config["status"] == "connected" else None

        return await eventually(read_status, timeout=8)

    async def test_registration_saves_scanner_as_only_allowed_user(self):
        try:
            import lark_oapi
            import qrcode  # noqa: F401
        except ImportError:
            self.skipTest("Feishu registration dependencies are optional")
        confirmed = asyncio.Event()
        verification_url = "https://open.feishu.cn/page/launcher?test=1"

        async def register(**options):
            options["on_qr_code"]({"url": verification_url, "expire_in": 3600})
            await confirmed.wait()
            return {"client_id": "cli_scanned", "client_secret": APP_SECRET, "user_info": {"open_id": "ou_scanner"}}

        app = self.app()
        path = SETTINGS_URL + "/registration"
        async with app.router.lifespan_context(app), self.client(app) as client:
            with patch.object(lark_oapi, "aregister_app", register):
                registration = (await client.post(path)).raise_for_status().json()
                poll_url = path + "/" + registration["id"]

                async def read_code():
                    state = (await client.get(poll_url)).raise_for_status().json()
                    return state if state["status"] == "waiting" else None

                waiting = await eventually(read_code, timeout=5)
                self.assertEqual(waiting["url"], verification_url)
                self.assertTrue(waiting["qr_png"].startswith("iVBOR"))
                self.assertEqual((await client.post(path)).status_code, 409)
                confirmed.set()
                connected = await self.connected(client)
                self.assertEqual(connected["app_id"], "cli_scanned")
                self.assertEqual(connected["allowed_open_ids"], ["ou_scanner"])
                response = (await client.get(poll_url)).raise_for_status()
                self.assertEqual(response.json()["status"], "completed")
                self.assertNotIn(APP_SECRET, response.text)

    async def test_settings_are_authenticated_write_only_and_persisted(self):
        app = self.app()
        async with app.router.lifespan_context(app), self.client(app, authorized=False) as anonymous:
            self.assertEqual((await anonymous.get(SETTINGS_URL)).status_code, 401)
            self.assertEqual((await anonymous.put(SETTINGS_URL, json=CONFIG)).status_code, 401)
            async with self.client(app) as client:
                initial = (await client.get(SETTINGS_URL)).raise_for_status().json()
                self.assertFalse(initial["enabled"])
                self.assertFalse(initial["has_app_secret"])
                self.assertEqual(initial["status"], "disabled")
                invalid = await client.put(SETTINGS_URL, json={**CONFIG, "allowed_open_ids": []})
                self.assertEqual(invalid.status_code, 422)
                self.assertNotIn(APP_SECRET, invalid.text)
                malformed = await client.put(SETTINGS_URL, json={**CONFIG, "app_secret": {"value": APP_SECRET}})
                self.assertEqual(malformed.status_code, 422)
                self.assertNotIn(APP_SECRET, malformed.text)

                saved = (await client.put(SETTINGS_URL, json=CONFIG)).raise_for_status()
                self.assertEqual(saved.headers["cache-control"], "no-store")
                self.assertNotIn(APP_SECRET, saved.text)
                self.assertTrue(saved.json()["has_app_secret"])
                connected = await self.connected(client)
                self.assertEqual(connected["allowed_open_ids"], ALLOWED)
                self.assertNotIn("app_secret", connected)
                self.assertEqual(self.transports[-1].credentials, (CONFIG["app_id"], APP_SECRET))

                changed = await client.put(SETTINGS_URL, json={**CONFIG, "app_id": "cli_other", "app_secret": None})
                self.assertEqual(changed.status_code, 422)
                saved = await client.put(SETTINGS_URL, json={**CONFIG, "app_secret": None, "enabled": False})
                self.assertEqual(saved.raise_for_status().json()["status"], "disabled")
                self.assertTrue(self.transports[-1].stopped)

        app = self.app()
        async with app.router.lifespan_context(app), self.client(app) as client:
            config = (await client.get(SETTINGS_URL)).raise_for_status().json()
            self.assertEqual(config["status"], "disabled")
            self.assertEqual(config["app_id"], CONFIG["app_id"])
            self.assertTrue(config["has_app_secret"])
            (await client.put(SETTINGS_URL, json={**CONFIG, "app_secret": None})).raise_for_status()
            await self.connected(client)
            self.assertEqual(self.transports[-1].credentials, (CONFIG["app_id"], APP_SECRET))

    async def test_inbound_text_reuses_sessions_and_approves_from_feishu(self):
        app = self.app()
        async with app.router.lifespan_context(app), self.client(app) as client:
            (await client.put(SETTINGS_URL, json={**CONFIG, "approval_buttons": True})).raise_for_status()
            await self.connected(client)
            transport = self.transports[-1]
            await transport.receive("om_unlisted", "ou_unlisted", "未授权的消息")
            await transport.receive("om_first", ALLOWED[0], "第一条消息")
            await transport.receive("om_first", ALLOWED[0], "重复投递")
            await transport.receive("om_second", ALLOWED[0], "第二条消息")
            await transport.receive("om_other", ALLOWED[1], "另一位用户")

            async def runs():
                return (await client.get("/v1/runs")).raise_for_status().json()

            async def awaiting_approval():
                return next((run for run in await runs() if run["status"] == "awaiting_approval"), None)

            first = await eventually(awaiting_approval, timeout=8)
            self.assertEqual(first["task"], "第一条消息")
            first_url = f"/v1/runs/{first['id']}"
            run = (await client.get(first_url)).raise_for_status().json()
            self.assertEqual(run["worker_target"], "browser")
            self.assertEqual(run["approval_mode"], "manual")
            self.assertEqual(self.worker.calls, [])
            self.assertNotIn("第二条消息", [run["task"] for run in await runs()])
            await eventually(lambda: any(mid == "om_first" and "审批" in text for mid, text, _ in transport.replies),
                             timeout=8)

            await eventually(lambda: "om_first" in transport.typing, timeout=8)
            approval = next(elements for mid, elements in transport.cards if mid == "om_first")
            value = approval[1]["behaviors"][0]["value"]
            self.assertEqual(transport.on_action(ALLOWED[1], value)["toast"]["type"], "error")
            self.assertEqual(transport.on_action(ALLOWED[0], value)["toast"]["type"], "success")
            self.assertEqual(transport.on_action(ALLOWED[0], value)["toast"]["type"], "info")
            expected = {("om_first", "首条回复"), ("om_second", "收到 第二条消息"), ("om_other", "收到 另一位用户")}
            await eventually(lambda: expected <= {(mid, text) for mid, text, _ in transport.replies}, timeout=8)
            await eventually(lambda: not transport.typing, timeout=8)
            first_cards = [elements[0] for mid, elements in transport.cards if mid == "om_first"]
            self.assertIn("**任务进度**", first_cards[1]["content"])
            self.assertEqual(first_cards[-1], {"tag": "markdown", "content": "首条回复"})
            records = await runs()
            self.assertCountEqual([run["task"] for run in records], ["第一条消息", "第二条消息", "另一位用户"])
            sessions = {run["task"]: run["session_id"] for run in records}
            self.assertEqual(sessions["第一条消息"], sessions["第二条消息"])
            self.assertNotEqual(sessions["第一条消息"], sessions["另一位用户"])
            self.assertEqual(len(self.worker.calls), 1)
            self.assertEqual(len({uid for _, _, uid in transport.replies}), len(transport.replies))

        app = self.app()
        async with app.router.lifespan_context(app), self.client(app) as client:
            await self.connected(client)
            transport = self.transports[-1]
            transport.recalled.add("om_recalled")
            await transport.receive("om_second", ALLOWED[0], "重启后的重复投递")
            await transport.receive("om_recalled", ALLOWED[0], "已撤回的消息")
            await transport.receive("om_third", ALLOWED[0], "第三条消息")
            await eventually(lambda: any(mid == "om_third" for mid, _, _ in transport.replies), timeout=8)
            records = (await client.get(f"/v1/sessions/{sessions['第一条消息']}/runs")).raise_for_status().json()
            self.assertEqual([run["task"] for run in records], ["第一条消息", "第二条消息", "已撤回的消息", "第三条消息"])
            self.assertEqual([mid for mid, _, _ in transport.replies], ["om_third"])

    async def test_feishu_approval_commands_and_channel_default(self):
        app = self.app()
        async with app.router.lifespan_context(app), self.client(app) as client:
            (await client.put(SETTINGS_URL, json=CONFIG)).raise_for_status()
            await self.connected(client)
            transport = self.transports[-1]
            await transport.receive("om_allow_session", ALLOWED[0], "第一条消息")

            async def pending_card():
                return next((elements for mid, elements in transport.cards if mid == "om_allow_session"), None)

            elements = await eventually(pending_card, timeout=8)
            self.assertEqual(len(elements), 1)
            runs = (await client.get("/v1/runs")).raise_for_status().json()
            run_id = runs[0]["id"]
            run = (await client.get(f"/v1/runs/{run_id}")).raise_for_status().json()
            code = approval_code(run_id, run["approval"]["call_id"])
            self.assertIn(f"/approve-session {code}", elements[0]["content"])
            await transport.receive("om_grant", ALLOWED[0], f"/approve-session {code}")
            await transport.receive("om_next", ALLOWED[0], "第一条消息")
            await eventually(lambda: any(mid == "om_next" and text == "首条回复"
                                         for mid, text, _ in transport.replies), timeout=8)
            records = (await client.get(f"/v1/sessions/{run['session_id']}/runs")).raise_for_status().json()
            self.assertEqual([item["approval_mode"] for item in records], ["allow", "allow"])

            await transport.receive("om_revoke", ALLOWED[0], "/approval manual")
            await transport.receive("om_again", ALLOWED[0], "第一条消息")
            await eventually(lambda: any(mid == "om_again" and "等待审批" in text
                                         for mid, text, _ in transport.replies), timeout=8)
            (await client.put(SETTINGS_URL, json={**CONFIG, "approval_mode": "allow"})).raise_for_status()
            await self.connected(client)
            transport = self.transports[-1]
            await transport.receive("om_channel", ALLOWED[1], "第一条消息")
            await eventually(lambda: any(mid == "om_channel" and text == "首条回复"
                                         for mid, text, _ in transport.replies), timeout=8)
            records = (await client.get("/v1/runs")).raise_for_status().json()
            self.assertEqual(len(records), 4)
