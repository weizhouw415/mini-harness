import asyncio
import json
import unittest
from base64 import b64decode
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx

from harness.channels import feishu_transport
from harness.channels.feishu_transport import FeishuReplyUnavailable, FeishuTransport, FeishuTransportError


class FeishuTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_event_is_acknowledged_after_the_inbox_accepts_it(self):
        try:
            from lark_channel.ws.pb.pbbp2_pb2 import Frame
        except ImportError:
            self.skipTest("Feishu SDK is an optional dependency")

        payload = {
            "header": {"app_id": "cli_test", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_test"}},
                "message": {"chat_type": "p2p", "message_type": "text", "message_id": "om_test",
                            "content": json.dumps({"text": "你好"})},
            },
        }
        frames = asyncio.Queue()
        sent = asyncio.Queue()
        accepting = asyncio.Event()
        release = asyncio.Event()
        states = []
        received = []

        class Socket:
            async def send(self, data):
                await sent.put(Frame.FromString(data))

            async def recv(self):
                return await frames.get()

        @asynccontextmanager
        async def socket(url, **kwargs):
            self.assertEqual(url, "wss://msg-frontier.feishu.cn/ws/v2?service_id=1")
            yield Socket()

        async def receive(*message):
            received.append(message)
            accepting.set()
            await release.wait()

        def enqueue(event, kind="event"):
            frame = Frame(SeqID=1, LogID=2, service=1, method=1, payload=json.dumps(event).encode())
            frame.headers.add(key="type", value=kind)
            frames.put_nowait(frame.SerializeToString())

        def submit(chat_type):
            payload["event"]["message"]["chat_type"] = chat_type
            enqueue(payload)

        def decide(sender, value):
            self.assertEqual(sender, "ou_test")
            self.assertEqual(value, {"approved": True})
            return {"toast": {"type": "success", "content": "已同意"}}

        transport = FeishuTransport("cli_test", "test-secret")
        endpoint = {"data": {"URL": "wss://msg-frontier.feishu.cn/ws/v2?service_id=1"}}
        with patch.object(transport, "_post", return_value=endpoint), patch.object(feishu_transport, "connect", socket):
            task = asyncio.create_task(transport.run(receive, states.append, on_action=decide))
            try:
                ping = await asyncio.wait_for(sent.get(), 2)
                self.assertEqual(ping.headers[0].value, "ping")
                submit("group")
                self.assertEqual(json.loads((await asyncio.wait_for(sent.get(), 2)).payload), {"code": 200})
                self.assertFalse(received)
                submit("p2p")
                await asyncio.wait_for(accepting.wait(), 2)
                self.assertTrue(sent.empty())
                self.assertEqual(received, [("om_test", "ou_test", "你好")])
                release.set()
                self.assertEqual(json.loads((await asyncio.wait_for(sent.get(), 2)).payload), {"code": 200})
                enqueue({"header": {"app_id": "cli_test", "event_type": "card.action.trigger"},
                         "event": {"operator": {"open_id": "ou_test"}, "action": {"value": {"approved": True}}}},
                        "card")
                ack = json.loads((await asyncio.wait_for(sent.get(), 2)).payload)
                self.assertEqual(ack["code"], 200)
                self.assertEqual(json.loads(b64decode(ack["data"]))["toast"]["content"], "已同意")
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(states, [True, False])

    async def test_reply_uses_stable_uuid_and_classifies_recalled_source(self):
        requests = []
        error_codes = iter((230011, 230020))

        def respond(request):
            requests.append(request)
            if request.url.path.endswith("/tenant_access_token/internal"):
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "t_test", "expire": 7200})
            return httpx.Response(400, json={"code": next(error_codes)})

        client_class = httpx.AsyncClient
        with patch.object(feishu_transport.httpx, "AsyncClient", side_effect=lambda **kwargs: client_class(
                transport=httpx.MockTransport(respond), **kwargs)):
            transport = FeishuTransport("cli_test", "test-secret")
            with self.assertRaises(FeishuReplyUnavailable):
                await transport.reply("om_test", "回复", "stable")
            with self.assertRaises(FeishuTransportError) as error:
                await transport.reply("om_test", "回复", "stable")
            self.assertNotIsInstance(error.exception, FeishuReplyUnavailable)

        self.assertEqual(len(requests), 3)
        for request in requests[1:]:
            self.assertEqual(request.url.path, "/open-apis/im/v1/messages/om_test/reply")
            self.assertEqual(request.headers["Authorization"], "Bearer t_test")
            body = json.loads(request.content)
            self.assertEqual(body["uuid"], "stable")
            self.assertEqual(json.loads(body["content"]), {"text": "回复"})
