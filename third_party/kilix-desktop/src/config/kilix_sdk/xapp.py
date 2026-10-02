"""Private X11 application sessions for Kilix providers.

The host owns process supervision, display authentication, capture fallback,
and input cleanup.  Providers own presentation and any desktop-specific window
management layered on top of the private display.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
import re
import subprocess
import threading
from typing import Iterable, Mapping

import stream
import xcapture
import xinject
from Xlib import display as xdisplay


_XAUTHORITY_LOCK = threading.RLock()
_PROCESS_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@contextmanager
def _temporary_xauthority(path: str):
    """Scope python-xlib's process-global XAUTHORITY lookup to one connect."""
    if not path:
        raise RuntimeError("private X display has no authority file")
    with _XAUTHORITY_LOCK:
        marker = object()
        previous = os.environ.get("XAUTHORITY", marker)
        os.environ["XAUTHORITY"] = path
        try:
            yield
        finally:
            if previous is marker:
                os.environ.pop("XAUTHORITY", None)
            else:
                os.environ["XAUTHORITY"] = previous


def _positive(value: int | None, fallback: int, label: str) -> int:
    selected = fallback if value is None else int(value)
    if selected <= 0:
        raise ValueError(f"{label} must be positive")
    return selected


def _stop_process(process, timeout: float = 2.0) -> None:
    if process is None:
        return
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=timeout)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
            try:
                process.wait(timeout=1)
            except Exception:
                pass
    for handle in (getattr(process, "stdin", None),
                   getattr(process, "stdout", None),
                   getattr(process, "stderr", None)):
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


@dataclass(frozen=True)
class CaptureStart:
    """Result of selecting a private-display capture backend."""

    backend: str
    initial_frame: bytes | None = None
    damage_error: Exception | None = None


