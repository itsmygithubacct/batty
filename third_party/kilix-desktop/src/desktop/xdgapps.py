"""kilix desktop — freedesktop application discovery.

The scanner itself lives in the host SDK (`kilix_sdk.xdgapps`, SDK 1.8) so
every desktop and the launcher catalog share one implementation. This module
keeps the desktop-side half: icon mapping and `launch(shell, entry)`, which
hands a discovered entry to Shell.launch. Everything is driven by
$XDG_DATA_HOME / $XDG_DATA_DIRS (spec defaults when unset); nothing about the
host machine is hardcoded.
"""
import shlex

import widgets as W
import wm

from kilix_sdk import xdgapps as _shared

# Discovery is the SDK's; these names stay importable here so the desktop's
# menus and tests keep one vocabulary.
BUCKET_ORDER = _shared.BUCKET_ORDER
app_dirs = _shared.app_dirs
scan = _shared.scan
bucket = _shared.bucket
grouped = _shared.grouped

# bucket → an existing kilix icon name (generic "app" fallback)
_BUCKET_ICONS = {
    "Accessories": "app",
    "Development": "terminal",
    "Education": "doc_text",
    "Games": "exe",
    "Graphics": "paint",
    "Internet": "browser",
    "Multimedia": "amp",
    "Office": "doc_text",
    "System": "settings",
    "Other": "app",
}


def icon_for(entry):
    return _BUCKET_ICONS.get(bucket(entry), "app")


# ── launching ────────────────────────────────────────────────────────────────

def launch(shell, entry, mode="tab"):
    """Open a discovered app. mode "tab" (default) synthesizes a launcher spec
    and runs it in a kilix tab; "window" streams it into a Win95 desktop window
    via XPane (the way the media player runs); "fullscreen" is the same, sized
    to the whole screen."""
    name = entry.get("name") or "app"
    if mode in ("window", "fullscreen"):
        try:                               # malformed Exec must not kill the desktop
            argv = shlex.split(entry.get("exec", ""))
        except ValueError:
            argv = []
        if not argv:                       # discovered entries always have one
            wm.msgbox(shell.desk, name, "Launcher has no Exec line.",
                      icon="error")
            return
        size = shell.desk.size() if mode == "fullscreen" else None
        shell.open_in_xpane(argv, name, icon=icon_for(entry),
                            cwd=entry.get("workdir") or None, app_size=size)
        return
    spec = {
        "Name": name,
        "Exec": entry.get("exec", ""),
        "Path": entry.get("workdir") or "~",
        "X-Kilix-Open": "tab" if entry.get("terminal") else "run",
    }
    shell.launch(spec)


def app_context(shell, entry):
    """Right-click menu: run the app in a kilix tab or a desktop window."""
    MI = W.MenuItem
    items = [MI("Open in tab", action=lambda: launch(shell, entry, "tab"))]
    if not entry.get("terminal"):          # no tty on Xvfb → dead window
        items.append(
            MI("Open in window", action=lambda: launch(shell, entry, "window")))
        items.append(
            MI("Open fullscreen", action=lambda: launch(shell, entry, "fullscreen")))
    return items
