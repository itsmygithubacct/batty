"""kilix — X11 input injection (keyboard/mouse) via XTest.

Factored out of apprun.AppPane so it can be reused by:
  - `kilix run` (Phase 2): inject the local pane's kitty-kbd + SGR-pixel events
    into the app's private X server (Xvfb or, in --serve mode, Xvnc).
  - `kilix share` (Phase 3, share.py): inject a remote viewer's events
    into the headless Xvfb the whole kilix runs on.

Injects only into the private display it is handed — never the real one. Tracks
which keys/buttons are currently held and can release them all on disconnect, so
a viewer that drops mid-drag or mid-keypress never leaves a stuck modifier or
button down on the shared display.
"""
from Xlib import X, XK
from Xlib.ext import xtest

# kitty functional keycodes for modifier keys -> X keysym names
MOD_KEYSYMS = {57441: "Shift_L", 57442: "Control_L", 57443: "Alt_L",
               57444: "Super_L", 57447: "Shift_R", 57448: "Control_R",
               57449: "Alt_R", 57450: "Super_R"}

NAME_KEYSYMS = {"Enter": "Return", "Escape": "Escape",
                "Backspace": "BackSpace", "Tab": "Tab",
                "ArrowUp": "Up", "ArrowDown": "Down",
                "ArrowLeft": "Left", "ArrowRight": "Right",
                "Home": "Home", "End": "End", "PageUp": "Prior",
                "PageDown": "Next", "Insert": "Insert", "Delete": "Delete",
                **{f"F{i}": f"F{i}" for i in range(1, 13)}}


class Injector:
    def __init__(self, xd, app_w, app_h):
        self.xd = xd
        self.app_w, self.app_h = app_w, app_h
        self._keys_down = set()      # keycodes currently pressed
        self._btns_down = set()      # X button numbers currently pressed

    def keysym_for(self, key):
        if len(key) == 1:
            o = ord(key)
            if o in MOD_KEYSYMS:
                return XK.string_to_keysym(MOD_KEYSYMS[o])
            if 57344 <= o <= 63743:      # other functional keys: unmapped
                return 0
            if o < 256:                  # latin-1 keysyms == codepoints
                return o
            return 0
        name = NAME_KEYSYMS.get(key)
        return XK.string_to_keysym(name) if name else 0

    def key(self, key, etype):
        """etype: 1 = press, 3 = release. Returns True if a key was injected."""
        keysym = self.keysym_for(key)
        if not keysym:
            return False
        keycode = self.xd.keysym_to_keycode(keysym)
        if not keycode:
            return False
        if etype == 1:
            xtest.fake_input(self.xd, X.KeyPress, keycode)
            self._keys_down.add(keycode)
        else:
            xtest.fake_input(self.xd, X.KeyRelease, keycode)
            self._keys_down.discard(keycode)
        self.xd.flush()
        return True

    def key_named(self, xname, etype):
        """Press/release by X keysym NAME (e.g. 'Return', 'Control_L', 'Up').
        Used by kilix share to inject a browser viewer's named keys."""
        keysym = XK.string_to_keysym(xname)
        if not keysym:
            return False
        keycode = self.xd.keysym_to_keycode(keysym)
        if not keycode:
            return False
        if etype == 1:
            xtest.fake_input(self.xd, X.KeyPress, keycode)
            self._keys_down.add(keycode)
        else:
            xtest.fake_input(self.xd, X.KeyRelease, keycode)
            self._keys_down.discard(keycode)
        self.xd.flush()
        return True

    def move_click(self, x, y, button=0, press=None):
        """Absolute pointer move, and optional button/wheel, in display pixels.
        Used by kilix share (whole-screen coords, no letterbox mapping)."""
        x = max(0, min(self.app_w - 1, int(x)))
        y = max(0, min(self.app_h - 1, int(y)))
        xtest.fake_input(self.xd, X.MotionNotify, x=x, y=y)
        if button in (4, 5):             # wheel
            xtest.fake_input(self.xd, X.ButtonPress, button)
            xtest.fake_input(self.xd, X.ButtonRelease, button)
        elif button and press is not None:
            if press:
                xtest.fake_input(self.xd, X.ButtonPress, button)
                self._btns_down.add(button)
            else:
                xtest.fake_input(self.xd, X.ButtonRelease, button)
                self._btns_down.discard(button)
        self.xd.flush()

    def paste(self, text):
        for ch in text:
            keysym = self.keysym_for(ch if ch != "\n" else "Enter")
            keycode = self.xd.keysym_to_keycode(keysym) if keysym else 0
            if keycode:
                xtest.fake_input(self.xd, X.KeyPress, keycode)
                xtest.fake_input(self.xd, X.KeyRelease, keycode)
        self.xd.flush()

    def mouse(self, ev, box):
        """Map a pane-pixel mouse event through `box` (x,y,w,h — the on-screen
        image rect) into app pixels, then inject motion/buttons/wheel."""
        bx, by, bw, bh = box
        ax = min(self.app_w - 1, max(0, round((ev["x"] - bx) * self.app_w / bw)))
        ay = min(self.app_h - 1, max(0, round((ev["y"] - by) * self.app_h / bh)))
        b = ev["b"]
        if b & 64:                       # wheel -> X buttons 4/5
            btn = 4 if (b & 3) == 0 else 5
            xtest.fake_input(self.xd, X.MotionNotify, x=ax, y=ay)
            xtest.fake_input(self.xd, X.ButtonPress, btn)
            xtest.fake_input(self.xd, X.ButtonRelease, btn)
        elif b & 32:                     # motion (with or without drag)
            xtest.fake_input(self.xd, X.MotionNotify, x=ax, y=ay)
        else:
            btn = (b & 3) + 1            # 0/1/2 -> left/middle/right
            xtest.fake_input(self.xd, X.MotionNotify, x=ax, y=ay)
            if ev["press"]:
                xtest.fake_input(self.xd, X.ButtonPress, btn)
                self._btns_down.add(btn)
            else:
                xtest.fake_input(self.xd, X.ButtonRelease, btn)
                self._btns_down.discard(btn)
        self.xd.flush()

    def release_all(self):
        """Release every key/button we still hold — call on client disconnect
        or shutdown so nothing stays stuck down on the shared display."""
        for keycode in list(self._keys_down):
            try:
                xtest.fake_input(self.xd, X.KeyRelease, keycode)
            except Exception:
                pass
        for btn in list(self._btns_down):
            try:
                xtest.fake_input(self.xd, X.ButtonRelease, btn)
            except Exception:
                pass
        self._keys_down.clear()
        self._btns_down.clear()
        try:
            self.xd.flush()
        except Exception:
            pass
