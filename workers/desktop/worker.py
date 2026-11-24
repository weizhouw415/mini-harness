import asyncio
import contextlib
import ctypes
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from harness.core.approval_shell import is_safe_bash
from harness.execution.protocol import MAX_IMAGE_BYTES
from workers.common.actions import GUI_ACTIONS, point_fields, scroll_deltas, text_field
from workers.common.config import env_flag
from workers.common.shell import run_shell
from workers.common.skill_workspace import collect_skill_candidates, prepare_skill_workspace


ACTIONS = (*GUI_ACTIONS, "observe", "press")
CLICK_METHODS = {"click": "click", "double_click": "doubleClick", "right_click": "rightClick"}
WINDOWS_WHEEL = 0x0800
WINDOWS_HORIZONTAL_WHEEL = 0x1000
WINDOWS_WHEEL_DELTA = 120
WINDOWS_PIXELS_PER_NOTCH = 120
KEY_ALIASES = {
    "alt": "alt", "arrowdown": "down", "arrowleft": "left", "arrowright": "right", "arrowup": "up",
    "backspace": "backspace", "control": "ctrl", "delete": "delete", "enter": "enter", "escape": "esc",
    "meta": "command" if sys.platform == "darwin" else "win", "pagedown": "pagedown", "pageup": "pageup",
    "shift": "shift", "space": "space", "tab": "tab",
}


@dataclass
class Run:
    path: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False
    skill_workspace_prepared: bool = False


