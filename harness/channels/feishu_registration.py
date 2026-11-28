from __future__ import annotations

import asyncio
from base64 import b64encode
from io import BytesIO
from urllib.parse import urlsplit
from uuid import uuid4

from harness.channels.feishu import FeishuService, FeishuSettings
from harness.storage.sqlite import Conflict, NotFound


class FeishuRegistration:
    def __init__(self, service: FeishuService):
        self.service = service
        self.task: asyncio.Task | None = None
        self.state: dict = {}

    def start(self) -> dict:
        if self.task and not self.task.done():
            raise Conflict("已有飞书扫码配置正在进行，请先取消或完成。")
        self.state = {"id": uuid4().hex, "status": "starting"}
        self.task = asyncio.create_task(self._register(self.service.config), name="feishu-registration")
        return self.state.copy()

    def status(self, registration_id: str) -> dict:
        if self.state.get("id") != registration_id:
            raise NotFound("此二维码已失效，请重新生成。")
        return self.state.copy()

    async def cancel(self, registration_id: str):
        if self.status(registration_id)["status"] in {"starting", "waiting"}:
            await self.stop()

    async def stop(self):
        if self.task and not self.task.done():
            if self.state["status"] != "saving":
                self.task.cancel()
                self._finish("cancelled")
            await asyncio.gather(self.task, return_exceptions=True)

    def _finish(self, status: str, **details):
        self.state = {"id": self.state["id"], "status": status, **details}

    async def _register(self, previous: FeishuSettings):
        try:
            import qrcode
            from lark_oapi import aregister_app
            from lark_oapi.scene.registration.errors import AppAccessDeniedError, AppExpiredError
        except ImportError:
            self._finish("error", error="缺少飞书扫码依赖。请使用 mini-harness local 重新启动，或重新安装 mini-harness。")
            return

        def show_code(info: dict):
            url = info["url"]
            target = urlsplit(url)
            if target.scheme != "https" or target.hostname not in {"accounts.feishu.cn", "open.feishu.cn"}:
                raise ValueError("Unexpected Feishu registration URL")
            image = BytesIO()
            qrcode.make(url).save(image, format="PNG")
            self._finish("waiting", qr_png=b64encode(image.getvalue()).decode("ascii"), url=url)

        def check_brand(info: dict):
            if info.get("status") == "domain_switched":
                raise UnsupportedTenant

        try:
            async with asyncio.timeout(3660):
                result = await aregister_app(
                    on_qr_code=show_code,
                    on_status_change=check_brand,
                    source="mini-harness",
                    app_preset={"name": "Mini Harness", "desc": "通过飞书单聊提交 Mini Harness 任务"},
                    addons={
                        "preset": False,
                        "scopes": {"tenant": ["im:message.p2p_msg:readonly", "im:message:send_as_bot",
                                              "im:message.reactions:write_only"]},
                        "events": {"items": {"tenant": ["im.message.receive_v1"]}},
                        "callbacks": {"items": ["card.action.trigger"]},
                    },
                    create_only=True,
                )
            user = result.get("user_info") or {}
            if user.get("tenant_brand") == "lark":
                raise UnsupportedTenant
            config = FeishuSettings(
                enabled=True, app_id=result["client_id"], app_secret=result["client_secret"],
                allowed_open_ids=[user["open_id"]],
            )
            self._finish("saving")
            await self.service.save(config, expected=previous)
            self._finish("completed", config=self.service.status())
        except (AppExpiredError, TimeoutError):
            self._finish("expired", error="二维码已过期，请重新生成。")
        except AppAccessDeniedError:
            self._finish("cancelled", error="已在飞书取消授权。")
        except UnsupportedTenant:
            self._finish("error", error="请使用飞书账号扫码。")
        except Conflict as error:
            self._finish("error", error=str(error))
        except Exception:
            self._finish("error", error="飞书扫码配置失败，请重新生成二维码或使用手动配置。")


class UnsupportedTenant(ValueError):
    pass
