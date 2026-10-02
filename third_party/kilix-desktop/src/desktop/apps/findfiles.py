"""kilix desktop — Find: All Files.

The classic Win95 search dialog: a name pattern (glob or substring), a
"Look in" root, an optional "Containing text" grep, and a results list that
grows as matches arrive. The tree is walked from a desk.tick_hook in bounded
chunks so a huge folder never blocks the event loop; results and depth are
capped.
"""
import fnmatch
import os
import time

import filedialog
import icons
import theme as T
import widgets as W
import wm

FX = 100                             # label→field column
M = 12                               # margin
Y_NAME, Y_LOOK, Y_TEXT, Y_BTN = 10, 38, 66, 94
LIST_Y = 124
STATUS_H = 20

CAP = 500                            # max results
MAX_DEPTH = 16                       # subfolder levels to descend
DIRS_PER_TICK = 40                   # bounded work per loop pass
READ_CAP = 1 << 20                   # bytes scanned for "containing text"

# a folder-with-magnifier icon, registered at import (icons.py is shared)
if "find" not in icons.ICONS:
    def _find(p):
        p.rect(1, 3, 4, 4, fill=icons.Y, outline=icons.K)      # folder tab
        p.rect(1, 4, 8, 10, fill=icons.Y, outline=icons.K)     # folder body
        p.hline(2, 7, 5, icons.W)
        p.rect(9, 7, 13, 11, fill=icons.C, outline=icons.K)    # lens
        p.hline(10, 12, 8, icons.W)                            # glint
        p.rect(13, 11, 15, 13, fill=icons.K)                   # handle
        p.px(14, 12, icons.K)
    icons.ICONS["find"] = _find


