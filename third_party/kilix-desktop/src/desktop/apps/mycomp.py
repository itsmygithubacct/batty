"""kilix desktop — My Computer, the top-level namespace.

A large-icon view of the machine: the filesystem root as a drive, the user's
Home, the Desktop folder, plus Control Panel (Settings) and the Recycle Bin.
Double-clicking a drive/folder opens a File Manager there; the Recycle Bin
opens the shared Recycle Bin window.
"""
import os

import recycle
import theme as T
import widgets as W
import wm

STATUS_H = 20


def _bin_icon():
    # mirror shell.refresh(): full when the bin holds items, else empty
    return "recyclebin_full" if recycle.has_items() else "recyclebin_empty"


class MyComputer(wm.Window):
    def __init__(self, desk, arg=None):
        super().__init__(desk, "My Computer", 480, 340, icon="computer")
        self.min_w, self.min_h = 320, 220
        cw, ch = self.client_size()
        self.menubar = self.add(W.MenuBar(cw, [
            ("File", self._file_menu), ("Help", self._help_menu)]))
        self.grid = self.add(W.IconGrid(
            2, T.MENU_H + 2, cw - 4, ch - T.MENU_H - STATUS_H - 4,
            on_activate=self._activate))
        self.grid.set_items(self._entries())
        self.set_focus(self.grid)
        self._bin_icon = _bin_icon()
        self.desk.tick_hooks.append(self._refresh_bin)

    def _refresh_bin(self, *_):
        # keep the Recycle Bin entry's icon in sync with the bin's fullness
        icon = _bin_icon()
        if icon == self._bin_icon:
            return
        self._bin_icon = icon
        for it in self.grid.items:
            if it["data"] == ("bin", None):
                it["icon"] = icon
        self.grid.invalidate()

    def close(self):
        if self._refresh_bin in self.desk.tick_hooks:
            self.desk.tick_hooks.remove(self._refresh_bin)
        super().close()

    def _entries(self):
        return [
            {"label": "Local Disk (/)", "icon": "drive",
             "data": ("filemgr", "/")},
            {"label": "Home", "icon": "home",
             "data": ("filemgr", os.path.expanduser("~"))},
            {"label": "Desktop", "icon": "folder",
             "data": ("filemgr", self.desk.shell.dir)},
            {"label": "Control Panel", "icon": "settings",
             "data": ("settings", None)},
            {"label": "Sounds", "icon": "soundcp",
             "data": ("soundcp", None)},
            {"label": "Recycle Bin", "icon": _bin_icon(),
             "data": ("bin", None)},
        ]

    def on_resize(self):
        cw, ch = self.client_size()
        self.menubar.w = cw
        self.grid.w = cw - 4
        self.grid.h = ch - T.MENU_H - STATUS_H - 4

    def draw_client(self, d, img):
        cw, ch = self.client_size()
        T.sunken(d, 2, ch - STATUS_H, cw - 3, ch - 3, fill=T.FACE)
        d.text((8, ch - STATUS_H + 3), f"{len(self.grid.items)} object(s)",
               font=T.FONT, fill=T.TEXT)

    def _activate(self, item):
        kind, arg = item["data"]
        if kind == "filemgr":
            self.desk.shell.open_app("filemgr", arg)
        elif kind == "settings":
            self.desk.shell.open_app("settings")
        elif kind == "soundcp":
            self.desk.shell.open_app("soundcp")
        elif kind == "bin":
            self._open_bin()

    def _open_bin(self):
        from apps import recyclebin
        for w in self.desk.wm.windows:
            if isinstance(w, recyclebin.RecycleBin):
                self.desk.wm.activate(w)
                return
        self.desk.wm.add(recyclebin.RecycleBin(self.desk))

    def _file_menu(self):
        MI = W.MenuItem
        sel = self.grid.selected_items()
        return [
            MI("Open", enabled=bool(sel),
               action=lambda: sel and self._activate(sel[0])),
            W.sep(),
            MI("Close", action=self.request_close),
        ]

    def _help_menu(self):
        return [W.MenuItem("About My Computer…", icon="computer",
                           action=lambda: wm.msgbox(
                               self.desk, "About My Computer",
                               "kilix 95 — My Computer\n"
                               "Your drives, folders and the Recycle Bin,\n"
                               "all in one place.", icon="computer"))]
