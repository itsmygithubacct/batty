"""kilix desktop — window manager.

Windows are PIL surfaces composited by the Desk; each has Win95 chrome
(4px 3D frame, navy title bar, minimize/maximize/close buttons, optional
menu bar as a client widget) and a flat list of client widgets. The WM owns
z-order, focus, move/resize drags and modality; dialogs are ordinary
non-resizable windows created by the msgbox()/inputbox() helpers.
"""
from PIL import Image

import icons
import theme as T
import widgets as W


class Window:
    _seq = 0                          # stable launch order for the taskbar

    def __init__(self, desk, title, w, h, x=None, y=None, icon="exe",
                 resizable=True, modal=False, on_close=None, chromeless=False):
        Window._seq += 1
        self.seq = Window._seq
        self.desk = desk
        self.title = title
        self.icon = icon
        # chromeless windows (e.g. the skinned media player) have no frame or
        # title bar — the whole surface is client area, drawn edge to edge and
        # composited with a transparency mask so the app's skin sits directly
        # on the desktop, Winamp-style.
        self.chromeless = chromeless
        if chromeless:
            self.w, self.h = max(w, 1), max(h, 1)
            resizable = False
        else:
            self.w, self.h = max(w, 120), max(h, T.TITLE_H + 2 * T.BORDER + 10)
        sw, sh = desk.size()
        n = len(desk.wm.windows)
        off = (n * 22) % 220          # cascade, wrapping like Win95
        if x is None:
            x = min((sw - self.w) // 2 + off, sw - self.w)
        if y is None:
            y = min((sh - T.TASKBAR_H - self.h) // 2 + off,
                    sh - T.TASKBAR_H - self.h)
        self.x = max(0, x)
        self.y = max(0, y)
        self.resizable = resizable
        self.modal = modal
        self.on_close = on_close
        self.widgets = []
        self.focus = None
        self.caret_on = True
        self.minimized = False
        self.maximized = False
        self._restore = None
        self.min_w, self.min_h = 140, 80
        self.surface = None
        self.compose_mask = None      # optional L-image: per-pixel opacity
        self.dirty = True
        self._capture = None          # widget holding the mouse until release
        self._capture_btn = 0         # button that grabbed it (only it releases)

    # ── geometry ────────────────────────────────────────────────────────────
    def client_size(self):
        if self.chromeless:
            return self.w, self.h
        return (self.w - 2 * T.BORDER,
                self.h - 2 * T.BORDER - T.TITLE_H)

    def client_origin(self):
        if self.chromeless:
            return self.x, self.y
        return self.x + T.BORDER, self.y + T.BORDER + T.TITLE_H

    def hit_test(self, gx, gy):
        """WM hit test — overridable so a chromeless window can pass clicks
        through its transparent pixels to whatever is behind it."""
        return self.hit(gx, gy)

    def hit(self, gx, gy):
        return (self.x <= gx < self.x + self.w
                and self.y <= gy < self.y + self.h)

    def tooltip_at(self, gx, gy):
        """Hover tip: the (possibly-ellipsized) title over the title bar."""
        if self.chromeless:
            return None
        lx, ly = gx - self.x, gy - self.y
        if (T.BORDER <= ly < T.BORDER + T.TITLE_H
                and T.BORDER <= lx < self.w - T.BORDER):
            return self.title
        return None

    # ── widget management ───────────────────────────────────────────────────
    def add(self, wdg):
        wdg.window = self
        self.widgets.append(wdg)
        if self.focus is None and wdg.focusable and wdg.visible:
            self.focus = wdg
        return wdg

    def remove(self, wdg):
        if wdg in self.widgets:
            self.widgets.remove(wdg)
        if self.focus is wdg:
            self.focus = None

    def set_focus(self, wdg):
        if self.focus is not wdg:
            old = self.focus
            self.focus = wdg
            self.caret_on = True
            if old:
                old.on_focus(False)
            if wdg:
                wdg.on_focus(True)
            self.invalidate()

    def invalidate(self):
        self.dirty = True
        self.desk.dirty = True

    def close(self):
        self.desk.wm.close(self)

    # ── chrome ──────────────────────────────────────────────────────────────
    def _sysbuttons(self):
        """[(kind, x0, y0, x1, y1)] in window coords, right to left."""
        out = []
        bx = self.x is not None  # noqa: readable anchor
        x = self.w - T.BORDER - 16
        y0 = T.BORDER + 2
        for kind in ("close", "max", "min"):
            if kind != "close" and not self.resizable:
                continue
            out.append((kind, x, y0, x + 15, y0 + 13))
            x -= 16 if kind == "close" else 16
            if kind == "close":
                x -= 2                # gap between close and max, like 95
        return out

    def render(self):
        if self.surface is None or self.surface.size != (self.w, self.h):
            self.surface = Image.new("RGB", (self.w, self.h), T.FACE)
            self.dirty = True
        if not self.dirty:
            return self.surface
        img = self.surface
        d = W.drawer(img)
        if self.chromeless:
            # no frame/title bar: widgets own the whole surface
            self.draw_client(d, img)
            for wdg in self.widgets:
                if wdg.visible:
                    wdg.draw(d, img)
            self.dirty = False
            return img
        d.rectangle([0, 0, self.w - 1, self.h - 1], fill=T.FACE)
        T.frame(d, 0, 0, self.w - 1, self.h - 1)
        active = self.desk.wm.active is self
        tx0, ty0 = T.BORDER, T.BORDER
        tx1, ty1 = self.w - T.BORDER - 1, T.BORDER + T.TITLE_H - 3
        T.titlebar(d, tx0, ty0, tx1, ty1, active=active)
        icons.paint(img, self.icon, tx0 + 2, ty0, 16)
        label = T.ellipsize(T.BOLD, self.title, self.w - 90)
        d.text((tx0 + 22, ty0 + 1), label, font=T.BOLD,
               fill=T.TITLE_A_TX if active else T.TITLE_I_TX)
        for kind, x0, y0, x1, y1 in self._sysbuttons():
            T.raised(d, x0, y0, x1, y1)
            cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
            if kind == "close":
                for i in range(6):
                    d.point((cx - 3 + i, cy - 3 + i), fill=T.TEXT)
                    d.point((cx - 2 + i, cy - 3 + i), fill=T.TEXT)
                    d.point((cx - 3 + i, cy + 2 - i), fill=T.TEXT)
                    d.point((cx - 2 + i, cy + 2 - i), fill=T.TEXT)
            elif kind == "max":
                if self.maximized:    # restore glyph: two little frames
                    d.rectangle([cx - 1, cy - 4, cx + 4, cy], outline=T.TEXT)
                    d.rectangle([cx - 4, cy - 1, cx + 1, cy + 3],
                                fill=T.FACE, outline=T.TEXT)
                    d.line([(cx - 4, cy), (cx + 1, cy)], fill=T.TEXT)
                else:
                    d.rectangle([cx - 4, cy - 4, cx + 4, cy + 3],
                                outline=T.TEXT)
                    d.line([(cx - 3, cy - 3), (cx + 3, cy - 3)], fill=T.TEXT)
            elif kind == "min":
                d.rectangle([cx - 4, cy + 2, cx + 1, cy + 3], fill=T.TEXT)
        # client area
        cw, ch = self.client_size()
        client = Image.new("RGB", (max(1, cw), max(1, ch)), T.FACE)
        cd = W.drawer(client)
        self.draw_client(cd, client)
        for wdg in self.widgets:
            if wdg.visible:
                wdg.draw(cd, client)
        img.paste(client, (T.BORDER, T.BORDER + T.TITLE_H))
        if self.resizable and not self.maximized:
            # size grip dashes in the corner
            gx, gy = self.w - T.BORDER - 2, self.h - T.BORDER - 2
            for i in range(3):
                d.line([(gx - 2 - i * 4, gy), (gx, gy - 2 - i * 4)],
                       fill=T.SHADOW)
                d.line([(gx - 3 - i * 4, gy), (gx, gy - 3 - i * 4)],
                       fill=T.LIGHT)
        self.dirty = False
        return img

    def draw_client(self, d, img):
        """Hook for subclasses: paint below the widgets."""

    # ── input ───────────────────────────────────────────────────────────────
    def sys_action(self, kind):
        wm = self.desk.wm
        if kind == "close":
            self.request_close()
        elif kind == "min":
            wm.minimize(self)
        elif kind == "max":
            wm.toggle_maximize(self)

    def request_close(self):
        """Apps override to confirm unsaved changes; default closes."""
        self.close()

    def _edge_at(self, lx, ly):
        if not self.resizable or self.maximized:
            return ""
        e = ""
        m = T.BORDER + 2
        if ly < m:
            e += "n"
        elif ly >= self.h - m:
            e += "s"
        if lx < m:
            e += "w"
        elif lx >= self.w - m:
            e += "e"
        return e

    def on_mouse(self, gev):
        """gev has global coords; window translates. Returns True if used."""
        lev = gev.at(self.x, self.y)
        lx, ly = lev.x, lev.y
        wm = self.desk.wm
        if self.chromeless:
            # a resizable chromeless window (e.g. an XPane app) gets an edge
            # resize grip and double-click-the-top-edge to maximize/restore
            # (the app has no working window manager of its own); anything else
            # goes straight to the app's skin.
            if (self.resizable and gev.press and gev.btn == 1
                    and self._capture is None):
                g = getattr(self, "_GRIP", T.BORDER + 2)
                edge = "" if self.maximized else self._edge_at(lx, ly)
                if gev.clicks == 2 and (edge or ly < g):
                    wm.toggle_maximize(self)
                    return True
                if edge:
                    wm.begin_drag(self, edge, gev.x, gev.y)
                    return True
            # no chrome: everything goes to the client widgets (the app's own
            # skin draws its titlebar / buttons and handles dragging)
            cev = lev
            target = self._capture
            if target is None:
                for wdg in reversed(self.widgets):
                    if wdg.visible and wdg.enabled and wdg.hit(cev.x, cev.y):
                        target = wdg
                        break
                if gev.press and target is not None:
                    self._capture = target
                    self._capture_btn = gev.btn
                    if target.focusable:
                        self.set_focus(target)
            if (not gev.press and not gev.move and not gev.wheel
                    and gev.btn == self._capture_btn):
                self._capture = None
            if target is not None:
                target.on_mouse(cev)
            return True
        if gev.press:
            edge = self._edge_at(lx, ly)
            in_title = (T.BORDER <= ly < T.BORDER + T.TITLE_H - 2
                        and T.BORDER <= lx < self.w - T.BORDER)
            for kind, x0, y0, x1, y1 in self._sysbuttons():
                if x0 <= lx <= x1 and y0 <= ly <= y1 and gev.btn == 1:
                    self.sys_action(kind)
                    return True
            if in_title and gev.btn == 1:
                if lx < T.BORDER + 20:                     # the title icon
                    self._system_menu()
                    return True
                if gev.clicks == 2 and self.resizable:
                    wm.toggle_maximize(self)
                    return True
                if not self.maximized:
                    wm.begin_drag(self, "move", gev.x, gev.y)
                return True
            if edge and gev.btn == 1:
                wm.begin_drag(self, edge, gev.x, gev.y)
                return True
        # client dispatch (per-widget capture from press until release; a
        # press always re-hit-tests so a stale capture can't steal clicks)
        cev = lev.at(T.BORDER, T.BORDER + T.TITLE_H)
        # a press with no held capture re-hit-tests so a stale capture can't
        # steal the click; a 2nd button pressed mid-drag keeps the owner so its
        # release can't abort an in-progress widget drag (selection, scrollbar)
        held = (gev.press and self._capture is not None
                and gev.btn != self._capture_btn)
        target = self._capture if (held or not gev.press) else None
        if target is None:
            for wdg in reversed(self.widgets):
                if wdg.visible and wdg.enabled and wdg.hit(cev.x, cev.y):
                    target = wdg
                    break
        if gev.press and not held:
            self._capture = target
            self._capture_btn = gev.btn
            if target is not None and target.focusable:
                self.set_focus(target)
        if (not gev.press and not gev.move and not gev.wheel
                and gev.btn == self._capture_btn):
            self._capture = None
        if target is not None:
            target.on_mouse(cev)
            return True
        return True                    # clicks on the frame still belong to us

    def _system_menu(self, gx=None, gy=None):
        # anchored at the title icon by default, or at (gx, gy) for the taskbar
        wm = self.desk.wm

        def restore():
            if self.minimized:
                wm.activate(self)
            else:
                wm.toggle_maximize(self)
        items = [
            W.MenuItem("Restore", action=restore,
                       enabled=self.minimized or self.maximized),
            W.MenuItem("Minimize", action=lambda: wm.minimize(self),
                       enabled=not self.modal and not self.minimized),
            W.MenuItem("Maximize", action=lambda: wm.toggle_maximize(self),
                       enabled=self.resizable and not self.maximized),
            W.sep(),
            W.MenuItem("Close", action=self.request_close),
        ]
        if gx is None:
            gx, gy = self.x + T.BORDER, self.y + T.BORDER + T.TITLE_H - 2
        self.desk.menus.open(items, gx, gy)

    def on_key(self, ev):
        if self.focus and self.focus.on_key(ev):
            return True
        if ev.key == "Tab" and not ev.ctrl:
            foci = [w for w in self.widgets if w.focusable and w.visible
                    and w.enabled]
            if foci:
                try:
                    i = foci.index(self.focus)
                except ValueError:
                    i = -1
                self.set_focus(foci[(i + (-1 if ev.shift else 1))
                                    % len(foci)])
            return True
        if ev.key == "Escape" and self.modal:
            self.request_close()
            return True
        return False


class WM:
    def __init__(self, desk):
        self.desk = desk
        self.windows = []             # bottom → top
        self.active = None
        self.drag = None              # (win, mode, grab_x, grab_y, orig_rect)

    # ── lifecycle ───────────────────────────────────────────────────────────
    def _is_app(self, win):
        # tasteful: blip only for ordinary app windows — not dialogs, tray
        # flyouts or chromeless skins (they get their own cues or none)
        return (not win.modal and not win.chromeless
                and not getattr(win, "_no_taskbar", False))

    def add(self, win):
        if self._is_app(win):
            self.desk.play_sound("open")
        self.windows.append(win)
        self.activate(win)
        # a modal dialog raised mid-construction (e.g. an app __init__ that
        # msgbox'es a load error) is added before the app window; keep it on
        # top so it stays visible and dismissable.
        m = self.modal_top()
        if m is not None and m is not win:
            self.activate(m)
        return win

    def close(self, win):
        if win in self.windows and self._is_app(win):
            self.desk.play_sound("close")
        if win in self.windows:
            self.windows.remove(win)
        if win.on_close:
            win.on_close()
        if self.active is win:
            self.active = None
            for w in reversed(self.windows):
                if not w.minimized:
                    self.activate(w)
                    break
        self.desk.dirty = True

    def activate(self, win):
        if win.minimized:
            win.minimized = False
        if self.windows and self.windows[-1] is not win:
            self.windows.remove(win)
            self.windows.append(win)
        old, self.active = self.active, win
        if old and old is not win:
            old.dirty = True
            old.caret_on = False      # only the active window blinks a caret
        win.caret_on = True
        win.dirty = True
        self.desk.dirty = True

    def minimize(self, win):
        self.desk.play_sound("minimize")
        win.minimized = True
        if self.active is win:
            self.active = None
            for w in reversed(self.windows):
                if not w.minimized:
                    self.activate(w)
                    break
        self.desk.dirty = True

    def toggle_maximize(self, win):
        if not win.resizable:
            return
        if win.maximized:
            win.x, win.y, win.w, win.h = win._restore
            win.maximized = False
            self.desk.play_sound("restore")
        else:
            self.desk.play_sound("maximize")
            win._restore = (win.x, win.y, win.w, win.h)
            sw, sh = self.desk.size()
            win.x = win.y = 0
            win.w, win.h = sw, sh - T.TASKBAR_H
            win.maximized = True
        win.surface = None
        win.on_resize()               # apps relayout their widgets
        self.activate(win)

    def modal_top(self):
        for w in reversed(self.windows):
            if w.modal and not w.minimized:
                return w
        return None

    # ── drags ───────────────────────────────────────────────────────────────
    def begin_drag(self, win, mode, gx, gy):
        self.drag = (win, mode, gx, gy, (win.x, win.y, win.w, win.h))

    def drag_motion(self, gev):
        win, mode, gx, gy, (ox, oy, ow, oh) = self.drag
        dx, dy = gev.x - gx, gev.y - gy
        sw, sh = self.desk.size()
        if mode == "move":
            win.x = ox + dx
            win.y = max(0, min(oy + dy, sh - T.TASKBAR_H - 10))
        else:
            x, y, w, h = ox, oy, ow, oh
            if "e" in mode:
                w = max(win.min_w, ow + dx)
            if "s" in mode:
                h = max(win.min_h, oh + dy)
            if "w" in mode:
                w = max(win.min_w, ow - dx)
                x = ox + ow - w
            if "n" in mode:
                h = max(win.min_h, oh - dy)
                y = max(0, oy + oh - h)
                h = oy + oh - y
            if (w, h) != (win.w, win.h):
                win.w, win.h = w, h
                win.surface = None
                win.on_resize()
            win.x, win.y = x, y
        self.desk.dirty = True

    def end_drag(self):
        self.drag = None

    # ── routing ─────────────────────────────────────────────────────────────
    def window_at(self, gx, gy):
        for win in reversed(self.windows):
            if not win.minimized and win.hit_test(gx, gy):
                return win
        return None

    def switch_list(self):
        """Alt+Tab order: non-minimized windows, most-recently-active first
        (top of the z-order first)."""
        return [w for w in reversed(self.windows) if not w.minimized]

    def blink(self):
        w = self.active
        if w and w.focus is not None and isinstance(
                w.focus, (W.TextField, W.TextArea)):
            w.caret_on = not w.caret_on
            w.invalidate()


# Window.on_resize default (subclasses relayout their widgets)
def _on_resize(self):
    pass


Window.on_resize = _on_resize


# ── dialogs ──────────────────────────────────────────────────────────────────

def _wrap_text(text, font, maxw):
    out = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            cand = (line + " " + word).strip()
            if T.text_w(font, cand) <= maxw or not line:
                line = cand
            else:
                out.append(line)
                line = word
        out.append(line)
    return out


def msgbox(desk, title, text, icon="info", buttons=("OK",), cb=None,
           default=0, win_icon=None):
    """Win95 message box. cb(label) fires with the chosen button."""
    lines = _wrap_text(text, T.FONT, 260)
    tw = max([T.text_w(T.FONT, ln) for ln in lines] + [120])
    bw, bh, gap = 72, 23, 8
    total = len(buttons) * bw + (len(buttons) - 1) * gap
    w = max(min(360, max(200, tw + 90)), total + 24 + 2 * T.BORDER)
    h = (T.TITLE_H + 2 * T.BORDER + 24 + max(len(lines) * 14, 34) + 40)
    win = Window(desk, title, w, h, icon=win_icon or icon, resizable=False,
                 modal=True)

    class _Body(W.Widget):
        def __init__(self):
            super().__init__(0, 0, w, h)

        def draw(self, d, img):
            icons.paint(img, icon, 14, 14, 32)
            ty = 14
            for ln in lines:
                d.text((60, ty), ln, font=T.FONT, fill=T.TEXT)
                ty += 14

    win.add(_Body())
    bx = (win.client_size()[0] - total) // 2
    by = win.client_size()[1] - bh - 10

    def choose(label):
        win.close()
        if cb:
            cb(label)

    for i, label in enumerate(buttons):
        b = win.add(W.Button(bx + i * (bw + gap), by, bw, bh, label,
                             cb=lambda l=label: choose(l),
                             default=(i == default)))
        if i == default:
            win.set_focus(b)
    desk.wm.add(win)
    snd = {"error": "error", "warn": "exclamation", "info": "asterisk",
           "question": "question"}.get(icon)
    if snd:
        desk.play_sound(snd)
    return win


def inputbox(desk, title, label, initial="", cb=None, icon="exe", width=300):
    """One text field + OK/Cancel. cb(text) on OK, cb(None) on cancel."""
    w = width + 2 * T.BORDER + 20
    h = T.TITLE_H + 2 * T.BORDER + 96
    win = Window(desk, title, w, h, icon=icon, resizable=False, modal=True)
    cw = win.client_size()[0]
    win.add(W.Label(12, 10, label))

    def ok(text=None):
        t = fld.text
        win.close()
        if cb:
            cb(t)

    def cancel():
        win.close()
        if cb:
            cb(None)

    fld = win.add(W.TextField(12, 28, cw - 24, initial, on_enter=ok))
    fld.anchor, fld.cur = 0, len(initial)
    win.add(W.Button(cw - 164, 60, 72, 23, "OK", cb=ok, default=True))
    win.add(W.Button(cw - 84, 60, 72, 23, "Cancel", cb=cancel))
    win.set_focus(fld)
    win.request_close = cancel
    desk.wm.add(win)
    return win