class FindFiles(wm.Window):
    def __init__(self, desk, arg=None):
        super().__init__(desk, "Find: All Files", 500, 380, icon="find",
                         on_close=self._teardown)
        self.min_w, self.min_h = 380, 260
        cw, ch = self.client_size()
        start = os.path.abspath(os.path.expanduser(arg or "~"))
        if not os.path.isdir(start):
            start = os.path.expanduser("~")

        self.add(W.Label(M, Y_NAME + 3, "Named:"))
        self.f_name = self.add(W.TextField(FX, Y_NAME, cw - FX - M,
                                           on_enter=lambda *_: self._find_now()))
        self.add(W.Label(M, Y_LOOK + 3, "Look in:"))
        self.f_look = self.add(W.TextField(FX, Y_LOOK, cw - FX - M - 70, start))
        self.b_browse = self.add(W.Button(cw - M - 64, Y_LOOK, 64, 21,
                                          "Browse…", cb=self._browse))
        self.add(W.Label(M, Y_TEXT + 3, "Containing text:"))
        self.f_text = self.add(W.TextField(FX, Y_TEXT, cw - FX - M,
                                           on_enter=lambda *_: self._find_now()))

        self.b_find = self.add(W.Button(M, Y_BTN, 78, 23, "Find Now",
                                        cb=self._find_now, default=True))
        self.b_stop = self.add(W.Button(M + 84, Y_BTN, 60, 23, "Stop",
                                        cb=self._stop))
        self.b_new = self.add(W.Button(M + 150, Y_BTN, 88, 23, "New Search",
                                       cb=self._new_search))
        self.results = self.add(W.ListBox(
            M, LIST_Y, cw - 2 * M, ch - LIST_Y - STATUS_H - 6,
            on_activate=self._open))

        self._scanning = False
        self._searched = False
        self._stack = []
        self._rows = []
        self._count = 0
        self._name_ok = None
        self._needle = None
        desk.tick_hooks.append(self._tick)
        self._sync()
        self.set_focus(self.f_name)

    # ── layout / chrome ──────────────────────────────────────────────────────
    def on_resize(self):
        cw, ch = self.client_size()
        self.f_name.w = cw - FX - M
        self.f_text.w = cw - FX - M
        self.f_look.w = cw - FX - M - 70
        self.b_browse.x = cw - M - 64
        self.results.w = cw - 2 * M
        self.results.h = ch - LIST_Y - STATUS_H - 6

    def draw_client(self, d, img):
        cw, ch = self.client_size()
        if self._scanning:
            msg = f"Searching…   {self._count} found"
        elif self._searched:
            cap = " (limit reached)" if self._count >= CAP else ""
            msg = f"{self._count} file(s) found{cap}"
        else:
            msg = "Ready"
        T.sunken(d, 2, ch - STATUS_H, cw - 3, ch - 3, fill=T.FACE)
        d.text((8, ch - STATUS_H + 3), msg, font=T.FONT, fill=T.TEXT)

    # ── search control ───────────────────────────────────────────────────────
    def _find_now(self):
        root = os.path.abspath(os.path.expanduser(self.f_look.text.strip()
                                                  or "~"))
        if not os.path.isdir(root):
            wm.msgbox(self.desk, "Find", f"'{root}' is not a folder.",
                      icon="error")
            return
        self._name_ok = _name_matcher(self.f_name.text)
        needle = self.f_text.text
        self._needle = needle.lower() if needle.strip() else None
        self._stack = [(root, 0)]
        self._rows = []
        self._count = 0
        self._scanning = True
        self._searched = True
        self.results.set_items([])
        self._sync()

    def _stop(self):
        self._scanning = False
        self._stack = []
        self._sync()

    def _new_search(self):
        self._scanning = False
        self._stack = []
        self._searched = False
        self._rows = []
        self._count = 0
        self.f_name.set("")
        self.f_text.set("")
        self.results.set_items([])
        self._sync()
        self.set_focus(self.f_name)

    def _sync(self):
        self.b_find.enabled = not self._scanning
        self.b_stop.enabled = self._scanning
        self.invalidate()

    def _browse(self):
        filedialog.pick_folder(self.desk, "Look In",
                               lambda p: p and self.f_look.set(p),
                               start=self.f_look.text or None)

    # ── the bounded walk (one chunk per loop pass) ───────────────────────────
    def _tick(self, now):
        if not self._scanning:
            return
        if self not in self.desk.wm.windows:      # window gone mid-scan
            self._scanning = False
            return
        new = []
        n = 0
        while self._stack and n < DIRS_PER_TICK and self._count < CAP:
            path, depth = self._stack.pop()
            n += 1
            try:
                entries = sorted(os.scandir(path), key=lambda e: e.name.lower())
            except OSError:
                continue
            for e in entries:
                try:
                    isdir = e.is_dir(follow_symlinks=False)
                except OSError:
                    isdir = False
                if isdir and depth < MAX_DEPTH:
                    self._stack.append((e.path, depth + 1))
                if self._count >= CAP:
                    break
                if self._match(e, isdir):
                    new.append(_row(e, isdir))
                    self._count += 1
        if new:
            self._rows.extend(new)
            self.results.set_items(self._rows, keep_sel=True)
        if not self._stack or self._count >= CAP:
            self._scanning = False
            self._sync()
        else:
            self.invalidate()

    def _match(self, entry, isdir):
        if not self._name_ok(entry.name):
            return False
        if self._needle is not None:
            return not isdir and _has_text(entry.path, self._needle)
        return True

    def _open(self, item):
        self.desk.shell.open_path(item[2])

    def _teardown(self):
        self._scanning = False
        if self._tick in self.desk.tick_hooks:
            self.desk.tick_hooks.remove(self._tick)


def _name_matcher(pat):
    pat = pat.strip()
    if not pat:
        return lambda name: True
    low = pat.lower()
    if any(c in pat for c in "*?["):
        return lambda name: fnmatch.fnmatch(name.lower(), low)
    return lambda name: low in name.lower()


def _has_text(path, needle_lower):
    try:
        with open(path, "rb") as f:
            chunk = f.read(READ_CAP)
    except OSError:
        return False
    if b"\0" in chunk:                            # skip binaries
        return False
    return needle_lower in chunk.decode("utf-8", "ignore").lower()


def _row(entry, isdir):
    p = entry.path
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        st = None
    size = "" if isdir else (_human(st.st_size) if st else "")
    kind = ("Folder" if isdir else
            "Launcher" if p.endswith(".desktop") else _ext_kind(p))
    when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            if st else "")
    text = "    ".join(x for x in (entry.name, os.path.dirname(p), size,
                                   kind, when) if x)
    return (icons.for_path(p, isdir), text, p)


def _ext_kind(path):
    ext = os.path.splitext(path)[1].lstrip(".").upper()
    return f"{ext} File" if ext else "File"


def _human(n):
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
