"""kilix desktop — one clipboard across everything.

Native kilix 95 widgets share Desk.clipboard directly, and Desk.set_clipboard
mirrors copies out to the host terminal with OSC 52. The odd realms out are the
XPanes: every X11 app (Firefox, kilix-amp, …) runs on its own private Xvfb
whose CLIPBOARD selection is walled off from everything else, so a copy in
Firefox could not be pasted into Notepad, another pane, or a terminal tab.

SelectionBridge closes that gap. Point one at a display — an XPane's Xvfb, or
the host X where the terminal/tabs live — and it makes that display's CLIPBOARD
selection track the Desk hub in both directions:

  * app copies (takes CLIPBOARD)  → XFixes notify → we read it → Desk.set_clipboard
  * hub changes (any other realm) → we take CLIPBOARD here → serve it on paste

Desk fans a copy to every bridge but the one it came from, so N panes plus the
host all converge on the same text without echoing in a loop. CLIPBOARD only —
PRIMARY (select-to-paste) is deliberately left alone, matching Win95 semantics.

Everything is best-effort: any X failure disables the bridge and never touches
the desktop. No INCR, so a single paste is capped at the server's max request
(hundreds of KB) — fine for text, and text is all the clipboard carries here.
"""
import os
from contextlib import contextmanager

from Xlib import X, Xatom
from Xlib import display as xdisplay
from Xlib.ext import xfixes
from Xlib.protocol import event as xevent


class SelectionBridge:
    """Bridge one X11 CLIPBOARD selection to a Desk's clipboard hub."""

    def __init__(self, desk, display_name, xauthority=None):
        # set attributes a failed teardown might touch *before* anything can
        # raise, so close()/__del__ are always safe even on a half-built bridge
        self.desk = desk
        self.d = None
        self.win = None
        self._fd = None
        self._text = None            # text we currently serve / last saw
        self._ok = False

        with xauthority_env(xauthority):
            self.d = xdisplay.Display(display_name)
        self.d.xfixes_query_version()             # raises if XFixes is absent
        scr = self.d.screen()
        self.win = scr.root.create_window(
            -10, -10, 1, 1, 0, X.CopyFromParent,
            window_class=X.InputOnly, visual=X.CopyFromParent,
            event_mask=X.PropertyChangeMask)

        self.A_CLIPBOARD = self.d.intern_atom("CLIPBOARD")
        self.A_UTF8 = self.d.intern_atom("UTF8_STRING")
        self.A_TARGETS = self.d.intern_atom("TARGETS")
        self.A_TEXT = self.d.intern_atom("TEXT")
        self.A_STRING = Xatom.STRING
        self.A_PROP = self.d.intern_atom("KILIX_CLIP")

        self.d.xfixes_select_selection_input(
            self.win, self.A_CLIPBOARD,
            xfixes.XFixesSetSelectionOwnerNotifyMask)
        self.d.flush()

        self._fd = self.d.fileno()
        desk.add_fd(self._fd, self._on_readable)
        desk.add_clip_sink(self.push)
        self._ok = True

    # ── hub → app: own CLIPBOARD here and serve `text` on paste ──────────────
    def push(self, text):
        if not self._ok or text is None or text == self._text:
            return
        self._text = text
        try:
            self.win.set_selection_owner(self.A_CLIPBOARD, X.CurrentTime)
            self.d.flush()
        except Exception:
            pass

    # ── app → hub: drain events, publish reads ──────────────────────────────
    def _on_readable(self):
        if not self._ok:
            return
        try:
            pending = self.d.pending_events()
        except Exception:
            return
        for _ in range(pending):
            try:
                ev = self.d.next_event()
            except Exception:
                return
            try:
                self._handle(ev)
            except Exception:
                pass

    def _handle(self, ev):
        # XFixes: the CLIPBOARD owner changed on this display
        if isinstance(ev, xfixes.SetSelectionOwnerNotify):
            if ev.owner == self.win.id:           # our own push — ignore
                return
            if ev.selection == self.A_CLIPBOARD:
                self._request_read()
            return
        et = getattr(ev, "type", None)
        if et == X.SelectionNotify:               # our read completed
            self._read_reply(ev)
        elif et == X.SelectionRequest:            # someone wants our text
            self._serve(ev)
        elif et == X.SelectionClear:              # app reclaimed CLIPBOARD
            self._text = None

    def _request_read(self, target=None):
        try:
            self.win.convert_selection(
                self.A_CLIPBOARD, target or self.A_UTF8, self.A_PROP,
                X.CurrentTime)
            self.d.flush()
        except Exception:
            pass

    def _read_reply(self, ev):
        if getattr(ev, "property", 0) == 0:
            # UTF8_STRING refused — fall back to STRING once, then give up
            if getattr(ev, "target", None) == self.A_UTF8:
                self._request_read(self.A_STRING)
            return
        try:
            r = self.win.get_full_property(self.A_PROP, X.AnyPropertyType)
        except Exception:
            r = None
        try:
            self.win.delete_property(self.A_PROP)
        except Exception:
            pass
        if not r or not getattr(r, "value", None):
            return
        text = _decode(r.value)
        if text is None or text == self._text:
            return
        self._text = text
        try:
            # source=self.push so the hub does not echo this straight back
            self.desk.set_clipboard(text, source=self.push)
        except Exception:
            pass

    def _serve(self, ev):
        req = ev.requestor
        prop = ev.property if ev.property != 0 else ev.target  # obsolete client
        served = False
        try:
            if ev.target == self.A_TARGETS:
                req.change_property(
                    prop, Xatom.ATOM, 32,
                    [self.A_TARGETS, self.A_UTF8, self.A_STRING, self.A_TEXT])
                served = True
            elif ev.target in (self.A_UTF8, self.A_STRING, self.A_TEXT):
                req.change_property(
                    prop, ev.target, 8, (self._text or "").encode("utf-8"))
                served = True
        except Exception:
            served = False
        try:
            req.send_event(xevent.SelectionNotify(
                time=ev.time, requestor=req.id, selection=ev.selection,
                target=ev.target, property=prop if served else 0))
            self.d.flush()
        except Exception:
            pass

    def close(self):
        self._ok = False
        try:
            if self._fd is not None:
                self.desk.remove_fd(self._fd)
        except Exception:
            pass
        try:
            self.desk.remove_clip_sink(self.push)
        except Exception:
            pass
        try:
            if self.d is not None:
                self.d.close()
        except Exception:
            pass


def _decode(value):
    """Selection property value → str. python-xlib hands back bytes for
    format-8 properties, but tolerate an int array just in case."""
    try:
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-8", "replace")
        return bytes(bytearray(value)).decode("utf-8", "replace")
    except Exception:
        return None


@contextmanager
def xauthority_env(path):
    """Temporarily point python-xlib at an Xauthority file for one connect."""
    old = os.environ.get("XAUTHORITY")
    had = "XAUTHORITY" in os.environ
    if path:
        os.environ["XAUTHORITY"] = path
    try:
        yield
    finally:
        if had:
            os.environ["XAUTHORITY"] = old
        else:
            os.environ.pop("XAUTHORITY", None)