class XAppSession:
    """Own one authenticated private X server and its application processes."""

    def __init__(self, session: str, width: int, height: int, fps: int = 30,
                 *, supervisor=None):
        width, height, fps = int(width), int(height), int(fps)
        if width <= 0 or height <= 0:
            raise ValueError("X app dimensions must be positive")
        if fps <= 0:
            raise ValueError("X app capture rate must be positive")
        self.session = session
        self.width = width
        self.height = height
        self.fps = fps
        self.supervisor = supervisor or stream.StreamSupervisor(session)
        self.number = None
        self.display = None
        self.server = None
        self.xd = None
        self.app = None
        self.injector = None
        self.capture = None
        self.capture_process = None
        self.capture_backend = "pending"
        self._capture_seq = 0
        self._closed = False

    @property
    def xauthority(self) -> str | None:
        return self.supervisor.xauth

    def _select_number(self, number: int | None) -> int:
        if self.number is not None:
            raise RuntimeError("private X display is already started")
        selected = (
            self.supervisor.pick_display() if number is None else int(number))
        if not 0 <= selected <= 65535:
            raise ValueError("X display number must be between 0 and 65535")
        return selected

    def start_xvfb(self, *, width: int | None = None,
                   height: int | None = None, nocursor: bool = False,
                   number: int | None = None) -> int:
        selected_width = _positive(width, self.width, "X app width")
        selected_height = _positive(height, self.height, "X app height")
        number = self._select_number(number)
        self.server = self.supervisor.start_xvfb(
            number, selected_width, selected_height,
            nocursor=nocursor)
        self.number, self.display = number, f":{number}"
        return number

    def start_xvnc(self, port: int, password_file: str, *,
                   desktop: str = "kilix", width: int | None = None,
                   height: int | None = None,
                   number: int | None = None) -> int:
        selected_width = _positive(width, self.width, "X app width")
        selected_height = _positive(height, self.height, "X app height")
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError("VNC port must be between 1 and 65535")
        number = self._select_number(number)
        self.server = self.supervisor.start_xvnc(
            number, selected_width, selected_height, port,
            password_file, desktop=desktop)
        self.number, self.display = number, f":{number}"
        return number

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        if self.display is None or not self.xauthority:
            raise RuntimeError("private X display has not started")
        env = dict(os.environ)
        if extra:
            env.update(extra)
        # The application/capture always belongs to this private display;
        # provider-supplied environment cannot redirect either X client.
        env["DISPLAY"] = self.display
        env["XAUTHORITY"] = self.xauthority
        return env

    def connect(self):
        if self.xd is None:
            if self.display is None or not self.xauthority:
                raise RuntimeError("private X display has not started")
            with _temporary_xauthority(self.xauthority):
                self.xd = xdisplay.Display(self.display)
        return self.xd

    def launch_app(self, command: Iterable[str], *,
                   env: Mapping[str, str] | None = None,
                   cwd: str | None = None,
                   stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL):
        if self.app is not None:
            raise RuntimeError("private X application is already running")
        argv = list(command)
        if not argv:
            raise ValueError("X app command must not be empty")
        self.app = self.supervisor.spawn(
            "app", argv, env=self.environment(env), cwd=cwd,
            stdout=stdout, stderr=stderr)
        return self.app

    def make_injector(self, *, width: int | None = None,
                      height: int | None = None):
        if self.injector is None:
            selected_width = _positive(width, self.width, "X app width")
            selected_height = _positive(height, self.height, "X app height")
            self.injector = xinject.Injector(
                self.connect(), selected_width, selected_height)
        return self.injector

    def set_geometry(self, width: int, height: int) -> None:
        width, height = int(width), int(height)
        if width <= 0 or height <= 0:
            raise ValueError("X app dimensions must be positive")
        self.width, self.height = width, height
        if self.injector is not None:
            self.injector.app_w, self.injector.app_h = self.width, self.height

    def start_capture(self, *, fps: int | None = None,
                      draw_cursor: bool = True, prefer_damage: bool = True,
                      capture_name: str = "cap") -> CaptureStart:
        """Use XDamage/MIT-SHM when available, otherwise supervised ffmpeg."""
        if self.display is None or not self.xauthority:
            raise RuntimeError("private X display has not started")
        rate = _positive(fps, self.fps, "X app capture rate")
        if not isinstance(capture_name, str) or not _PROCESS_NAME.fullmatch(
                capture_name):
            raise ValueError("capture name must be a short, plain process name")
        self.stop_capture()
        damage_error = None
        if prefer_damage and os.environ.get("KILIX_XDAMAGE_CAPTURE", "1") != "0":
            candidate = None
            try:
                # python-xlib resolves XAUTHORITY from the process environment.
                # Keep both the connection and its first request scoped to the
                # private display's cookie without leaking it to the host.
                with _temporary_xauthority(self.xauthority):
                    candidate = xcapture.XDamageCapture(
                        self.display, self.width, self.height,
                        draw_cursor=draw_cursor)
                    initial = candidate.snapshot()
            except Exception as error:
                damage_error = error
                if candidate is not None:
                    try:
                        candidate.close()
                    except Exception:
                        pass
            else:
                self.capture = candidate
                self.capture_backend = "xdamage+mit-shm"
                return CaptureStart(self.capture_backend, initial)

        self._capture_seq += 1
        name = capture_name if self._capture_seq == 1 else f"{capture_name}-{self._capture_seq}"
        argv = [
            "ffmpeg", "-loglevel", "quiet", "-f", "x11grab",
            "-draw_mouse", "1" if draw_cursor else "0",
            "-framerate", str(rate), "-video_size",
            f"{self.width}x{self.height}", "-i", self.display,
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ]
        process = self.supervisor.spawn(
            name, argv, env=self.environment(), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        self.capture_process = process
        try:
            if process.stdout is None:
                raise RuntimeError("capture process has no stdout pipe")
            os.set_blocking(process.stdout.fileno(), False)
        except Exception:
            self.capture_process = None
            _stop_process(process)
            self.capture_backend = "stopped"
            raise
        self.capture_backend = f"ffmpeg@{rate}"
        return CaptureStart(self.capture_backend, damage_error=damage_error)

    def stop_capture(self) -> None:
        if self.capture is not None:
            capture, self.capture = self.capture, None
            try:
                capture.close()
            except Exception:
                pass
        if self.capture_process is not None:
            process, self.capture_process = self.capture_process, None
            _stop_process(process)
        self.capture_backend = "stopped"

    def release_input(self) -> None:
        if self.injector is not None:
            try:
                self.injector.release_all()
            except Exception:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.release_input()
        self.stop_capture()
        if self.xd is not None:
            try:
                self.xd.close()
            except Exception:
                pass
            self.xd = None
        self.supervisor.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, _kind, _value, _traceback):
        self.close()


__all__ = ["CaptureStart", "XAppSession"]
