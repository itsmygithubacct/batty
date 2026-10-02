"""kilix desktop — Calculator (Standard). A pending-operator state machine
over a grid of raised Buttons with a sunken result display."""
import math

import theme as T
import widgets as W
import wm

PAD, GAP, COLS = 6, 5, 6
DISP_H, TOP_H = 30, 26

_BIG = T._find_font(T._candidates(False), 18)   # big right-aligned readout

_DIV0 = "Cannot divide by zero"
_BADIN = "Invalid input"

# token, col, row (in the 4×6 grid); memory col, digits, operators, functions
_GRID = [
    ("MC", 0, 0), ("7", 1, 0), ("8", 2, 0), ("9", 3, 0), ("/", 4, 0), ("sqrt", 5, 0),
    ("MR", 0, 1), ("4", 1, 1), ("5", 2, 1), ("6", 3, 1), ("*", 4, 1), ("%", 5, 1),
    ("MS", 0, 2), ("1", 1, 2), ("2", 2, 2), ("3", 3, 2), ("-", 4, 2), ("1/x", 5, 2),
    ("M+", 0, 3), ("0", 1, 3), ("+/-", 2, 3), (".", 3, 3), ("+", 4, 3), ("=", 5, 3),
]
_LABEL = {"sqrt": "√", "1/x": "1/x", "+/-": "+/-", "bs": "Backspace"}


class Calc(wm.Window):
    def __init__(self, desk, arg=None):
        super().__init__(desk, "Calculator", 256, 280, icon="calc",
                         resizable=False)
        self.acc = 0.0
        self.op = None
        self.entry = "0"
        self.fresh = True            # next digit starts a new entry
        self.error = None
        self.mem = 0.0
        cw, ch = self.client_size()
        bw = (cw - 2 * PAD - (COLS - 1) * GAP) // COLS
        gy = PAD + DISP_H + GAP + TOP_H + GAP
        bh = (ch - gy - PAD - 3 * GAP) // 4
        colx = lambda c: PAD + c * (bw + GAP)
        span2 = 2 * bw + GAP
        ty = PAD + DISP_H + GAP
        self.add(W.Button(colx(0), ty, span2, TOP_H, "Backspace",
                          cb=lambda: self._press("bs")))
        self.add(W.Button(colx(2), ty, span2, TOP_H, "CE",
                          cb=lambda: self._press("ce")))
        self.add(W.Button(colx(4), ty, span2, TOP_H, "C",
                          cb=lambda: self._press("c")))
        for tok, c, r in _GRID:
            self.add(W.Button(colx(c), gy + r * (bh + GAP), bw, bh,
                              _LABEL.get(tok, tok),
                              cb=lambda t=tok: self._press(t)))

    def draw_client(self, d, img):
        cw, _ = self.client_size()
        T.sunken(d, PAD, PAD, cw - PAD, PAD + DISP_H - 1)
        s = self.error or self.entry
        tw = int(_BIG.getlength(s))
        d.text((cw - PAD - 6 - tw, PAD + (DISP_H - 18) // 2), s,
               font=_BIG, fill=T.TEXT)
        if self.mem:
            T.sunken(d, PAD, PAD + 4, PAD + 14, PAD + DISP_H - 5, fill=T.FACE)
            d.text((PAD + 4, PAD + 6), "M", font=T.SMALL, fill=T.TEXT)

    # ── state machine ───────────────────────────────────────────────────────
    def _press(self, tok):
        if tok == "c":
            self.acc, self.op, self.entry, self.fresh, self.error = \
                0.0, None, "0", True, None
        elif tok == "ce":
            self.entry, self.fresh, self.error = "0", True, None
        elif self.error and tok not in ("MC",):
            pass                     # error latches until C/CE clears it
        elif tok.isdigit():
            self._digit(tok)
        elif tok == ".":
            if self.fresh:
                self.entry, self.fresh = "0.", False
            elif "." not in self.entry:
                self.entry += "."
        elif tok == "bs":
            if not self.fresh:
                self.entry = self.entry[:-1] or "0"
                if self.entry in ("", "-"):
                    self.entry = "0"
        elif tok == "+/-":
            if self.entry not in ("0", "0."):
                self.entry = self.entry[1:] if self.entry.startswith("-") \
                    else "-" + self.entry
        elif tok in ("+", "-", "*", "/"):
            self._op(tok)
        elif tok == "=":
            self._equals()
        elif tok == "sqrt":
            self._unary(lambda v: math.sqrt(v) if v >= 0 else None, _BADIN)
        elif tok == "1/x":
            self._unary(lambda v: (1.0 / v) if v else None, _DIV0)
        elif tok == "%":
            self._set(self.acc * self._val() / 100.0)
        elif tok == "MC":
            self.mem = 0.0
        elif tok == "MR":
            self._set(self.mem)
        elif tok == "MS":
            self.mem = self._val()
        elif tok == "M+":
            self.mem += self._val()
        self.invalidate()

    def _digit(self, ch):
        if self.fresh or self.entry == "0":
            self.entry = ch
        elif len(self.entry) < 16:
            self.entry += ch
        self.fresh = False

    def _op(self, newop):
        cur = self._val()
        if self.op is not None and not self.fresh:
            r = _apply(self.acc, self.op, cur)
            if r is None:
                return self._fail(_DIV0)
            self.acc = r
            self.entry = _fmt(r)
        else:
            self.acc = cur
        self.op, self.fresh = newop, True

    def _equals(self):
        if self.op is None:
            self.fresh = True
            return
        r = _apply(self.acc, self.op, self._val())
        if r is None:
            return self._fail(_DIV0)
        self.acc, self.op, self.fresh = r, None, True
        self.entry = _fmt(r)

    def _unary(self, fn, err):
        r = fn(self._val())
        if r is None:
            return self._fail(err)
        self._set(r)

    def _set(self, v):
        self.entry, self.fresh = _fmt(v), True

    def _fail(self, msg):
        self.error, self.op, self.fresh = msg, None, True

    def _val(self):
        try:
            return float(self.entry)
        except ValueError:
            return 0.0

    # ── keyboard ────────────────────────────────────────────────────────────
    def on_key(self, ev):
        t = ev.text
        if t and t in "0123456789":
            self._press(t)
        elif t == ".":
            self._press(".")
        elif t in ("+", "-", "*", "/"):
            self._press(t)
        elif t == "%":
            self._press("%")
        elif ev.key == "Enter" or t == "=":
            self._press("=")
        elif ev.key == "Escape":
            self._press("c")
        elif ev.key == "Backspace":
            self._press("bs")
        else:
            return super().on_key(ev)
        return True


def _apply(a, op, b):
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        return None if b == 0 else a / b
    return b


def _fmt(v):
    if v != v or v in (float("inf"), float("-inf")):
        return _DIV0
    if v == int(v) and abs(v) < 1e16:
        return str(int(v))
    return f"{v:.12g}"
