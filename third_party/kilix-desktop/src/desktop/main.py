#!/usr/bin/env python3
"""kilix desktop — a Windows 95-style desktop environment in a kilix pane.

The whole desktop is rendered as pixels (PIL framebuffer, blitted through
the kitty graphics protocol via a bounded POSIX shared-memory ring, or inline
in streamed sessions) with pixel-precise SGR mouse input. Start bar,
overlapping windows, desktop launchers, a file manager,
Notepad, an image viewer and a Settings app that edits the kilix config
live. Programs launch into new kilix tabs over kitty remote control.

Usage:  kilix desktop                 (from inside kilix)
        main.py --screenshot out.png --scene start   (headless render, tests)
Quit :  Start ▸ Shut Down…  ·  Ctrl+Alt+Q
"""
import argparse
import base64
import os
import select
import signal
import sys
import time

_here = os.path.dirname(os.path.abspath(__file__))
KILIX_HOME = os.environ.get("KILIX_HOME") or os.path.dirname(_here)
sys.path.insert(0, os.path.join(KILIX_HOME, "config"))   # kilix_sdk
sys.path.insert(0, _here)

import storage
_pycache = storage.cache_dir("desktop-pycache")
os.makedirs(_pycache, exist_ok=True)
sys.pycache_prefix = _pycache

from PIL import Image

from kilix_sdk import require_compatible as require_kilix_sdk
from kilix_sdk import graphics as kilix_graphics
from kilix_sdk import term as kilix_term
require_kilix_sdk("1.14")
import icons
import shell as shell_mod
import taskbar as taskbar_mod
import theme as T
import widgets as W
import wm as wm_mod

# keys the host terminal parser doesn't map (it never needed F-keys): add them
kilix_term.SPECIAL_TILDE.update({
    11: ("F1", "F1", 112), 12: ("F2", "F2", 113), 13: ("F3", "F3", 114),
    14: ("F4", "F4", 115), 15: ("F5", "F5", 116), 17: ("F6", "F6", 117),
    18: ("F7", "F7", 118), 19: ("F8", "F8", 119), 20: ("F9", "F9", 120),
    21: ("F10", "F10", 121), 23: ("F11", "F11", 122),
    24: ("F12", "F12", 123)})
kilix_term.SPECIAL_CSI.update({
    "P": ("F1", "F1", 112), "Q": ("F2", "F2", 113),
    "S": ("F4", "F4", 115)})

# kitty reports the numeric keypad only as functional (PUA) codes under flags
# 13 (no KP folding, no embedded text) — map them to (key, text) here so the
# keypad types and navigates like the main block.
_KEYPAD = {
    57409: (".", "."), 57410: ("/", "/"), 57411: ("*", "*"),
    57412: ("-", "-"), 57413: ("+", "+"), 57414: ("Enter", "\r"),
    57415: ("=", "="), 57417: ("ArrowLeft", ""), 57418: ("ArrowRight", ""),
    57419: ("ArrowUp", ""), 57420: ("ArrowDown", ""), 57421: ("PageUp", ""),
    57422: ("PageDown", ""), 57423: ("Home", ""), 57424: ("End", ""),
    57425: ("Insert", ""), 57426: ("Delete", "")}
for _i in range(10):
    _KEYPAD[57399 + _i] = (str(_i), str(_i))
# PrintScreen also arrives only as a functional code — name it (text-less, so
# it can never type into a field) for dispatch_key's screenshot binding.
_KEYPAD[57361] = ("PrintScreen", "")


