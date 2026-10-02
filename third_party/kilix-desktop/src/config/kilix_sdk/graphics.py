"""Kitty graphics helpers for Kilix-hosted pixel applications."""

import gfx as _gfx

CHUNK = _gfx.CHUNK
FRAME_BYTES = _gfx.FRAME_BYTES
FramePresenter = _gfx.FramePresenter
FrameSocketTap = _gfx.FrameSocketTap
TappedFrame = _gfx.TappedFrame
PresentResult = _gfx.PresentResult
PresenterStats = _gfx.PresenterStats
PosixShmRing = _gfx.PosixShmRing
ShmBusy = _gfx.ShmBusy
write_frame = _gfx.write_frame
build_direct = _gfx.build_direct
blit_direct = _gfx.blit_direct
# Damage-aware updates: identify exact rectangles and edit those regions of the
# displayed root image in place (a=f frame edits).
diff_band = _gfx.diff_band
diff_damage_rects = _gfx.diff_damage_rects
diff_rect = _gfx.diff_rect
diff_rects = _gfx.diff_rects
extract_rect = _gfx.extract_rect
detect_vertical_scroll = _gfx.detect_vertical_scroll
build_compose = _gfx.build_compose
build_frame_edit = _gfx.build_frame_edit
blit_frame_edit = _gfx.blit_frame_edit
build_frame_edit_file = _gfx.build_frame_edit_file
build_frame_edit_shm = _gfx.build_frame_edit_shm
build_full_shm = _gfx.build_full_shm


def wrap_tmux_passthrough(apc: str) -> str:
    """Wrap one APC escape for tmux passthrough."""
    return _gfx.wrap_tmux_passthrough(apc)


__all__ = [
    "CHUNK", "FRAME_BYTES", "FramePresenter", "FrameSocketTap",
    "TappedFrame", "PresentResult", "PresenterStats", "PosixShmRing",
    "ShmBusy", "write_frame",
    "build_direct", "blit_direct", "diff_band", "diff_damage_rects",
    "diff_rect", "diff_rects", "extract_rect", "detect_vertical_scroll",
    "build_compose",
    "build_frame_edit", "blit_frame_edit", "build_frame_edit_file",
    "build_frame_edit_shm", "build_full_shm", "wrap_tmux_passthrough",
]