class WorkerBackend:
    def __init__(self, workspace_root: Path | None = None):
        self.workspace_root = Path(
            workspace_root or os.getenv("MINI_WORKER_WORKSPACE_ROOT") or Path.home() / ".mini-harness" / "workspaces"
        ).expanduser().resolve()
        self.pause_file = Path(os.getenv("MINI_DESKTOP_PAUSE_FILE") or self.workspace_root / ".paused").expanduser().resolve()
        self.desktop_enabled = env_flag("MINI_DESKTOP_CONTROL")
        self.bash_enabled = env_flag("MINI_DESKTOP_BASH")
        self._runs: dict[str, Run] = {}
        self._gui = None
        self._gui_lock = threading.Lock()

    @property
    def environment(self) -> str:
        return {"darwin": "macos-desktop", "win32": "windows-desktop"}.get(sys.platform, "unsupported-desktop")

    def capabilities(self) -> dict:
        actions = list(ACTIONS) if self.desktop_enabled else []
        return {
            "environment": self.environment, "desktop": True, "bash": self.bash_enabled,
            "safe_bash_path": True, "skill_workspace": self.bash_enabled and self.desktop_enabled,
            "computer": self.desktop_enabled, "download": False, "upload": False,
            "actions": actions, "computer_targets": {"desktop": actions},
        }

    def pause(self) -> None:
        self.pause_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.pause_file.touch(mode=0o600)

    def resume(self) -> None:
        self.pause_file.unlink(missing_ok=True)

    def check_available(self) -> None:
        if self.environment == "unsupported-desktop":
            raise RuntimeError("The desktop worker supports macOS and Windows")
        if not self.desktop_enabled:
            raise PermissionError("Set MINI_DESKTOP_CONTROL=1 on the desktop to authorize local control")
        if self.pause_file.exists():
            raise PermissionError("Desktop worker is paused. Resume it locally before continuing")

    async def open_run(self, run_id: str, workspace_id: str) -> None:
        self.check_available()
        if run_id in self._runs and not self._runs[run_id].closed:
            return
        if self._runs:
            raise RuntimeError("This desktop worker already has an active run")
        self.workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._runs[run_id] = Run(Path(tempfile.mkdtemp(prefix="run-", dir=self.workspace_root)))

    def _get_run(self, run_id: str) -> Run:
        try:
            return self._runs[run_id]
        except KeyError:
            raise ValueError("Unknown run") from None

    async def prepare_skill_workspace(self, run_id: str, skills: list[dict]) -> None:
        if not self.bash_enabled:
            raise PermissionError("Set MINI_DESKTOP_BASH=1 on the desktop to authorize local Bash")
        run = self._get_run(run_id)
        async with run.lock:
            self.check_available()
            if run.closed:
                raise ValueError("Run is closed")
            if not run.skill_workspace_prepared:
                prepare_skill_workspace(run.path, skills)
                run.skill_workspace_prepared = True

    async def collect_skill_candidates(self, run_id: str) -> dict:
        if not self.bash_enabled:
            raise PermissionError("Set MINI_DESKTOP_BASH=1 on the desktop to authorize local Bash")
        run = self._get_run(run_id)
        async with run.lock:
            self.check_available()
            if run.closed or not run.skill_workspace_prepared:
                raise ValueError("Run has no available skill workspace")
            return collect_skill_candidates(run.path)

    def _get_gui(self):
        if self._gui is None:
            import pyautogui

            pyautogui.FAILSAFE = True
            self._gui = pyautogui
        return self._gui

    @staticmethod
    def _point(gui, action: dict, x_name: str = "x", y_name: str = "y") -> tuple[int, int]:
        x, y = point_fields(action, x_name, y_name)
        if not gui.onScreen(x, y):
            raise ValueError("Coordinates are outside the primary screen")
        return x, y

    def _drag(self, gui, action: dict) -> None:
        start = self._point(gui, action)
        end = self._point(gui, action, "end_x", "end_y")
        if start in gui.FAILSAFE_POINTS or end in gui.FAILSAFE_POINTS:
            raise ValueError("Drag coordinates must avoid the mouse-corner fail-safe points")
        gui.mouseDown(*start, button="left")
        try:
            gui.dragTo(*end, duration=0.3, button="left", mouseDownUp=False)
        finally:
            try:
                gui.mouseUp(button="left")
            except gui.FailSafeException:
                # Release the held button at the current position before pausing.
                with contextlib.suppress(Exception):
                    gui.platformModule._mouseUp(*gui.position(), "left")
                raise

    def _scroll(self, gui, action: dict) -> None:
        dx, dy = scroll_deltas(action)
        if "x" in action or "y" in action:
            gui.moveTo(*self._point(gui, action))
            gui.failSafeCheck()
        if "direction" in action:
            if sys.platform == "darwin":
                import Quartz

                event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitPixel, 2, -dy, dx)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            else:
                # Windows scroll distance is set by each application and wheel settings.
                delta = dx or -dy
                notches = max(1, round(abs(delta) / WINDOWS_PIXELS_PER_NOTCH))
                wheel_delta = WINDOWS_WHEEL_DELTA * (notches if delta > 0 else -notches)
                event = WINDOWS_HORIZONTAL_WHEEL if dx else WINDOWS_WHEEL
                ctypes.windll.user32.mouse_event(event, 0, 0, wheel_delta, 0)
            return
        if dx and sys.platform == "win32":
            ctypes.windll.user32.mouse_event(WINDOWS_HORIZONTAL_WHEEL, 0, 0, dx * WINDOWS_WHEEL_DELTA, 0)
        elif dx:
            gui.hscroll(dx)
        if dy:
            gui.scroll(-dy)

    @staticmethod
    def _macos_capture_error(detail: str) -> Exception:
        import Quartz

        preflight = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
        if preflight is not None and not preflight():
            return PermissionError(
                "macOS has not authorized the desktop worker to capture the screen. "
                "Enable Screen & System Audio Recording for the app that launches Mini Harness "
                "in System Settings → Privacy & Security, then relaunch from that app."
            )
        return RuntimeError(f"macOS screen capture failed: {detail}")

    @classmethod
    def _macos_screenshot(cls):
        # Pillow's macOS grab opens its temporary file even if screencapture failed.
        with tempfile.TemporaryDirectory(prefix="mini-harness-screen-") as directory:
            path = Path(directory) / "screen.png"
            try:
                result = subprocess.run(
                    ["/usr/sbin/screencapture", "-x", "-m", "-t", "png", str(path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    encoding="utf-8", errors="replace", timeout=15,
                )
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("macOS screen capture timed out after 15 seconds.") from error
            if result.returncode:
                detail = result.stderr.strip()[:300] or f"screencapture exited with code {result.returncode}"
                raise cls._macos_capture_error(detail)
            from PIL import Image

            try:
                with Image.open(path, formats=["PNG"]) as captured:
                    return captured.copy()
            except OSError as error:
                raise cls._macos_capture_error("screencapture did not produce a readable PNG") from error

    @classmethod
    def _screenshot(cls, gui) -> dict:
        image = cls._macos_screenshot() if sys.platform == "darwin" else gui.screenshot()
        width, height = gui.size()
        if image.size != (width, height):
            image = image.resize((width, height))
        for format_name, options in (("PNG", {}), ("JPEG", {"quality": 65})):
            candidate = image if format_name == "PNG" else image.convert("RGB")
            with io.BytesIO() as output:
                candidate.save(output, format_name, **options)
                if output.tell() <= MAX_IMAGE_BYTES:
                    return {
                        "image": output.getvalue(), "mime_type": f"image/{format_name.lower()}",
                        "width": width, "height": height,
                    }
        raise ValueError("Desktop screenshot exceeds the image size limit")

    @staticmethod
    def _press(gui, raw_key: str) -> None:
        keys = [raw_key] if raw_key == "+" else raw_key.split("+")
        normalized = []
        for key in keys:
            lowered = key.strip().lower()
            if lowered == "controlormeta":
                lowered = "meta" if sys.platform == "darwin" else "control"
            normalized.append(KEY_ALIASES.get(lowered, lowered))
        if not normalized or any(key not in gui.KEYBOARD_KEYS for key in normalized):
            raise ValueError("Unsupported desktop keyboard key or shortcut")
        if len(normalized) == 1:
            gui.press(normalized[0])
        else:
            gui.hotkey(*normalized)

    def _computer(self, run: Run, name: str, action: dict) -> dict:
        with self._gui_lock:
            self.check_available()
            if run.closed:
                raise ValueError("Run is closed")
            gui = self._get_gui()
            try:
                gui.failSafeCheck()
                if name in {"screenshot", "observe"}:
                    return self._screenshot(gui)
                if name in CLICK_METHODS:
                    getattr(gui, CLICK_METHODS[name])(*self._point(gui, action))
                elif name == "move":
                    gui.moveTo(*self._point(gui, action))
                elif name == "drag":
                    self._drag(gui, action)
                elif name == "type":
                    value = text_field(action, "text", allow_empty=True, limit=4096)
                    if any(not (32 <= ord(char) < 127 or char in "\n\t") for char in value):
                        raise ValueError("Desktop typing supports ASCII text, newlines, and tabs")
                    for start in range(0, len(value), 64):
                        self.check_available()
                        if run.closed:
                            raise ValueError("Run is closed")
                        gui.write(value[start:start + 64])
                elif name == "scroll":
                    self._scroll(gui, action)
                elif name in {"key", "press"}:
                    self._press(gui, text_field(action, "key", limit=80))
            except gui.FailSafeException as exc:
                self.pause()
                raise PermissionError("Desktop worker paused by the mouse-corner fail-safe") from exc
            return {"ok": True}

    async def computer(self, run_id: str, action: dict) -> dict:
        if action.get("target", "desktop") != "desktop":
            raise ValueError("This worker only accepts desktop computer actions")
        name = text_field(action, "action", limit=40)
        if name not in ACTIONS:
            raise ValueError(f"Unsupported desktop action: {name}")
        run = self._get_run(run_id)
        async with run.lock:
            task = asyncio.create_task(asyncio.to_thread(self._computer, run, name, action))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                run.closed = True
                with contextlib.suppress(Exception):
                    await task
                raise

    async def _bash_arguments(self, path: Path, command: str) -> list[str]:
        configured_path = os.getenv("MINI_DESKTOP_BASH_PATH")
        # Commands eligible for automatic approval must resolve external tools
        # from the system rather than from the user's PATH. Privileged mode also
        # ignores Bash startup hooks and imported shell functions (notably in WSL).
        safe = is_safe_bash(command)
        if safe:
            command = "PATH=/usr/bin:/bin\n" + command
        options = ["--noprofile", "--norc"] + (["-p"] if safe else []) + ["-c", command]
        if sys.platform != "win32":
            return [configured_path or "/bin/bash", *options]
        kind = os.getenv("MINI_DESKTOP_BASH_KIND", "git-bash")
        if kind == "git-bash":
            candidates = [
                Path(os.environ[key]) / "Git" / "bin" / "bash.exe"
                for key in ("ProgramFiles", "ProgramFiles(x86)") if key in os.environ
            ]
            executable = configured_path or next((str(path) for path in candidates if path.is_file()), None)
            if not executable:
                raise RuntimeError("Git Bash was not found. Set MINI_DESKTOP_BASH_PATH or use WSL")
            return [executable, *options]
        if kind == "wsl":
            executable = configured_path or "wsl.exe"
            conversion = await run_shell(
                [executable, "--exec", "wslpath", "-a", "-u", str(path)],
                cwd=path, environment=self._bash_environment(), timeout=10,
            )
            linux_path = conversion["stdout"].strip()
            if conversion["exit_code"] != 0 or not linux_path.startswith("/"):
                raise RuntimeError("WSL could not convert the run workspace path")
            return [executable, "--cd", linux_path, "--exec", "/bin/bash", *options]
        raise ValueError("MINI_DESKTOP_BASH_KIND must be git-bash or wsl")

    @staticmethod
    def _bash_environment() -> dict[str, str]:
        names = {
            "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "PROGRAMFILES", "PROGRAMFILES(X86)",
            "TMP", "TEMP", "LANG", "TERM", "LOCALAPPDATA", "APPDATA",
        }
        return {name: value for name, value in os.environ.items() if name.upper() in names}

    async def _watch_pause(self) -> None:
        while not self.pause_file.exists():
            await asyncio.sleep(0.2)

    async def bash(self, run_id: str, command: str, timeout: float) -> dict:
        if not self.bash_enabled:
            raise PermissionError("Set MINI_DESKTOP_BASH=1 on the desktop to authorize local Bash")
        run = self._get_run(run_id)
        async with run.lock:
            self.check_available()
            if run.closed:
                raise ValueError("Run is closed")
            arguments = await self._bash_arguments(run.path, command)
            self.check_available()
            shell = asyncio.create_task(run_shell(
                arguments, cwd=run.path, environment=self._bash_environment(), timeout=timeout,
            ))
            pause = asyncio.create_task(self._watch_pause())
            try:
                completed, _ = await asyncio.wait({shell, pause}, return_when=asyncio.FIRST_COMPLETED)
                if pause in completed:
                    raise PermissionError("Desktop worker was paused while Bash was running")
                return shell.result()
            finally:
                for task in (shell, pause):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(shell, pause, return_exceptions=True)

    async def close_run(self, run_id: str) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        run.closed = True
        async with run.lock:
            with contextlib.suppress(FileNotFoundError):
                await asyncio.to_thread(shutil.rmtree, run.path)
            self._runs.pop(run_id, None)

    async def close(self) -> None:
        for run_id in list(self._runs):
            await self.close_run(run_id)