class DeskTerm(kilix_term.Term):
    """Kilix terminal parser with any-motion mouse tracking (hover, drags)."""

    def enter(self):
        import tty
        tty.setraw(self.fd)
        # alt screen, hide cursor, no autowrap, kitty kbd protocol,
        # any-motion + SGR + SGR-pixels mouse, bracketed paste.
        # >15u adds event-type reporting (flag 2) on top of the usual
        # disambiguate+alternates+all-keys so the loop sees the Alt key's
        # release — needed to commit the Alt+Tab switcher (_parse_csi tags
        # every key event with its type; _norm_key drops the releases).
        self.write("\x1b[?1049h\x1b[2J\x1b[?25l\x1b[?7l\x1b[>15u"
                   "\x1b[?1003h\x1b[?1006h\x1b[?1016h\x1b[?2004h"
                   f"\x1b]2;{T.PRODUCT_NAME}\x07")

    def _parse_csi(self, params, final):
        # tag key events with the kitty event type (1 press, 2 repeat,
        # 3 release) — browse drops it; the switcher needs it
        ev = super()._parse_csi(params, final)
        if ev and ev.get("kind") == "key":
            evt = 1
            parts = params.split(";")
            if len(parts) > 1:
                sub = parts[1].split(":")
                if len(sub) > 1 and sub[1]:
                    try:
                        evt = int(sub[1])
                    except ValueError:
                        evt = 1
            ev["evt"] = evt
        return ev

    def restore(self):
        try:
            # tmux filters unwrapped APCs, so in a streamed tmux session the
            # placement-delete must ride the same passthrough envelope the
            # frames do or the dead desktop's last frame stays on attached
            # clients (mirrors blit_direct's in_tmux path).
            delete = "\x1b_Ga=d,d=A\x1b\\"
            if os.environ.get("KILIX_STREAM") == "1" and os.environ.get("TMUX"):
                delete = kilix_graphics.wrap_tmux_passthrough(delete)
            self.write("\x1b[<u\x1b[?1003l\x1b[?1006l\x1b[?1016l\x1b[?2004l"
                       "\x1b[?7h" + delete + "\x1b[?25h\x1b[?1049l")
        finally:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)


