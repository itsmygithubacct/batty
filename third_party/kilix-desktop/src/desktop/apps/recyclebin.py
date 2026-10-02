"""kilix desktop — the Recycle Bin (Explorer's trash view over recycle.py).

Lists deleted items (name, original location, date deleted, size); File and
context menus Restore, Delete (purge one), or Empty Recycle Bin — each
confirmed and followed by a refresh. Empty view shows the classic notice.
"""
import os
import time

import recycle
import theme as T
import widgets as W
import wm

STATUS_H = 20
_COLS = "{name}    {loc}    {when}    {size}"


class RecycleBin(wm.Window):
    def __init__(self, desk, arg=None):
        super().__init__(desk, "Recycle Bin", 520, 340, icon="recyclebin_full")
        self.min_w, self.min_h = 320, 200
        cw, ch = self.client_size()
        self.menubar = self.add(W.MenuBar(cw, [
            ("File", self._file_menu), ("Edit", self._edit_menu),
            ("Help", self._help_menu)]))
        self.lb = self.add(W.ListBox(
            2, T.MENU_H + 2, cw - 4, ch - T.MENU_H - STATUS_H - 4,
            on_activate=self._activate, on_context=self._context))
        self.items = []
        self.set_focus(self.lb)
        self.refresh()

    # ── layout / chrome ─────────────────────────────────────────────────────
    def on_resize(self):
        cw, ch = self.client_size()
        self.menubar.w = cw
        self.lb.w = cw - 4
        self.lb.h = ch - T.MENU_H - STATUS_H - 4

    def draw_client(self, d, img):
        cw, ch = self.client_size()
        T.sunken(d, 2, ch - STATUS_H, cw - 3, ch - 3, fill=T.FACE)
        n = len(self.items)
        msg = "The Recycle Bin is empty." if not n else f"{n} object(s)"
        d.text((8, ch - STATUS_H + 3), msg, font=T.FONT, fill=T.TEXT)

    # ── data ────────────────────────────────────────────────────────────────
    def refresh(self):
        self.items = recycle.items()
        rows = []
        for it in self.items:
            icon = "folder" if it["is_dir"] else "doc"
            loc = os.path.dirname(it["orig"]) or "?"
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(it["when"]))
            rows.append((icon, _COLS.format(
                name=it["name"], loc=loc, when=when,
                size=_human(it["size"])), it))
        self.lb.set_items(rows)
        self.invalidate()

    def _selected(self):
        return self.lb.items[self.lb.sel][2] if self.lb.sel >= 0 else None

    def _activate(self, item):
        self._properties(item[2])

    # ── menus ───────────────────────────────────────────────────────────────
    def _file_menu(self):
        MI, sep = W.MenuItem, W.sep
        one = self._selected()
        return [
            MI("Restore", enabled=one is not None,
               action=lambda: self._restore_item(one)),
            MI("Delete", enabled=one is not None,
               action=lambda: self._purge(one)),
            sep(),
            MI("Empty Recycle Bin", icon="recyclebin_full",
               enabled=bool(self.items), action=self._empty),
            sep(),
            MI("Properties…", enabled=one is not None,
               action=lambda: self._properties(one)),
            sep(),
            MI("Close", action=self.request_close),
        ]

    def _edit_menu(self):
        MI = W.MenuItem
        one = self._selected()
        return [
            MI("Restore", enabled=one is not None,
               action=lambda: self._restore_item(one)),
            MI("Delete", enabled=one is not None,
               action=lambda: self._purge(one)),
        ]

    def _help_menu(self):
        return [W.MenuItem("About the Recycle Bin…", icon="recyclebin_full",
                           action=lambda: wm.msgbox(
                               self.desk, "About the Recycle Bin",
                               "kilix 95 Recycle Bin\n"
                               "Restore deleted files to where they came\n"
                               "from, or empty the bin to reclaim space.",
                               icon="recyclebin_full"))]

    def _context(self, item, ev):
        gx, gy = self.client_origin()
        gx, gy = gx + ev.x, gy + ev.y
        MI, sep = W.MenuItem, W.sep
        if item is None:
            items = [MI("Empty Recycle Bin", icon="recyclebin_full",
                        enabled=bool(self.items), action=self._empty),
                     MI("Refresh", action=self.refresh)]
        else:
            data = item[2]
            items = [MI("Restore", action=lambda: self._restore_item(data)),
                     MI("Delete", action=lambda: self._purge(data)),
                     sep(),
                     MI("Properties…",
                        action=lambda: self._properties(data))]
        self.desk.menus.open(items, gx, gy)

    # ── operations ──────────────────────────────────────────────────────────
    def _restore_item(self, it):
        if not it:
            return
        try:
            dest = recycle.restore(it["token"])
        except (KeyError, OSError) as e:
            wm.msgbox(self.desk, "Restore", str(e), icon="error")
            dest = None
        if dest:
            self.desk.shell.dir_changed(os.path.dirname(dest))
        self.refresh()

    def _purge(self, it):
        if not it:
            return

        def do(ans):
            if ans == "Yes":
                recycle.purge(it["token"])
                self.refresh()
        wm.msgbox(self.desk, "Confirm Delete",
                  f"Permanently delete {it['name']}?\nThis cannot be undone.",
                  icon="warn", buttons=("Yes", "No"), default=1, cb=do)

    def _empty(self):
        if not self.items:
            return

        def do(ans):
            if ans == "Yes":
                recycle.empty()
                self.desk.play_sound("recycle_empty")
                self.desk.shell.refresh()
                self.refresh()
        wm.msgbox(self.desk, "Empty Recycle Bin",
                  f"Permanently delete all {len(self.items)} item(s)?\n"
                  "This cannot be undone.",
                  icon="warn", buttons=("Yes", "No"), default=1, cb=do)

    def _properties(self, it):
        if not it:
            return
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(it["when"]))
        kind = "Folder" if it["is_dir"] else "File"
        wm.msgbox(self.desk, f"{it['name']} Properties",
                  f"Type:      {kind}\n"
                  f"Origin:    {it['orig']}\n"
                  f"Deleted:   {when}\n"
                  f"Size:      {_human(it['size'])}",
                  icon="recyclebin_full", win_icon="recyclebin_full")

    # ── keys ────────────────────────────────────────────────────────────────
    def on_key(self, ev):
        if self.focus is self.lb:
            one = self._selected()
            if ev.key == "F5":
                self.refresh()
                return True
            if ev.key == "Delete" and one:
                self._purge(one)
                return True
            if ev.alt and ev.key == "Enter" and one:
                self._properties(one)
                return True
        return super().on_key(ev)


def _human(n):
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
