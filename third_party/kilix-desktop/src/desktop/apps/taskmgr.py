"""kilix desktop — Task Manager (Win95 "Close Program" list).

A live list of the running application windows: pick one and End Task
(request_close), Switch To (activate + restore), or start a New Task via the
Run dialog. The list re-syncs from a tick hook as windows come and go.
"""
import icons
import theme as T
import widgets as W
import wm


def _taskmgr(p):
    P = icons                                       # palette lives on the module
    p.rect(5, 1, 14, 10, fill=P.S, outline=P.K)     # back window
    p.rect(5, 1, 14, 2, fill=P.DB)                  # its (inactive) title bar
    p.rect(1, 5, 10, 14, fill=P.W, outline=P.K)     # front window, on top
    p.rect(1, 5, 10, 6, fill=P.DB)                  # active title bar
    for y in (9, 11, 13):                           # list rows
        p.hline(3, 8, y, P.G)


icons.ICONS.setdefault("taskmgr", _taskmgr)


class TaskManager(wm.Window):
    open_name = "taskmgr"
    label = "Task Manager"

    def __init__(self, desk, arg=None):
        super().__init__(desk, "Task Manager", 340, 380, icon="taskmgr")
        self.min_w, self.min_h = 260, 240
        cw, ch = self.client_size()
        self.list = self.add(W.ListBox(8, 24, cw - 16, ch - 96,
                                       on_select=self._select,
                                       on_activate=lambda it: self._switch()))
        self.b_end = self.add(W.Button(8, ch - 62, 100, 24, "End Task",
                                       cb=self._end, default=True))
        self.b_switch = self.add(W.Button(118, ch - 62, 100, 24, "Switch To",
                                          cb=self._switch))
        self.b_new = self.add(W.Button(228, ch - 62, 100, 24, "New Task…",
                                       cb=self._new))
        self._sel = None                            # selected window, tracked
        self._sig = None
        self._refresh()
        self.set_focus(self.list)
        self.on_close = self._untick
        desk.tick_hooks.append(self._tick)

    # ── the running-window model ─────────────────────────────────────────────
    def _apps(self):
        return [w for w in self.desk.wm.windows
                if w is not self and not w.modal]

    def _signature(self, apps):
        return tuple((id(w), w.title, w.icon, w.minimized) for w in apps)

    def _refresh(self):
        apps = self._apps()
        self._sig = self._signature(apps)
        if self._sel not in apps:
            self._sel = None
        items = [(w.icon, w.title, w) for w in apps]
        self.list.set_items(items)
        for i, w in enumerate(apps):
            if w is self._sel:
                self.list.sel = i
        self._sync_buttons()
        self.invalidate()

    def _tick(self, now):
        if self._signature(self._apps()) != self._sig:
            self._refresh()

    def _untick(self):
        if self._tick in self.desk.tick_hooks:
            self.desk.tick_hooks.remove(self._tick)

    def _sync_buttons(self):
        has = self._sel is not None
        for b in (self.b_end, self.b_switch):
            if b.enabled != has:
                b.enabled = has
                b.invalidate()

    # ── callbacks ────────────────────────────────────────────────────────────
    def _select(self, item):
        self._sel = item[2]
        self._sync_buttons()

    def _switch(self):
        if self._sel is not None:
            self.desk.wm.activate(self._sel)        # activate restores minimized

    def _end(self):
        w = self._sel
        if w is None:
            return
        w.request_close()
        self._refresh()

    def _new(self):
        self.desk.shell.run_dialog()

    # ── chrome ───────────────────────────────────────────────────────────────
    def on_resize(self):
        cw, ch = self.client_size()
        self.list.w, self.list.h = cw - 16, ch - 96
        for b in (self.b_end, self.b_switch, self.b_new):
            b.y = ch - 62

    def draw_client(self, d, img):
        cw, ch = self.client_size()
        d.text((8, 6), "Applications:", font=T.FONT, fill=T.TEXT)
        T.sunken(d, 2, ch - 20, cw - 3, ch - 3, fill=T.FACE)
        n = len(self.list.items)
        d.text((8, ch - 17), f"{n} task{'' if n == 1 else 's'} running",
               font=T.FONT, fill=T.TEXT)