class Desk:
    def __init__(self, term=None, size=None, draw_cursor=False,
                 show_first_run_help=True):
        self.term = term
        if term:
            self.w = int(term.cols * term.cell_w)
            self.h = int(term.rows * term.cell_h)
        else:
            self.w, self.h = size or (1024, 768)
        self.fb = Image.new("RGB", (self.w, self.h), T.DESKTOP)
        self.dirty = True
        self.running = True
        self.clipboard = ""
        self._clip_sinks = []         # realms that mirror the hub (XPanes, host)
        self.clip_host = None         # host-X CLIPBOARD bridge (set up in run())
        self.draw_cursor = draw_cursor
        self.show_first_run_help = show_first_run_help
        self.mouse_pos = (self.w // 2, self.h // 2)
        self.menus = W.MenuHost(self)
        self.wm = wm_mod.WM(self)
        self.shell = shell_mod.Shell(self)
        self.taskbar = taskbar_mod.Taskbar(self)
        self.password_nag = False
        self.fd_hooks = {}            # fd -> callback (XPane video feeds etc.)
        self.tick_hooks = []          # callables(now), each loop pass
        self.mouse_owner = None
        self._owner_btn = 0           # button that captured mouse_owner/drag
        self._buttons = 0
        self._last_click = (0.0, -99, -99, 0, 0)
        # graphics transport (mirrors the host browser transport)
        self.stream = os.environ.get("KILIX_STREAM") == "1"
        wid = os.environ.get("KITTY_WINDOW_ID", str(os.getpid()))
        self.wid = wid
        self.img_id = 1 + ((int(wid) if wid.isdigit() else os.getpid())
                           % 4000)
        self.presenter = None           # lazy: screenshot tests have no term
        # WM/loop polish state
        self.switcher = None          # Alt+Tab overlay: {"wins", "sel"} or None
        self._tooltip = None          # current tooltip text or None
        self._tooltip_pos = (0, 0)
        self._hover_pos = self.mouse_pos
        self._hover_since = 0.0
        self.saver = None             # active screensaver instance or None
        self.saving = False
        self._saver_last = 0.0
        self._last_input = time.time()
        try:
            self.saver_idle = float(os.environ.get("KILIX_SAVER_IDLE") or 180)
        except ValueError:
            self.saver_idle = 180.0

    def size(self):
        return self.w, self.h

    def add_fd(self, fd, cb):
        """Watch fd in the main select loop; cb() when readable."""
        self.fd_hooks[fd] = cb

    def remove_fd(self, fd):
        self.fd_hooks.pop(fd, None)

    def quit(self):
        self.running = False

    def add_clip_sink(self, sink):
        """Register a realm (an XPane's Xvfb, the host X) that should mirror
        the clipboard hub. `sink` is a callable(text)."""
        self._clip_sinks.append(sink)

    def remove_clip_sink(self, sink):
        if sink in self._clip_sinks:
            self._clip_sinks.remove(sink)

    def set_clipboard(self, text, source=None):
        """Publish `text` as the one clipboard. `source`, when given, is the
        sink the copy came from — it is skipped in the fan-out so a read from
        one realm never echoes straight back into it."""
        self.clipboard = text
        # OSC 52 mirrors to the host terminal/tabs — but only when no host X
        # bridge is active, or the two would fight over the host CLIPBOARD
        if self.term and self.clip_host is None:
            b64 = base64.b64encode(text.encode()).decode()
            self.term.write(f"\x1b]52;c;{b64}\x07")
        for sink in list(self._clip_sinks):
            if sink is source:
                continue
            try:
                sink(text)
            except Exception:
                pass

    def play_sound(self, name):
        """Fire-and-forget UI sound. No-op headless (term is None) or when the
        tray volume is muted/zero; never blocks the loop, never raises."""
        if self.term is None:
            return
        try:
            st = self.shell.state
            vol, muted = int(st.get("volume", 75)), bool(st.get("muted", False))
        except Exception:
            vol, muted = 75, False
        try:
            import sounds
            sounds.play(name, volume=vol, muted=muted)
        except Exception:
            pass

    # ── rendering ───────────────────────────────────────────────────────────
    def render(self):
        if not self.dirty:
            return
        fb = self.fb
        d = W.drawer(fb)
        self.shell.draw(fb, d)
        for win in self.wm.windows:
            if win.minimized:
                continue
            surf = win.render()
            fb.paste(surf, (win.x, win.y), win.compose_mask)
        self.taskbar.draw(fb, d)
        self.menus.draw(fb, d)
        if self.switcher is not None:
            self._draw_switcher(d)
        elif self._tooltip:
            self._draw_tooltip(d)
        if self.draw_cursor:
            self._paint_cursor(d)
        self.dirty = False
        self.blit()

    def _paint_cursor(self, d):
        x, y = self.mouse_pos
        pts = [(x, y), (x, y + 14), (x + 4, y + 10), (x + 7, y + 16),
               (x + 9, y + 15), (x + 6, y + 9), (x + 11, y + 9)]
        d.polygon(pts, fill=T.LIGHT, outline=T.TEXT)

    # ── Alt+Tab window switcher ──────────────────────────────────────────────
    def _switch(self, direction):
        """Open (or advance) the switcher overlay by one step."""
        wins = self.wm.switch_list()
        if not wins:
            return
        if self.switcher is None:
            self.switcher = {"wins": wins, "sel": 0}
        sel = (self.switcher["sel"] + direction) % len(self.switcher["wins"])
        self.switcher["sel"] = sel
        self.dirty = True

    def _end_switch(self, commit=True):
        sw, self.switcher = self.switcher, None
        if sw is None:
            return
        wins, sel = sw["wins"], sw["sel"]
        # a window may have closed mid-switch (an XPane teardown fires from a
        # tick hook while Alt is held) — only activate one still on the stack
        if (commit and 0 <= sel < len(wins)
                and wins[sel] in self.wm.windows):
            self.wm.activate(wins[sel])
        self.dirty = True

    def _cycle(self, direction):
        """Alt+Esc: raise the next/previous window with no overlay."""
        wins = self.wm.switch_list()
        if len(wins) >= 2:
            self.wm.activate(wins[direction % len(wins)])

    def _draw_switcher(self, d):
        sw = self.switcher
        wins = sw["wins"]
        if not wins:
            return
        row_h, pad = 22, 8
        tw = max(T.text_w(T.FONT, w.title) for w in wins)
        pw = max(180, min(tw + 26 + 24, self.w - 40))
        ph = pad * 2 + len(wins) * row_h
        px = (self.w - pw) // 2
        py = (self.h - ph) // 2
        T.raised(d, px, py, px + pw - 1, py + ph - 1)
        for i, win in enumerate(wins):
            ry = py + pad + i * row_h
            rx0, rx1 = px + pad, px + pw - pad - 1
            if i == sw["sel"]:
                d.rectangle([rx0, ry, rx1, ry + row_h - 2], fill=T.SEL_BG)
                T.focus_rect(d, rx0, ry, rx1, ry + row_h - 2, off=T.SEL_BG)
                col = T.SEL_TX
            else:
                col = T.TEXT
            icons.paint(self.fb, win.icon, rx0 + 4, ry + (row_h - 2 - 16) // 2,
                        16)
            label = T.ellipsize(T.FONT, win.title, pw - 26 - 2 * pad)
            d.text((rx0 + 26, ry + (row_h - 13) // 2), label, font=T.FONT,
                   fill=col)

    # ── tooltips ─────────────────────────────────────────────────────────────
    def _hide_tooltip(self):
        if self._tooltip is not None:
            self._tooltip = None
            self.dirty = True

    def _tooltip_query(self, x, y):
        win = self.wm.window_at(x, y)
        obj = win if win is not None else (
            self.taskbar if self.taskbar.hit(x, y) else self.shell)
        fn = getattr(obj, "tooltip_at", None)
        if fn is None:
            return None
        try:
            return fn(x, y)
        except Exception:
            return None

    def _draw_tooltip(self, d):
        txt = self._tooltip
        x, y = self._tooltip_pos
        tw = T.text_w(T.FONT, txt)
        bx, by = x + 12, y + 20
        bx = min(bx, self.w - tw - 8)
        if by + 16 > self.h:
            by = y - 18
        d.rectangle([bx, by, bx + tw + 5, by + 15], fill=T.INFO_BG,
                    outline=T.TEXT)
        d.text((bx + 3, by + 2), txt, font=T.FONT, fill=T.TEXT)

    # ── idle screensaver ─────────────────────────────────────────────────────
    def maybe_start_saver(self, now=None):
        """Engage the screensaver if idle past the timeout. Never headless."""
        now = now if now is not None else time.time()
        if (self.term is not None and not self.saving and self.saver_idle > 0
                and now - self._last_input >= self.saver_idle):
            self._start_saver()
            return True
        return False

    def _start_saver(self):
        import screensaver
        self.saver = screensaver.pick(self.size())
        self.saving = True
        self._saver_last = time.time()
        self.dirty = True

    def _wake_saver(self):
        if not self.saving:
            return
        self.saving = False
        self.saver = None
        self._last_input = time.time()
        self.dirty = True

    # ── shutdown screen ──────────────────────────────────────────────────────
    def shutdown(self):
        """Quit path. Plays the shutdown cue, then returns so the session just
        ends — no 'It's now safe to turn off your computer' screen."""
        self.play_sound("shutdown")

    def blit(self, img=None, force_full=False):
        if not self.term:
            return
        rgb = (img if img is not None else self.fb).tobytes()
        if self.presenter is None:
            self.presenter = kilix_graphics.FramePresenter(
                self.term, self.img_id, stream=self.stream,
                in_tmux=bool(os.environ.get("TMUX")),
                stream_warmup_seconds=0)
        result = self.presenter.present(
            rgb, self.w, self.h, self.term.cols, self.term.rows,
            content_key="fb" if img is None else "system",
            force_full=force_full)
        # Keepalive cadence measures actual transmissions, not render calls.
        if result.emitted:
            self._last_blit = time.time()
        return result

    def cleanup_shm(self, *, discard=False):
        if self.presenter is not None:
            self.presenter.close(discard=discard)
            self.presenter = None

    # ── screenshots (PrtSc) ─────────────────────────────────────────────────
    def screenshot(self, window=None):
        """Save the screen — or one window's surface — as a timestamped PNG.

        The desktop already renders to an RGB framebuffer, so the capture is
        a render + save. Shots land in the desktop folder, so each one shows
        up as an icon the moment the key is pressed. Returns the path, or
        None when the save failed (reported in a message box, never a crash).
        """
        self.render()                 # flush pending damage: shoot what shows
        img = self.fb if window is None else window.render()
        base = time.strftime("Screenshot %Y-%m-%d %H.%M.%S")
        path = os.path.join(self.shell.dir, base + ".png")
        n = 2
        while os.path.exists(path):
            path = os.path.join(self.shell.dir, f"{base} ({n}).png")
            n += 1
        try:
            img.save(path)
        except Exception as e:
            wm_mod.msgbox(self, "Screenshot", str(e), icon="error")
            return None
        self.play_sound("asterisk")
        self.shell.dir_changed(self.shell.dir)
        return path

    # ── input normalization ─────────────────────────────────────────────────
    def _norm_key(self, raw):
        evt = raw.get("evt", 1)       # 1 press, 2 repeat, 3 release
        key = raw["key"]
        # the Alt key reports its own press AND release (so the switcher can
        # commit on Alt-up); other bare modifiers stay filtered out
        if len(key) == 1 and ord(key) in (57443, 57449):   # L/R Alt
            return W.Ev(kind="key", key="Alt", press=(evt != 3),
                        alt=(evt != 3))
        if evt == 3:
            return None               # drop key releases (no double-typing)
        mods = max(0, raw.get("mods", 1) - 1)
        text = raw.get("text", "")
        if len(key) == 1 and 57344 <= ord(key) <= 63743:
            kp = _KEYPAD.get(ord(key))
            if kp is None:
                return None           # kitty functional keycodes (bare mods)
            key, text = kp
        return W.Ev(kind="key", key=key, text=text,
                    shift=bool(mods & 1), alt=bool(mods & 2),
                    ctrl=bool(mods & 4))

    def _norm_mouse(self, raw):
        b = raw["b"]
        if b & 256:                   # SGR-pixel leave indicator
            return None
        if b & 128:                   # side buttons 8-11: no desktop meaning
            return None
        # padding-edge / inter-pane wheel reports carry coords outside the
        # framebuffer (kitty measures from the content origin) — clamp so they
        # route to the edge window instead of missing every window.
        x = max(0, min(raw["x"], self.w - 1))
        y = max(0, min(raw["y"], self.h - 1))
        self.mouse_pos = (x, y)
        mods = dict(shift=bool(b & 4), alt=bool(b & 8), ctrl=bool(b & 16))
        if b & 64:                    # wheel: 0 up, 1 down, 2/3 horizontal
            c3 = b & 3
            if c3 >= 2:
                return None           # horizontal wheel — not a vertical scroll
            return W.Ev(kind="mouse", x=x, y=y,
                        wheel=(-1 if c3 == 0 else 1), **mods)
        if b & 32:                    # motion
            return W.Ev(kind="mouse", x=x, y=y, move=True,
                        btn=self._buttons, **mods)
        btn = (b & 3) + 1
        if raw["press"]:
            t, lx, ly, lb, cc = self._last_click
            cc = cc + 1 if (btn == lb and time.time() - t < 0.4
                            and abs(x - lx) < 5 and abs(y - ly) < 5) else 1
            self._last_click = (time.time(), x, y, btn, cc)
            self._buttons |= 1 << (btn - 1)
            return W.Ev(kind="mouse", x=x, y=y, btn=btn, press=True,
                        clicks=cc, **mods)
        self._buttons &= ~(1 << (btn - 1))
        return W.Ev(kind="mouse", x=x, y=y, btn=btn, press=False, **mods)

    # ── dispatch ────────────────────────────────────────────────────────────
    def dispatch_mouse(self, ev):
        self._last_input = time.time()
        if ev.press or ev.wheel:
            self._hover_since = self._last_input
            self._hide_tooltip()
        self._dispatch_mouse(ev)
        if (not ev.press and not ev.move and not ev.wheel
                and ev.btn == self._owner_btn):
            # the release of the CAPTURING button ends capture (even when a
            # menu ate the event — otherwise the owner set by the menu-opening
            # press leaks); another button's release must not abort the drag
            self.mouse_owner = None

    def _dispatch_mouse(self, ev):
        if self.draw_cursor and (ev.move or ev.press):
            self.dirty = True
        if self.menus.active:
            if self.menus.on_mouse(ev):
                return
        if self.mouse_owner is not None:
            self.mouse_owner(ev)
            return
        modal = self.wm.modal_top()
        # the taskbar is composited last (on top), so it must be hit-tested
        # before windows or a window overlapping the bottom strip steals its
        # Start-button/task-button clicks
        if self.taskbar.hit(ev.x, ev.y):
            self.taskbar.on_mouse(ev)
            if ev.press:
                self.mouse_owner = self.taskbar.on_mouse
                self._owner_btn = ev.btn
            return
        win = self.wm.window_at(ev.x, ev.y)
        if win is not None:
            if modal and win is not modal:
                if ev.press:
                    self.wm.activate(modal)
                return
            if ev.press:
                if self.wm.active is not win:
                    self.wm.activate(win)
                self.mouse_owner = self._route_window(win)
                self._owner_btn = ev.btn
            win.on_mouse(ev)
            if self.wm.drag:
                self.mouse_owner = self._route_drag
            return
        if modal:
            if ev.press:
                self.wm.activate(modal)
            return
        self.shell.on_mouse(ev)
        if ev.press:
            self.mouse_owner = self.shell.on_mouse
            self._owner_btn = ev.btn

    def _route_window(self, win):
        def route(ev):
            if self.wm.drag:
                self._route_drag(ev)
            else:
                win.on_mouse(ev)
        return route

    def _route_drag(self, ev):
        if ev.move:
            self.wm.drag_motion(ev)
        elif not ev.press and ev.btn == self._owner_btn:
            self.wm.end_drag()

    def dispatch_key(self, ev):
        self._last_input = time.time()
        self._hover_since = self._last_input
        self._hide_tooltip()
        if ev.key == "PrintScreen":
            # global, before menus/modality: PrtSc captures the screen as it
            # is, open menus included; Alt+PrtSc just the active window
            self.screenshot(self.wm.active if ev.alt else None)
            return
        if self.menus.active:
            self.menus.on_key(ev)
            return
        # ── Alt+Tab / Alt+Esc window switching ──────────────────────────────
        if ev.key == "Alt":
            if not ev.press:          # Alt released → commit the selection
                self._end_switch(commit=True)
            return
        if ev.alt and ev.key == "Tab":
            self._switch(-1 if ev.shift else 1)
            return
        if ev.alt and ev.key == "Escape":     # Alt+Esc: cycle, no overlay
            self._cycle(-1 if ev.shift else 1)
            return
        if self.switcher is not None:         # any other key ends the switcher
            self._end_switch(commit=True)
            return
        if ev.ctrl and ev.alt and ev.key == "q":
            self.quit()
            return
        if ev.ctrl and ev.key == "Escape":
            self.taskbar.open_start_menu()
            return
        modal = self.wm.modal_top()
        if modal and self.wm.active is not modal:
            # keyboard is modal too: never let typing (or Alt+F4) reach a
            # window activated behind an open modal dialog
            self.wm.activate(modal)
            return
        if ev.alt and ev.key == "F4":
            if self.wm.active:
                self.wm.active.request_close()
            return
        if self.wm.active and self.wm.active.on_key(ev):
            return
        if not self.wm.active or self.wm.modal_top() is None:
            self.shell.on_key(ev)

    def dispatch_paste(self, text):
        if self.menus.active:
            return
        win = self.wm.active
        if win and isinstance(win.focus, (W.TextField, W.TextArea)):
            win.focus.insert(text)
            if isinstance(win.focus, W.TextArea):
                win.focus._reveal()
            win.invalidate()

    # ── resize ──────────────────────────────────────────────────────────────
    def do_resize(self):
        self.term.refresh_size()
        self.w = int(self.term.cols * self.term.cell_w)
        self.h = int(self.term.rows * self.term.cell_h)
        self.fb = Image.new("RGB", (self.w, self.h), T.DESKTOP)
        if self.presenter is not None:
            self.presenter.invalidate()
        self.menus.close_all()        # popups clamp to the old size — drop them
        self.shell.on_resize()
        for win in self.wm.windows:
            if win.maximized:
                win.x = win.y = 0
                win.w, win.h = self.w, self.h - T.TASKBAR_H
                win.surface = None
                win.on_resize()
                if win._restore:      # keep the un-maximize rect on-screen too
                    rx, ry, rw, rh = win._restore
                    rw, rh = min(rw, self.w), min(rh, self.h - T.TASKBAR_H)
                    rx = max(0, min(rx, self.w - 60))
                    ry = max(0, min(ry, self.h - T.TASKBAR_H - 20))
                    win._restore = (rx, ry, rw, rh)
            else:
                win.x = max(0, min(win.x, self.w - 60))
                win.y = max(0, min(win.y, self.h - T.TASKBAR_H - 20))
            win.dirty = True
        self.dirty = True

    def _first_run_help(self):
        """First launch: open the Help book so a new user (e.g. a fresh
        Plebian-OS boot into the kilix 95 desktop) gets oriented. A marker in
        the persisted desktop state makes it pop exactly once."""
        if self.shell.state.get("help_shown"):
            return
        self.shell.state["help_shown"] = True
        self.shell._save_state()
        try:
            import apps
            apps.open(self, "winhelp", None)
        except Exception:
            pass

    def _refresh_password_nag(self):
        """Refresh the persistent tray warning from the helper's state."""
        try:
            import security
            self.password_nag = bool(self.term) and security.is_default_password()
        except Exception:
            self.password_nag = False
        self.dirty = True

    def _first_run_password_nag(self):
        """Show one reminder balloon per session while the default remains."""
        if not self.password_nag or getattr(self, "_pw_balloon_shown", False):
            return
        self._pw_balloon_shown = True
        try:
            self.taskbar.show_password_balloon()
        except Exception:
            pass

    # ── main loop ───────────────────────────────────────────────────────────
    def run(self):
        term = self.term
        resized = [False]
        signal.signal(signal.SIGWINCH, lambda *a: resized.__setitem__(0, True))
        for s in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(s, lambda *a: sys.exit(0))
        os.set_blocking(term.fd, False)
        term.enter()
        # one clipboard across tabs/panes/windows: bridge the host X CLIPBOARD
        # (where the terminal and its tabs live) into the hub. Best-effort — with
        # no reachable host X (remote/nested share) OSC 52 stays the fallback.
        if os.environ.get("KILIX_HOST_CLIP", "1") != "0" and \
                os.environ.get("DISPLAY"):
            try:
                import clipboard as clip_mod
                self.clip_host = clip_mod.SelectionBridge(
                    self, os.environ["DISPLAY"])
            except Exception:
                self.clip_host = None
        try:
            import sounds
            sounds.warm()             # off-thread cache fill so no cue blocks the loop
        except Exception:
            pass
        self.play_sound("startup")
        self._refresh_password_nag()
        if self.show_first_run_help:
            self._first_run_help()
        self._first_run_password_nag()
        last_blink = time.time()
        self._last_blit = 0.0
        start = time.time()
        try:
            self.render()
            while self.running:
                rlist = [term.fd] + list(self.fd_hooks)
                r, _, _ = select.select(rlist, [], [], 0.25)
                for fd in r:
                    if fd == term.fd:
                        continue
                    cb = self.fd_hooks.get(fd)
                    if cb:
                        cb()
                if term.fd in r:
                    for raw in term.read_input():
                        self._last_input = time.time()
                        if self.saving:
                            self._wake_saver()   # any input exits; swallow it
                            continue
                        if raw["kind"] == "key":
                            ev = self._norm_key(raw)
                            if ev:
                                self.dispatch_key(ev)
                        elif raw["kind"] == "mouse":
                            ev = self._norm_mouse(raw)
                            if ev:
                                self.dispatch_mouse(ev)
                        elif raw["kind"] == "paste":
                            self.dispatch_paste(raw["text"])
                if resized[0]:
                    resized[0] = False
                    self.do_resize()
                now = time.time()
                self.taskbar.tick(now)
                for hook in list(self.tick_hooks):
                    hook(now)
                if self.presenter is not None:
                    flushed = self.presenter.flush()
                    if flushed.emitted:
                        self._last_blit = time.time()
                if now - last_blink >= 0.53:
                    last_blink = now
                    self.wm.blink()
                # idle screensaver: while engaged, step and blit each pass
                # (this doubles as the keepalive) and skip the desktop render
                self.maybe_start_saver(now)
                if self.saving:
                    self.blit(self.saver.step(now - self._saver_last))
                    self._saver_last = now
                    continue
                # hover-dwell tooltip
                if self.switcher is None and not self.menus.active:
                    if self.mouse_pos != self._hover_pos:
                        self._hover_pos = self.mouse_pos
                        self._hover_since = now
                        self._hide_tooltip()
                    elif self._tooltip is None and now - self._hover_since >= 0.7:
                        txt = self._tooltip_query(*self.mouse_pos)
                        if txt:
                            self._tooltip = txt
                            self._tooltip_pos = self.mouse_pos
                            self.dirty = True
                # keepalive re-blits: kitty drops graphics sent while the
                # window is still settling (tab bar / pane title bar appear
                # right after startup and clear placements), and rendering is
                # otherwise damage-driven — so repeat the frame aggressively
                # for the first seconds and slowly forever after
                age = now - self._last_blit
                if age >= 0.5 and now - start < 5 or age >= 10:
                    self.blit(force_full=True)   # heal dropped placements
                self.render()
        except KeyboardInterrupt:
            pass
        finally:
            try:
                self.shutdown()       # shutdown cue on the way out
            except Exception:
                pass
            term.restore()
            self.cleanup_shm()


# ── screenshot mode (offscreen render, used by the self-test) ───────────────

def _scene(desk, name):
    import apps
    if name in ("filemgr", "all"):
        apps.open(desk, "filemgr", KILIX_HOME)
    if name in ("notepad", "all"):
        apps.open(desk, "notepad", None)
        np = desk.wm.windows[-1]
        np.ta.set_text("kilix 95 — notepad self-test\n\nThe quick brown fox "
                       "jumps over the lazy dog.\n0123456789\n")
        np.x, np.y = 90, 60
    if name in ("settings", "all"):
        apps.open(desk, "settings", None)
    if name == "dialog":
        desk.shell.shutdown_dialog()
    if name == "launcher":
        desk.shell.create_launcher_dialog()
    if name == "menu":
        desk.shell._context(None, W.Ev(kind="mouse", x=300, y=200))
    if name == "start":
        desk.taskbar.open_start_menu()
        # walk into Programs so the submenu shows too
        m = desk.menus.stack[0]
        m.hot = 0
        for it, (x0, y0, x1, y1) in m.item_rects():
            if it.label == "Programs":
                desk.menus.open(it.submenu, x1 - 2, y0 - 2)
                break


def main():
    ap = argparse.ArgumentParser(prog="kilix desktop")
    ap.add_argument("--cursor", dest="cursor", action="store_true",
                    default=True, help=argparse.SUPPRESS)   # legacy (now default)
    ap.add_argument("--no-cursor", dest="cursor", action="store_false",
                    help="don't draw the desktop's own mouse pointer")
    ap.add_argument("--dir", help="desktop folder override")
    ap.add_argument("--app", metavar="NAME",
                    help="boot with a built-in or pinned catalog app open")
    ap.add_argument("--open", metavar="PATH",
                    help="open a document or directory with the desktop's file associations")
    ap.add_argument("--screenshot", metavar="PNG",
                    help="render offscreen to PNG and exit (no terminal)")
    ap.add_argument("--scene", default="desktop",
                    choices=["desktop", "start", "filemgr", "notepad",
                             "settings", "dialog", "launcher", "menu", "all"])
    ap.add_argument("--size", default="1024x768",
                    help="screenshot size WxH")
    a = ap.parse_args()
    if a.dir:
        os.environ["KILIX_DESKTOP_DIR"] = a.dir
    if a.screenshot:
        w, h = (int(v) for v in a.size.lower().split("x"))
        desk = Desk(term=None, size=(w, h))
        _scene(desk, a.scene)
        desk.render()
        desk.fb.save(a.screenshot)
        print(f"wrote {a.screenshot} ({w}x{h}, scene={a.scene})")
        return
    desk = Desk(term=DeskTerm(), draw_cursor=a.cursor,
                show_first_run_help=not (a.app or a.open))
    if a.app:
        # Through shell.open_app, the same door the Start menu uses: a bad
        # name or a disabled game becomes a dialog on the desk, never a
        # crashed boot.
        desk.shell.open_app(a.app)
    if a.open:
        desk.shell.open_path(a.open)
    desk.run()


if __name__ == "__main__":
    main()
