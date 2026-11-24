import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from workers.common.browser import BrowserController, BrowserSession
from workers.desktop.worker import Run, WorkerBackend


class BrowserGesturesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.controller = BrowserController(None, chromium_sandbox=False)
        self.page = Mock()
        self.page.mouse = AsyncMock()
        self.page.keyboard = AsyncMock()
        self.page.locator.return_value = AsyncMock()
        self.controller._get_page = AsyncMock(return_value=self.page)

    async def computer(self, name, **arguments):
        return await self.controller.computer(BrowserSession(), Path("."), {"action": name, **arguments})

    async def test_dispatches_graphical_gestures_and_legacy_scroll(self):
        await self.computer("double_click", selector="#item")
        self.page.locator.assert_called_once_with("#item")
        self.page.locator.return_value.click.assert_awaited_once_with(click_count=2)
        await self.computer("right_click", x=10, y=20)
        await self.computer("move", x=30, y=40)
        await self.computer("drag", x=50, y=60, end_x=70, end_y=80)
        await self.computer("scroll", x=90, y=100, direction="left", amount=120)
        await self.computer("scroll", dx=12, dy=-20)
        self.assertEqual(self.page.mouse.method_calls, [
            call.click(10, 20, button="right"), call.move(30, 40), call.move(50, 60),
            call.down(), call.move(70, 80, steps=10), call.up(), call.move(90, 100),
            call.wheel(-120, 0), call.wheel(12, -20),
        ])
        await self.computer("key", key="Control+a")
        await self.computer("press", key="Enter")
        self.assertEqual(self.page.keyboard.press.await_args_list, [call("Control+a"), call("Enter")])

    async def test_invalid_or_failed_drag_does_not_leave_button_down(self):
        with self.assertRaisesRegex(ValueError, "end_y"):
            await self.computer("drag", x=10, y=20, end_x=30)
        self.assertEqual(self.page.mouse.method_calls, [])

        self.page.mouse.move.side_effect = [None, RuntimeError("Pointer move failed")]
        with self.assertRaisesRegex(RuntimeError, "Pointer move failed"):
            await self.computer("drag", x=10, y=20, end_x=30, end_y=40)
        self.page.mouse.up.assert_awaited_once_with()

    async def test_screen_info_does_not_launch_browser_or_capture_screen(self):
        self.assertEqual(await self.computer("screen_info"), {"target": "browser", "width": 1280, "height": 720})
        self.controller._get_page.assert_not_awaited()


class DesktopGesturesTest(unittest.TestCase):
    def setUp(self):
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        workspace_root = Path(workspace.name)
        self.worker = WorkerBackend(workspace_root)
        self.worker.pause_file = workspace_root / ".paused"
        self.worker.check_available = Mock()
        self.worker._get_gui = Mock()
        self.gui = self.worker._get_gui.return_value
        self.gui.onScreen.return_value = True
        self.gui.FAILSAFE_POINTS = [(0, 0)]
        self.gui.KEYBOARD_KEYS = ["enter"]
        self.gui.FailSafeException = type("FailSafeException", (Exception,), {})
        self.run = Run(workspace_root)

    def computer(self, name, **arguments):
        return self.worker._computer(self.run, name, {"action": name, **arguments})

    def test_click_move_drag_and_key_dispatch(self):
        self.computer("double_click", x=10, y=20)
        self.gui.doubleClick.assert_called_once_with(10, 20)
        self.computer("right_click", x=30, y=40)
        self.gui.rightClick.assert_called_once_with(30, 40)
        self.computer("move", x=50, y=60)
        self.gui.moveTo.assert_called_once_with(50, 60)
        self.computer("drag", x=10, y=20, end_x=70, end_y=80)
        self.gui.mouseDown.assert_called_once_with(10, 20, button="left")
        self.gui.dragTo.assert_called_once_with(70, 80, duration=0.3, button="left", mouseDownUp=False)
        self.gui.mouseUp.assert_called_once_with(button="left")
        self.computer("key", key="Enter")
        self.gui.press.assert_called_once_with("enter")

    def test_drag_fail_safe_releases_button_and_pauses_worker(self):
        self.gui.dragTo.side_effect = self.gui.FailSafeException
        self.gui.mouseUp.side_effect = self.gui.FailSafeException
        self.gui.position.return_value = (0, 0)
        with self.assertRaisesRegex(PermissionError, "fail-safe"):
            self.computer("drag", x=10, y=20, end_x=30, end_y=40)
        self.gui.platformModule._mouseUp.assert_called_once_with(0, 0, "left")
        self.assertTrue(self.worker.pause_file.is_file())

    def test_scroll_converts_pixels_for_native_platforms(self):
        quartz = SimpleNamespace(kCGScrollEventUnitPixel=0, kCGHIDEventTap=0,
                                 CGEventCreateScrollWheelEvent=Mock(), CGEventPost=Mock())
        with patch("workers.desktop.worker.sys.platform", "darwin"), patch.dict("sys.modules", Quartz=quartz):
            self.computer("scroll", direction="down", amount=85)
            quartz.CGEventCreateScrollWheelEvent.assert_called_once_with(None, 0, 2, -85, 0)
            quartz.CGEventPost.assert_called_once_with(0, quartz.CGEventCreateScrollWheelEvent.return_value)

        windll = Mock()
        with patch("workers.desktop.worker.sys.platform", "win32"), \
                patch("workers.desktop.worker.ctypes.windll", windll, create=True):
            self.computer("scroll", direction="left", amount=220)
            windll.user32.mouse_event.assert_called_once_with(0x01000, 0, 0, -240, 0)

    def test_macos_capture_failure_reports_permission_or_native_error(self):
        scope = SimpleNamespace(CGPreflightScreenCaptureAccess=Mock(side_effect=[False, True]))
        screenshot = SimpleNamespace(returncode=1, stderr="could not create image from display\n")
        with patch("workers.desktop.worker.sys.platform", "darwin"), \
                patch("workers.desktop.worker.subprocess.run", return_value=screenshot) as native, \
                patch.dict("sys.modules", Quartz=scope):
            with self.assertRaisesRegex(PermissionError, "Screen & System Audio Recording"):
                self.computer("screenshot")
            with self.assertRaisesRegex(RuntimeError, "could not create image from display"):
                self.computer("observe")

        self.gui.screenshot.assert_not_called()
        for args in native.call_args_list:
            self.assertFalse(Path(args.args[0][-1]).parent.exists())
