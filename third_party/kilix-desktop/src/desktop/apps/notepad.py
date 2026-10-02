"""kilix desktop — Notepad. A plain text editor over widgets.TextArea."""
import os

import filedialog
import theme as T
import widgets as W
import wm

_FILTERS_OPEN = [("Text Documents", "*.txt;*.md;*.log"), ("All Files", "*.*")]
_FILTERS_SAVE = [("Text Documents", "*.txt"), ("All Files", "*.*")]


class Notepad(wm.Window):
    def __init__(self, desk, path=None):
        super().__init__(desk, "Untitled - Notepad", 520, 380, icon="notepad")
        self.min_w, self.min_h = 260, 160
        self.path = None
        self.modified = False
        cw, ch = self.client_size()
        self.menubar = self.add(W.MenuBar(cw, [
            ("File", self._file_menu), ("Edit", self._edit_menu),
            ("Help", self._help_menu)]))
        self.ta = self.add(W.TextArea(2, T.MENU_H + 2, cw - 4,
                                      ch - T.MENU_H - 4))
        self.ta.on_change = self._changed
        self.set_focus(self.ta)
        if path:
            self._load(path)

    def on_resize(self):
        cw, ch = self.client_size()
        self.menubar.w = cw
        self.ta.w, self.ta.h = cw - 4, ch - T.MENU_H - 4

    def _retitle(self):
        name = os.path.basename(self.path) if self.path else "Untitled"
        star = "*" if self.modified else ""
        self.title = f"{star}{name} - Notepad"
        self.invalidate()

    def _changed(self):
        if not self.modified:
            self.modified = True
            self._retitle()

    # ── file plumbing ───────────────────────────────────────────────────────
    def _load(self, path):
        path = os.path.expanduser(path)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                self.ta.set_text(f.read())
        except OSError as e:
            wm.msgbox(self.desk, "Notepad", str(e), icon="error")
            return
        self.path = path
        self.modified = False
        self.desk.shell.add_recent(path)
        self._retitle()

    def _save(self, then=None, path=None):
        target = os.path.expanduser(path) if path else self.path
        if not target:
            return self._save_as(then)
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(self.ta.text())
        except OSError as e:
            wm.msgbox(self.desk, "Notepad", str(e), icon="error")
            return
        self.path = target
        self.modified = False
        self._retitle()
        if then:
            then()

    def _save_as(self, then=None):
        def do(path):
            if path:
                self._save(then, path=path)
        filedialog.save_file(self.desk, "Save As", do,
                             start=os.path.dirname(self.path) if self.path
                             else None, filters=_FILTERS_SAVE,
                             filename=os.path.basename(self.path)
                             if self.path else "")

    def _open(self):
        def go():
            filedialog.open_file(self.desk, "Open",
                                 lambda p: p and self._load(p),
                                 start=os.path.dirname(self.path)
                                 if self.path else None,
                                 filters=_FILTERS_OPEN)
        self._if_saved(go)

    def _new(self):
        def go():
            self.path = None
            self.ta.set_text("")
            self.modified = False
            self._retitle()
        self._if_saved(go)

    def _if_saved(self, then):
        if not self.modified:
            then()
            return

        def do(ans):
            if ans == "Yes":
                self._save(then)
            elif ans == "No":
                then()
        wm.msgbox(self.desk, "Notepad",
                  "The text has changed.\nSave the changes?",
                  icon="warn", buttons=("Yes", "No", "Cancel"), cb=do)

    def request_close(self):
        self._if_saved(self.close)

    # ── menus ───────────────────────────────────────────────────────────────
    def _file_menu(self):
        MI, sep = W.MenuItem, W.sep
        return [
            MI("New", action=self._new),
            MI("Open…", action=self._open),
            MI("Save", action=self._save),
            MI("Save As…", action=self._save_as),
            sep(),
            MI("Close", action=self.request_close),
        ]

    def _edit_menu(self):
        MI, sep = W.MenuItem, W.sep
        ta = self.ta

        def key(k, ctrl=True):
            return lambda: ta.on_key(W.Ev(kind="key", key=k, ctrl=ctrl))
        return [
            MI("Cut", action=key("x")),
            MI("Copy", action=key("c")),
            MI("Paste", action=key("v")),
            sep(),
            MI("Select All", action=key("a")),
        ]

    def _help_menu(self):
        return [W.MenuItem(
            "About Notepad…", icon="notepad",
            action=lambda: wm.msgbox(
                self.desk, "About Notepad",
                "kilix 95 Notepad\nCtrl+S save · Ctrl+O open · Ctrl+N new\n"
                "Copy lands on the system clipboard via OSC 52;\n"
                "paste with kilix's paste (Ctrl+Shift+V).",
                icon="notepad"))]

    def on_key(self, ev):
        if ev.ctrl and ev.key == "s":
            self._save()
            return True
        if ev.ctrl and ev.key == "o":
            self._open()
            return True
        if ev.ctrl and ev.key == "n":
            self._new()
            return True
        return super().on_key(ev)
