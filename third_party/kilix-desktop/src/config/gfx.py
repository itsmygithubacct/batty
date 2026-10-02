"""Kilix compatibility surface for :mod:`kitty_frame_presenter`.

New renderers use ``FramePresenter`` directly.  The function-level helpers
remain exported for the public Kilix SDK and for streamed clients built against
older releases.
"""

import base64
import hashlib
import os
import re
import stat
import sys
import time
from pathlib import Path


def _load_shared_package():
    root = Path(__file__).resolve().parents[1]
    candidates = (
        root / "third_party" / "kitty-frame-presenter" / "src",
        root.parent / "kilix-modules" / "kitty-frame-presenter" / "src",
    )
    for candidate in candidates:
        if candidate.is_dir():
            sys.path.insert(0, str(candidate))
            import kitty_frame_presenter as package
            return package
    try:
        import kitty_frame_presenter as package
        return package
    except ImportError as error:
        raise ImportError(
            "kitty-frame-presenter is unavailable; initialize Kilix "
            "submodules with: git submodule update --init --recursive") from error


_shared = _load_shared_package()
_BROKER_SESSION = re.compile(r"[0-9a-f]{16,64}\Z")

CHUNK = _shared.CHUNK
FRAME_BYTES = _shared.FRAME_BYTES
FrameSocketTap = _shared.FrameSocketTap
TappedFrame = _shared.TappedFrame
_SharedFramePresenter = _shared.FramePresenter
PresentResult = _shared.PresentResult
PresenterStats = _shared.PresenterStats
PosixShmRing = _shared.PosixShmRing
ShmBusy = _shared.ShmBusy
build_compose = _shared.build_compose
build_direct = _shared.build_direct
build_frame_edit = _shared.build_frame_edit
build_frame_edit_shm = _shared.build_frame_edit_shm
build_full_shm = _shared.build_full_shm
detect_vertical_scroll = _shared.detect_vertical_scroll
diff_band = _shared.diff_band
diff_damage_rects = _shared.diff_damage_rects
diff_rect = _shared.diff_rect
diff_rects = _shared.diff_rects
extract_rect = _shared.extract_rect
wrap_tmux_passthrough = _shared.wrap_tmux_passthrough


def _remote_frame_socket(session: str) -> Path:
    session_home = Path(os.environ.get(
        "KILIX_SESSION_HOME",
        os.path.expanduser("~/.local/gpu_terminal/kilix/session"),
    ))
    candidate = session_home / "remote" / f"{session}.tap"
    if len(os.fsencode(candidate)) >= 107:
        digest = hashlib.sha256(session.encode("ascii")).hexdigest()[:24]
        candidate = session_home / "remote" / f"{digest}.tap"
    return candidate


class _KilixFrameTap:
    """Discover a running local multiplexer without burdening local drawing."""

    def __init__(self, session: str):
        self.session = session
        self.path = _remote_frame_socket(session)
        self.delegate = None
        self.next_check = float("-inf")

    def __call__(self, frame: TappedFrame) -> None:
        if self.delegate is not None:
            self.delegate(frame)
            return
        now = time.monotonic()
        if now < self.next_check:
            return
        self.next_check = now + 1.0
        try:
            info = self.path.lstat()
        except OSError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            return
        self.delegate = FrameSocketTap(str(self.path), self.session)
        self.delegate(frame)

    def close(self) -> None:
        if self.delegate is not None:
            self.delegate.close()
            self.delegate = None


class FramePresenter(_SharedFramePresenter):
    """Shared presenter with lazy, session-scoped Kilix frame discovery."""

    def __init__(self, *args, **kwargs):
        self._kilix_frame_tap = None
        if "tap" not in kwargs:
            session = os.environ.get("KITTY_PTY_BROKER_SESSION", "")
            if _BROKER_SESSION.fullmatch(session):
                self._kilix_frame_tap = _KilixFrameTap(session)
                kwargs["tap"] = self._kilix_frame_tap
        super().__init__(*args, **kwargs)

    def close(self, *, discard: bool = False) -> None:
        super().close(discard=discard)
        if self._kilix_frame_tap is not None:
            self._kilix_frame_tap.close()
            self._kilix_frame_tap = None


def session_dir(*parts: str) -> str:
    """Create and return a private Kilix session directory."""
    root = os.environ.get("KILIX_SESSION_HOME") or os.path.join(
        os.environ.get("KILIX_STORAGE_HOME", os.path.expanduser(
            "~/.local/gpu_terminal/kilix")), "session")
    path = os.path.abspath(os.path.join(root, *parts))
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_frame(path: str, data: bytes) -> None:
    """Write a legacy one-shot ``t=t`` frame without path reuse."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


# Compatibility for callers predating the public helper name.
_tmux_wrap = wrap_tmux_passthrough


def blit_direct(term, rgb: bytes, width: int, height: int, columns: int,
                rows: int, image_id: int, origin_row: int = 1,
                origin_column: int = 1, in_tmux: bool = False) -> int:
    sequence = build_direct(rgb, width, height, columns, rows, image_id,
                            origin_row, origin_column, in_tmux)
    if sequence:
        term.write(sequence)
    return len(sequence)


def blit_frame_edit(term, rgb: bytes, width: int, height: int, x: int, y: int,
                    image_id: int, in_tmux: bool = False) -> int:
    sequence = build_frame_edit(rgb, width, height, x, y, image_id, in_tmux)
    if sequence:
        term.write(sequence)
    return len(sequence)


def build_frame_edit_file(path: str, width: int, height: int, x: int, y: int,
                          image_id: int) -> str:
    """Legacy ``t=t`` builder retained for SDK compatibility."""
    payload = base64.b64encode(path.encode()).decode()
    return (f"\x1b_Ga=f,i={image_id},r=1,x={x},y={y},t=t,f=24,N=1,"
            f"s={width},v={height},q=2;{payload}\x1b\\")


__all__ = [
    "CHUNK", "FRAME_BYTES", "FramePresenter", "FrameSocketTap", "TappedFrame",
    "PresentResult", "PresenterStats", "PosixShmRing", "ShmBusy",
    "session_dir", "write_frame",
    "build_compose", "build_direct", "blit_direct", "build_frame_edit",
    "blit_frame_edit", "build_frame_edit_file", "build_frame_edit_shm",
    "build_full_shm", "detect_vertical_scroll", "diff_band",
    "diff_damage_rects", "diff_rect", "diff_rects", "extract_rect",
    "wrap_tmux_passthrough",
]
