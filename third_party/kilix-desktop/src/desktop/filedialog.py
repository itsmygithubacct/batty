"""kilix desktop — the common Open/Save file dialog.

A modal window mirroring wm.msgbox/inputbox's modality + callback style:
open_file/save_file(desk, title, cb, ...) call cb(path) with the chosen
absolute path, or cb(None) on Cancel. Fixed-size, Win95 idiom.
"""
import fnmatch
import os
import stat

import icons
import theme as T
import widgets as W
import wm

_DEFAULT_FILTERS = [("All Files", "*.*")]


class FileDialog(wm.Window):
    def __init__(self, desk, title, cb, save=False, start=None, filters=None,
                 filename="", pick_dir=False):
        super().__init__(desk, title, 460, 330, icon="folder_open",
                         resizable=False, modal=True)
        self.cb = cb
        self.save = save
        self.pick_dir = pick_dir           # choose a folder instead of a file
        self.filters = ([("Folders", "")] if pick_dir
                        else (filters or _DEFAULT_FILTERS))
        self.cwd = os.path.expanduser("~")
        self._ancestors = []
        self._filled = False
        cw, ch = self.client_size()

        self.add(W.Label(10, 12, "Look in:"))
        self.lookin = self.add(W.Dropdown(58, 8, cw - 64 - 58, ["/"]))
        self.lookin.cb = lambda *_: self._nav(
            self._ancestors[self.lookin.index])
        self.add(W.Button(cw - 58, 8, 24, 22, icon="up", cb=self._up))
        self.add(W.Button(cw - 30, 8, 24, 22, icon="home",
                          cb=lambda: self._nav("~")))

        self.list = self.add(W.ListBox(10, 38, cw - 20, 200,
                                       on_activate=self._activate,
                                       on_select=self._select))

        self.add(W.Label(10, ch - 57, "Folder:" if pick_dir else "File name:"))
        self.name = self.add(W.TextField(80, ch - 60, cw - 80 - 95,
                                         os.path.basename(filename),
                                         on_enter=lambda _t: self._confirm()))
        ok = "Select" if pick_dir else ("Save" if save else "Open")
        self.add(W.Button(cw - 85, ch - 61, 75, 23, ok, cb=self._confirm,
                          default=True))

        self.add(W.Label(10, ch - 29, "Files of type:"))
        self.ftype = self.add(W.Dropdown(80, ch - 32, cw - 80 - 95,
                                         [f[0] for f in self.filters]))
        self.ftype.cb = lambda *_: self._fill()
        self.add(W.Button(cw - 85, ch - 33, 75, 23, "Cancel", cb=self._cancel))

        start = start or (os.path.dirname(filename) if filename else None)
        self._nav(start or "~")
        self.set_focus(self.name)
        self.request_close = self._cancel
        desk.wm.add(self)

    # ── navigation / listing ────────────────────────────────────────────────
    def _nav(self, path):
        path = os.path.abspath(os.path.expanduser(path or "~"))
        try:
            os.listdir(path)
        except OSError:
            if self._filled:                        # already showing a dir: stay put
                wm.msgbox(self.desk, self.title,
                          f"{os.path.basename(path) or path}\n"
                          "is not accessible.", icon="warn")
                return
            for cand in (os.path.expanduser("~"), "/"):   # opening: need a usable dir
                try:
                    os.listdir(cand)
                except OSError:
                    continue
                path = cand
                break
            else:
                return
        self.cwd = path
        self._fill()

    def _up(self):
        self._nav(os.path.dirname(self.cwd))

    def _match(self, name):
        for p in self.filters[self.ftype.index][1].split(";"):
            p = p.strip()
            if p in ("", "*", "*.*") or fnmatch.fnmatch(name.lower(),
                                                        p.lower()):
                return True
        return False

    def _fill(self):
        try:
            names = os.listdir(self.cwd)
        except OSError:
            names = []
        dirs, files = [], []
        for n in sorted(names, key=str.lower):
            if n.startswith("."):
                continue
            full = os.path.join(self.cwd, n)
            if os.path.isdir(full):
                dirs.append((n, full))
            elif self._match(n):
                files.append((n, full))
        items = [("folder", n, ("dir", f)) for n, f in dirs]
        if not self.pick_dir:                       # folder mode: dirs only
            items += [(icons.for_path(f), n, ("file", f)) for n, f in files]
        self.list.set_items(items)
        self._filled = True
        self._update_lookin()

    def _update_lookin(self):
        chain, p = [], self.cwd
        while True:
            chain.append(p)
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
        self._ancestors = list(reversed(chain))
        self.lookin.options = [os.path.basename(a) or a
                               for a in self._ancestors]
        self.lookin.index = len(self._ancestors) - 1
        self.lookin.invalidate()

    # ── selection ───────────────────────────────────────────────────────────
    def _select(self, item):
        kind, path = item[2]
        if kind == "file" or (self.pick_dir and kind == "dir"):
            self.name.set(os.path.basename(path))

    def _activate(self, item):
        kind, path = item[2]
        if kind == "dir":
            self._nav(path)
        else:
            self.name.set(os.path.basename(path))
            self._confirm()

    # ── confirm / cancel ────────────────────────────────────────────────────
    def _confirm(self):
        text = self.name.text.strip()
        if self.pick_dir:
            # Select chooses the current folder, or a typed/selected subfolder
            p = os.path.abspath(os.path.join(
                self.cwd, os.path.expanduser(text))) if text else self.cwd
            if os.path.isdir(p):
                self._done(p)
            else:
                wm.msgbox(self.desk, self.title,
                          f"{text}\nis not a folder.", icon="warn")
            return
        if not text:
            return
        p = os.path.expanduser(text)
        if not os.path.isabs(p):
            p = os.path.join(self.cwd, p)
        p = os.path.abspath(p)
        if os.path.isdir(p):
            self.name.set("")
            self._nav(p)
            return
        if self.save:
            if os.path.exists(p):
                try:
                    st = os.stat(p)
                except OSError as e:
                    wm.msgbox(self.desk, self.title, str(e), icon="warn")
                    return
                if not stat.S_ISREG(st.st_mode):
                    wm.msgbox(self.desk, self.title,
                              f"{os.path.basename(p) or p}\n"
                              "Cannot replace this special file.",
                              icon="warn")
                    return

                def ans(a, q=p):
                    if a == "Yes":
                        self._done(q)
                wm.msgbox(self.desk, self.title,
                          f"{os.path.basename(p)} already exists.\n"
                          "Do you want to replace it?", icon="question",
                          buttons=("Yes", "No"), cb=ans)
            else:
                self._done(p)
        elif os.path.isfile(p):
            self._done(p)
        else:
            wm.msgbox(self.desk, self.title,
                      f"{os.path.basename(p)}\nFile not found. Check the "
                      "file name and try again.", icon="warn")

    def _done(self, path):
        self.close()
        if self.cb:
            self.cb(os.path.abspath(path))

    def _cancel(self):
        self.close()
        if self.cb:
            self.cb(None)

    def on_key(self, ev):
        if ev.key == "Backspace" and self.focus is self.list:
            self._up()
            return True
        return super().on_key(ev)


def open_file(desk, title, cb, start=None, filters=None, filename=""):
    return FileDialog(desk, title, cb, save=False, start=start,
                      filters=filters, filename=filename)


def save_file(desk, title, cb, start=None, filters=None, filename=""):
    return FileDialog(desk, title, cb, save=True, start=start,
                      filters=filters, filename=filename)


def pick_folder(desk, title, cb, start=None):
    """Choose a directory. cb(path) with the chosen folder, or cb(None)."""
    return FileDialog(desk, title, cb, start=start, pick_dir=True)
