"""kilix desktop — the widget toolkit.

Retained widgets drawn with PIL onto a window's client surface. Coordinates
are client-relative; the window translates global mouse events before
dispatch and captures the pressed widget until release, so every widget gets
drag semantics for free.

Popups (menus, dropdowns, context menus, the Start menu) are windows-free:
they live in the Desk's MenuHost, drawn on top of the whole framebuffer and
given first refusal on input while open.
"""
from PIL import Image, ImageDraw

import icons
import theme as T


def drawer(img):
    d = ImageDraw.Draw(img)
    d.fontmode = "1"          # binary rendering: crisp Win95 text, no AA
    return d


class Ev:
    """Normalized input event. mouse: x/y/btn/press/move/wheel/clicks/mods.
    key: key/text/ctrl/alt/shift."""

    def __init__(self, **kw):
        self.kind = kw.get("kind")
        self.x = kw.get("x", 0)
        self.y = kw.get("y", 0)
        self.btn = kw.get("btn", 0)          # 1 left, 2 middle, 3 right
        self.press = kw.get("press", False)
        self.move = kw.get("move", False)
        self.wheel = kw.get("wheel", 0)      # -1 up, +1 down
        self.clicks = kw.get("clicks", 1)
        self.key = kw.get("key", "")
        self.text = kw.get("text", "")
        self.ctrl = kw.get("ctrl", False)
        self.alt = kw.get("alt", False)
        self.shift = kw.get("shift", False)

    def at(self, dx, dy):
        e = Ev(**self.__dict__)
        e.kind = self.kind
        e.x, e.y = self.x - dx, self.y - dy
        return e


# ── base ────────────────────────────────────────────────────────────────────

class Widget:
    focusable = False

    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.window = None
        self.visible = True
        self.enabled = True

    def hit(self, px, py):
        return (self.visible and self.x <= px < self.x + self.w
                and self.y <= py < self.y + self.h)

    def invalidate(self):
        if self.window:
            self.window.invalidate()

    @property
    def desk(self):
        return self.window.desk if self.window else None

    def draw(self, d, img):
        pass

    def on_mouse(self, ev):
        return False

    def on_key(self, ev):
        return False

    def on_focus(self, got):
        pass


class Label(Widget):
    def __init__(self, x, y, text, font=None, color=T.TEXT, bold=False):
        self.font = T.BOLD if bold else (font or T.FONT)
        super().__init__(x, y, T.text_w(self.font, text), 14)
        self.text = text
        self.color = color

    def set(self, text):
        self.text = text
        self.w = T.text_w(self.font, text)
        self.invalidate()

    def draw(self, d, img):
        d.text((self.x, self.y), self.text, font=self.font, fill=self.color)


class Button(Widget):
    focusable = True

    def __init__(self, x, y, w, h, text="", cb=None, icon=None, default=False):
        super().__init__(x, y, w, h)
        self.text, self.cb, self.icon = text, cb, icon
        self.default = default        # heavier border (dialog default button)
        self.down = False             # currently held
        self.toggled = False          # sticky pressed look (taskbar buttons)

    def draw(self, d, img):
        x0, y0 = self.x, self.y
        x1, y1 = x0 + self.w - 1, y0 + self.h - 1
        if self.default:
            d.rectangle([x0, y0, x1, y1], outline=T.DKSHADOW)
            x0, y0, x1, y1 = x0 + 1, y0 + 1, x1 - 1, y1 - 1
        pushed = self.down or self.toggled
        if pushed:
            T.pressed(d, x0, y0, x1, y1)
        else:
            T.raised(d, x0, y0, x1, y1)
        off = 1 if pushed else 0
        tw = T.text_w(T.FONT, self.text)
        cx = x0 + (x1 - x0 + 1 - tw - (20 if self.icon else 0)) // 2
        if self.icon:
            icons.paint(img, self.icon, cx + off,
                        y0 + (y1 - y0 + 1 - 16) // 2 + off, 16)
            cx += 20
        col = T.TEXT if self.enabled else T.DISABLED
        d.text((cx + off, y0 + (y1 - y0 + 1 - 13) // 2 + off), self.text,
               font=T.FONT, fill=col)
        if self.window and self.window.focus is self:
            T.focus_rect(d, x0 + 3, y0 + 3, x1 - 3, y1 - 3)

    def on_mouse(self, ev):
        if not self.enabled:
            return True
        if ev.press and ev.btn == 1:
            self.down = True
            self.invalidate()
        elif ev.move and self.down:
            inside = self.hit(ev.x, ev.y)
            if inside != self.down:
                pass
        elif not ev.press and not ev.move and ev.btn == 1 and self.down:
            self.down = False
            self.invalidate()
            if self.hit(ev.x, ev.y) and self.cb:
                self.cb()
        return True

    def on_key(self, ev):
        if ev.key in ("Enter", " ") and self.cb:
            self.cb()
            return True
        return False


class Checkbox(Widget):
    focusable = True

    def __init__(self, x, y, text, checked=False, cb=None):
        super().__init__(x, y, 17 + T.text_w(T.FONT, text), 14)
        self.text, self.checked, self.cb = text, checked, cb

    def draw(self, d, img):
        bx = self.x
        by = self.y + (self.h - 12) // 2
        T.sunken(d, bx, by, bx + 11, by + 11,
                 fill=T.WINDOW_BG if self.enabled else T.FACE)
        if self.checked:
            for i in range(3):
                d.line([(bx + 3 + i, by + 5 + i), (bx + 3 + i, by + 7 + i)],
                       fill=T.TEXT)
            for i in range(4):
                d.line([(bx + 6 + i, by + 6 - i), (bx + 6 + i, by + 8 - i)],
                       fill=T.TEXT)
        d.text((self.x + 17, self.y + 1), self.text, font=T.FONT,
               fill=T.TEXT if self.enabled else T.DISABLED)

    def on_mouse(self, ev):
        if ev.press and ev.btn == 1 and self.enabled:
            self.checked = not self.checked
            self.invalidate()
            if self.cb:
                self.cb(self.checked)
        return True

    def on_key(self, ev):
        if ev.key == " ":
            self.checked = not self.checked
            self.invalidate()
            if self.cb:
                self.cb(self.checked)
            return True
        return False


class GroupBox(Widget):
    def __init__(self, x, y, w, h, label=""):
        super().__init__(x, y, w, h)
        self.label = label

    def draw(self, d, img):
        T.groove(d, self.x, self.y + 6, self.x + self.w - 1,
                 self.y + self.h - 1)
        if self.label:
            tw = T.text_w(T.FONT, self.label)
            d.rectangle([self.x + 8, self.y, self.x + 14 + tw, self.y + 12],
                        fill=T.FACE)
            d.text((self.x + 11, self.y), self.label, font=T.FONT, fill=T.TEXT)


# ── scrollbar gadget (embedded by TextArea / ListBox / IconGrid) ────────────

class VScroll:
    def __init__(self):
        self.x = self.y = self.h = 0
        self.total = self.page = self.pos = 0
        self.drag = None                       # (grab_offset_px)

    def place(self, x, y, h):
        self.x, self.y, self.h = x, y, h

    def clamp(self):
        self.pos = max(0, min(self.pos, max(0, self.total - self.page)))

    def _thumb(self):
        span = self.h - 2 * T.SCROLL_W
        if self.total <= self.page or span <= 8:
            return None
        th = max(8, span * self.page // self.total)
        ty = self.y + T.SCROLL_W + (span - th) * self.pos // max(
            1, self.total - self.page)
        return ty, th

    def hit(self, px, py):
        return (self.x <= px < self.x + T.SCROLL_W
                and self.y <= py < self.y + self.h)

    def draw(self, d):
        x0, x1 = self.x, self.x + T.SCROLL_W - 1
        # checkered track
        d.rectangle([x0, self.y, x1, self.y + self.h - 1], fill=T.LTGRAY)
        for yy in range(self.y, self.y + self.h):
            for xx in range(x0 + (yy % 2), x1 + 1, 2):
                d.point((xx, yy), fill=T.LIGHT)
        for dy, up in ((0, True), (self.h - T.SCROLL_W, False)):
            by = self.y + dy
            T.raised(d, x0, by, x1, by + T.SCROLL_W - 1)
            cx, cy = x0 + T.SCROLL_W // 2, by + T.SCROLL_W // 2
            pts = ([(cx - 3, cy + 1), (cx + 3, cy + 1), (cx, cy - 2)] if up
                   else [(cx - 3, cy - 1), (cx + 3, cy - 1), (cx, cy + 2)])
            d.polygon(pts, fill=T.TEXT if self.total > self.page else T.SHADOW)
        t = self._thumb()
        if t:
            ty, th = t
            T.raised(d, x0, ty, x1, ty + th - 1)

    def on_mouse(self, ev, line=1):
        """Returns True if the position changed (caller re-clamps/redraws)."""
        old = self.pos
        if ev.press and ev.btn == 1:
            if ev.y < self.y + T.SCROLL_W:
                self.pos -= line
            elif ev.y >= self.y + self.h - T.SCROLL_W:
                self.pos += line
            else:
                t = self._thumb()
                if t and t[0] <= ev.y < t[0] + t[1]:
                    self.drag = ev.y - t[0]
                elif t:
                    self.pos += -self.page if ev.y < t[0] else self.page
        elif ev.move and self.drag is not None:
            span = self.h - 2 * T.SCROLL_W
            t = self._thumb()
            if t:
                th = t[1]
                frac = (ev.y - self.drag - self.y - T.SCROLL_W) / max(
                    1, span - th)
                self.pos = int(frac * (self.total - self.page) + 0.5)
        elif not ev.press and not ev.move:
            self.drag = None
        self.clamp()
        return self.pos != old


# ── text editing ────────────────────────────────────────────────────────────

def _osc52(desk, text):
    if desk:
        desk.set_clipboard(text)


class TextField(Widget):
    focusable = True

    def __init__(self, x, y, w, text="", on_enter=None, on_change=None,
                 mask=False):
        super().__init__(x, y, w, 21)
        self.text = text
        self.cur = len(text)
        self.anchor = None            # selection anchor (char index) or None
        self.scroll = 0               # first visible pixel
        self.on_enter, self.on_change = on_enter, on_change
        self.mask = mask

    def _disp(self):
        """Visible value; masked fields never render their secret text."""
        return ("•" * len(self.text)) if self.mask else self.text

    # selection helpers
    def _sel(self):
        if self.anchor is None or self.anchor == self.cur:
            return None
        return min(self.anchor, self.cur), max(self.anchor, self.cur)

    def _del_sel(self):
        s = self._sel()
        if s:
            self.text = self.text[:s[0]] + self.text[s[1]:]
            self.cur = s[0]
            self.anchor = None
            return True
        return False

    def set(self, text):
        self.text = text
        self.cur = min(self.cur, len(text))
        self.anchor = None
        self.scroll = 0
        self._reveal()
        self.invalidate()

    def _x_of(self, i):
        return 4 + T.text_w(T.FONT, self._disp()[:i]) - self.scroll

    def _idx_at(self, px):
        px += self.scroll - 4
        disp = self._disp()
        for i in range(len(disp) + 1):
            if T.text_w(T.FONT, disp[:i]) >= px:
                return i
        return len(disp)

    def _reveal(self):
        cx = 4 + T.text_w(T.FONT, self._disp()[:self.cur])
        if cx - self.scroll > self.w - 8:
            self.scroll = cx - self.w + 8
        if cx - self.scroll < 4:
            self.scroll = max(0, cx - 4)

    def draw(self, d, img):
        x0, y0 = self.x, self.y
        x1, y1 = x0 + self.w - 1, y0 + self.h - 1
        bg = T.WINDOW_BG if self.enabled else T.FACE
        T.sunken(d, x0, y0, x1, y1, fill=bg)
        # render into an interior-sized strip so scrolled text can't bleed
        # past the box onto neighbouring widgets
        iw, ih = self.w - 4, self.h - 4
        ty = (ih - 13) // 2
        strip = Image.new("RGB", (iw, ih), bg)
        sd = drawer(strip)
        ox = 2 - self.scroll                      # x of text[0] within strip
        s = self._sel()
        focused = self.window and self.window.focus is self
        disp = self._disp()
        if s and focused:
            sx0 = ox + T.text_w(T.FONT, disp[:s[0]])
            sx1 = ox + T.text_w(T.FONT, disp[:s[1]])
            sd.rectangle([max(0, sx0), ty - 1, min(iw, sx1), ty + 13],
                         fill=T.SEL_BG)
        sd.text((ox, ty), disp, font=T.FONT, fill=T.TEXT)
        if s and focused:
            sd.text((ox + T.text_w(T.FONT, disp[:s[0]]), ty),
                    disp[s[0]:s[1]], font=T.FONT, fill=T.SEL_TX)
        img.paste(strip, (x0 + 2, y0 + 2))
        if focused and self.window.caret_on:
            cx = x0 + self._x_of(self.cur)
            if x0 + 2 <= cx <= x1 - 2:
                cy = y0 + 2 + ty
                d.line([(cx, cy - 1), (cx, cy + 13)], fill=T.TEXT)

    def on_mouse(self, ev):
        if ev.press and ev.btn == 1:
            self.cur = self._idx_at(ev.x - self.x)
            self.anchor = self.cur if ev.clicks == 1 else None
            if ev.clicks == 2:                       # double-click: select all
                self.anchor, self.cur = 0, len(self.text)
            self.invalidate()
        elif ev.move and (ev.btn & 1):
            self.cur = self._idx_at(ev.x - self.x)
            self._reveal()
            self.invalidate()
        return True

    def on_key(self, ev):
        k, changed = ev.key, False
        sel_mod = ev.shift
        if k == "ArrowLeft":
            self._move(self.cur - 1, sel_mod)
        elif k == "ArrowRight":
            self._move(self.cur + 1, sel_mod)
        elif k == "Home":
            self._move(0, sel_mod)
        elif k == "End":
            self._move(len(self.text), sel_mod)
        elif k == "Backspace":
            changed = self._del_sel()
            if not changed and self.cur > 0:
                self.text = self.text[:self.cur - 1] + self.text[self.cur:]
                self.cur -= 1
                changed = True
        elif k == "Delete":
            changed = self._del_sel()
            if not changed and self.cur < len(self.text):
                self.text = self.text[:self.cur] + self.text[self.cur + 1:]
                changed = True
        elif ev.ctrl and k == "a":
            self.anchor, self.cur = 0, len(self.text)
        elif ev.ctrl and k in ("c", "x"):
            s = self._sel()
            if s:
                if not self.mask:
                    _osc52(self.desk, self.text[s[0]:s[1]])
                if k == "x":
                    changed = self._del_sel()
        elif ev.ctrl and k == "v":
            changed = self.insert(self.desk.clipboard if self.desk else "")
        elif k == "Enter":
            if self.on_enter:
                self.on_enter(self.text)
        elif k == "Tab":
            return False                       # let the window cycle focus
        elif ev.text and not ev.ctrl and not ev.alt:
            changed = self.insert(ev.text)
        else:
            return False
        self._reveal()
        self.invalidate()
        if changed and self.on_change:
            self.on_change(self.text)
        return True

    def insert(self, s):
        s = s.replace("\n", " ").replace("\r", "")
        if not s:
            return False
        self._del_sel()
        self.text = self.text[:self.cur] + s + self.text[self.cur:]
        self.cur += len(s)
        return True

    def _move(self, to, keep_sel):
        to = max(0, min(to, len(self.text)))
        if keep_sel:
            if self.anchor is None:
                self.anchor = self.cur
        else:
            self.anchor = None
        self.cur = to


class TextArea(Widget):
    """Multi-line editor: mono font, selection, clipboard, scrollbar."""
    focusable = True
    LH = 15                       # line height

    def __init__(self, x, y, w, h, text=""):
        super().__init__(x, y, w, h)
        self.lines = text.split("\n") or [""]
        self.cr = self.cc = 0                 # cursor row/col
        self.anchor = None                    # (row, col) or None
        self.goal_col = 0
        self.hx = 0                           # horizontal scroll (pixels)
        self.sb = VScroll()
        self.on_change = None
        self.font = T.FONT
        self._cw = None

    def set_text(self, text):
        self.lines = text.split("\n") or [""]
        self.cr = self.cc = 0
        self.anchor = None
        self.sb.pos = 0
        self.hx = 0
        self.invalidate()

    def text(self):
        return "\n".join(self.lines)

    # geometry
    def _rows(self):
        return max(1, (self.h - 4) // self.LH)

    def _col_at(self, row, px):
        s = self.lines[row]
        for i in range(len(s) + 1):
            if T.text_w(self.font, s[:i]) >= px - 1:
                return i
        return len(s)

    def _sel(self):
        if self.anchor is None or self.anchor == (self.cr, self.cc):
            return None
        a, b = sorted([self.anchor, (self.cr, self.cc)])
        return a, b

    def _reveal(self):
        self.sb.total, self.sb.page = len(self.lines), self._rows()
        if self.cr < self.sb.pos:
            self.sb.pos = self.cr
        if self.cr >= self.sb.pos + self._rows():
            self.sb.pos = self.cr - self._rows() + 1
        self.sb.clamp()
        maxw = self.w - T.SCROLL_W - 10
        cx = 4 + T.text_w(self.font, self.lines[self.cr][:self.cc])
        if cx - self.hx > maxw:
            self.hx = cx - maxw
        if cx - self.hx < 4:
            self.hx = max(0, cx - 4)

    def _span(self, s, maxw):
        """Visible [a, b) char range of s at horizontal offset self.hx —
        binary-searched so a long line costs O(log n) width probes, not O(n)."""
        n = len(s)
        if not n:
            return 0, 0
        f, W = self.font, T.text_w
        lo, hi = 0, n
        while lo < hi:                        # first char whose right edge > hx
            m = (lo + hi) // 2
            if W(f, s[:m + 1]) > self.hx:
                hi = m
            else:
                lo = m + 1
        a, right = lo, self.hx + maxw
        lo, hi = a, n
        while lo < hi:                        # first char at or past the right
            m = (lo + hi) // 2
            if W(f, s[:m]) >= right:
                hi = m
            else:
                lo = m + 1
        return a, lo

    def draw(self, d, img):
        x0, y0 = self.x, self.y
        x1, y1 = x0 + self.w - 1, y0 + self.h - 1
        T.sunken(d, x0, y0, x1, y1)
        self.sb.total, self.sb.page = len(self.lines), self._rows()
        self.sb.clamp()
        self.sb.place(x1 - T.SCROLL_W + 1 - 2, y0 + 2, self.h - 4)
        sel = self._sel()
        focused = self.window and self.window.focus is self
        tx = x0 + 4
        maxw = self.w - T.SCROLL_W - 10
        W = T.text_w
        for i in range(self._rows()):
            row = self.sb.pos + i
            if row >= len(self.lines):
                break
            yy = y0 + 3 + i * self.LH
            s = self.lines[row]
            a, b = self._span(s, maxw)            # visible pixel window slice
            sel_row = None
            if sel:
                (ar, ac), (br, bc) = sel
                if ar <= row <= br:
                    c0 = ac if row == ar else 0
                    c1 = bc if row == br else len(s)
                    sel_row = (c0, c1, row < br)
            # render the line into an interior-width strip; anything outside is
            # clipped by the strip, so horizontal scroll can't bleed sideways
            strip = Image.new("RGB", (maxw, self.LH), T.WINDOW_BG)
            sd = drawer(strip)
            if sel_row:
                c0, c1, nl = sel_row
                sx0 = W(self.font, s[:c0]) - self.hx
                sx1 = W(self.font, s[:c1]) - self.hx + (4 if nl else 0)
                sx0, sx1 = max(0, sx0), min(maxw, sx1)
                if sx1 > sx0:
                    sd.rectangle([sx0, 0, sx1 - 1, self.LH - 1], fill=T.SEL_BG)
            sd.text((W(self.font, s[:a]) - self.hx, 1), s[a:b],
                    font=self.font, fill=T.TEXT)
            if sel_row:
                d0, d1 = max(a, c0), min(b, c1)
                if d1 > d0:
                    sd.text((W(self.font, s[:d0]) - self.hx, 1), s[d0:d1],
                            font=self.font, fill=T.SEL_TX)
            img.paste(strip, (tx, yy - 1))
            if focused and self.window.caret_on and row == self.cr:
                cx = tx + W(self.font, s[:self.cc]) - self.hx
                if tx <= cx <= tx + maxw:
                    d.line([(cx, yy - 1), (cx, yy + self.LH - 2)], fill=T.TEXT)
        self.sb.draw(d)

    # editing ops
    def _del_sel(self):
        sel = self._sel()
        if not sel:
            return False
        (ar, ac), (br, bc) = sel
        self.lines[ar:br + 1] = [self.lines[ar][:ac] + self.lines[br][bc:]]
        self.cr, self.cc = ar, ac
        self.anchor = None
        return True

    def _sel_text(self):
        sel = self._sel()
        if not sel:
            return ""
        (ar, ac), (br, bc) = sel
        if ar == br:
            return self.lines[ar][ac:bc]
        mid = self.lines[ar + 1:br]
        return "\n".join([self.lines[ar][ac:]] + mid + [self.lines[br][:bc]])

    def insert(self, s):
        self._del_sel()
        s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
        parts = s.split("\n")
        line = self.lines[self.cr]
        head, tail = line[:self.cc], line[self.cc:]
        if len(parts) == 1:
            self.lines[self.cr] = head + parts[0] + tail
            self.cc += len(parts[0])
        else:
            self.lines[self.cr:self.cr + 1] = (
                [head + parts[0]] + parts[1:-1] + [parts[-1] + tail])
            self.cr += len(parts) - 1
            self.cc = len(parts[-1])
        self._changed()

    def _changed(self):
        if self.on_change:
            self.on_change()

    def _move(self, r, c, keep):
        r = max(0, min(r, len(self.lines) - 1))
        c = max(0, min(c, len(self.lines[r])))
        if keep:
            if self.anchor is None:
                self.anchor = (self.cr, self.cc)
        else:
            self.anchor = None
        self.cr, self.cc = r, c

    def on_mouse(self, ev):
        lx, ly = ev.x - self.x, ev.y - self.y
        if self.sb.hit(ev.x, ev.y) or self.sb.drag is not None:
            if self.sb.on_mouse(ev, line=1):
                self.invalidate()
            if ev.press or self.sb.drag is not None:
                return True
        if ev.wheel:
            self.sb.total, self.sb.page = len(self.lines), self._rows()
            self.sb.pos += ev.wheel * 3
            self.sb.clamp()
            self.invalidate()
            return True
        row = self.sb.pos + max(0, (ly - 3) // self.LH)
        row = min(row, len(self.lines) - 1)
        col = self._col_at(row, lx - 4 + self.hx)
        if ev.press and ev.btn == 1:
            self._move(row, col, ev.shift)
            if not ev.shift:
                self.anchor = (row, col)
            self.goal_col = self.cc
            self.invalidate()
        elif ev.move and (ev.btn & 1):
            self.cr, self.cc = row, col
            self.goal_col = self.cc
            self._reveal()
            self.invalidate()
        elif not ev.press and not ev.move and self.anchor == (self.cr, self.cc):
            self.anchor = None
        return True

    def on_key(self, ev):
        k = ev.key
        rows = self._rows()
        if k == "ArrowLeft":
            if self.cc > 0:
                self._move(self.cr, self.cc - 1, ev.shift)
            elif self.cr > 0:
                self._move(self.cr - 1, len(self.lines[self.cr - 1]), ev.shift)
        elif k == "ArrowRight":
            if self.cc < len(self.lines[self.cr]):
                self._move(self.cr, self.cc + 1, ev.shift)
            elif self.cr < len(self.lines) - 1:
                self._move(self.cr + 1, 0, ev.shift)
        elif k in ("ArrowUp", "ArrowDown"):
            step = -1 if k == "ArrowUp" else 1
            self._move(self.cr + step, self.goal_col, ev.shift)
        elif k in ("PageUp", "PageDown"):
            step = -rows if k == "PageUp" else rows
            self._move(self.cr + step, self.goal_col, ev.shift)
        elif k == "Home":
            self._move(self.cr, 0, ev.shift)
        elif k == "End":
            self._move(self.cr, len(self.lines[self.cr]), ev.shift)
        elif ev.ctrl and k == "a":
            self.anchor = (0, 0)
            self.cr = len(self.lines) - 1
            self.cc = len(self.lines[-1])
        elif ev.ctrl and k in ("c", "x"):
            t = self._sel_text()
            if t:
                _osc52(self.desk, t)
                if k == "x":
                    self._del_sel()
                    self._changed()
        elif ev.ctrl and k == "v":
            self.insert(self.desk.clipboard if self.desk else "")
        elif k == "Enter":
            self.insert("\n")
        elif k == "Tab":
            self.insert("    ")
        elif k == "Backspace":
            did = self._del_sel()
            if not did:
                if self.cc > 0:
                    ln = self.lines[self.cr]
                    self.lines[self.cr] = ln[:self.cc - 1] + ln[self.cc:]
                    self.cc -= 1
                    did = True
                elif self.cr > 0:
                    self.cc = len(self.lines[self.cr - 1])
                    self.lines[self.cr - 1] += self.lines.pop(self.cr)
                    self.cr -= 1
                    did = True
            if did:
                self._changed()
        elif k == "Delete":
            did = self._del_sel()
            if not did:
                ln = self.lines[self.cr]
                if self.cc < len(ln):
                    self.lines[self.cr] = ln[:self.cc] + ln[self.cc + 1:]
                    did = True
                elif self.cr < len(self.lines) - 1:
                    self.lines[self.cr] += self.lines.pop(self.cr + 1)
                    did = True
            if did:
                self._changed()
        elif ev.text and not ev.ctrl and not ev.alt:
            self.insert(ev.text)
        else:
            return False
        if k not in ("ArrowUp", "ArrowDown", "PageUp", "PageDown"):
            self.goal_col = self.cc
        self._reveal()
        self.invalidate()
        return True


# ── lists & grids ───────────────────────────────────────────────────────────

class ListBox(Widget):
    focusable = True
    RH = 17

    def __init__(self, x, y, w, h, items=None, on_activate=None,
                 on_select=None, on_context=None):
        super().__init__(x, y, w, h)
        self.items = items or []      # (icon_name_or_None, text, data)
        self.sel = -1
        self.sb = VScroll()
        self.on_activate, self.on_select = on_activate, on_select
        self.on_context = on_context

    def set_items(self, items, keep_sel=False):
        old = self.items[self.sel][1] if 0 <= self.sel < len(self.items) else None
        self.items = items
        self.sel = -1
        if keep_sel and old is not None:
            for i, it in enumerate(items):
                if it[1] == old:
                    self.sel = i
                    break
        self.sb.pos = 0 if not keep_sel else self.sb.pos
        self.invalidate()

    def _rows(self):
        return max(1, (self.h - 4) // self.RH)

    def draw(self, d, img):
        x0, y0 = self.x, self.y
        x1, y1 = x0 + self.w - 1, y0 + self.h - 1
        T.sunken(d, x0, y0, x1, y1)
        self.sb.total, self.sb.page = len(self.items), self._rows()
        self.sb.clamp()
        self.sb.place(x1 - T.SCROLL_W - 1, y0 + 2, self.h - 4)
        for i in range(self._rows()):
            idx = self.sb.pos + i
            if idx >= len(self.items):
                break
            ic, text, _ = self.items[idx]
            yy = y0 + 2 + i * self.RH
            tw_max = self.w - 26 - (T.SCROLL_W if self.sb.total > self.sb.page
                                    else 0)
            if idx == self.sel:
                d.rectangle([x0 + 2, yy, x1 - 2 - (
                    T.SCROLL_W if self.sb.total > self.sb.page else 0),
                    yy + self.RH - 1], fill=T.SEL_BG)
            tx = x0 + 4
            if ic:
                icons.paint(img, ic, tx, yy, 16)
                tx += 20
            d.text((tx, yy + 2), T.ellipsize(T.FONT, text, tw_max),
                   font=T.FONT,
                   fill=T.SEL_TX if idx == self.sel else T.TEXT)
        if self.sb.total > self.sb.page:
            self.sb.draw(d)

    def on_mouse(self, ev):
        if (self.sb.total > self.sb.page
                and (self.sb.hit(ev.x, ev.y) or self.sb.drag is not None)):
            if self.sb.on_mouse(ev):
                self.invalidate()
            if ev.press or self.sb.drag is not None:
                return True
        if ev.wheel:
            self.sb.pos += ev.wheel * 3
            self.sb.clamp()
            self.invalidate()
            return True
        rel = ev.y - self.y - 2                # only the drawn rows are hits:
        r = rel // self.RH if rel >= 0 else -1  # reject top bevel + partial tail
        idx = self.sb.pos + r
        valid = 0 <= r < self._rows() and 0 <= idx < len(self.items)
        if ev.press and ev.btn in (1, 3):
            self.sel = idx if valid else -1
            self.invalidate()
            if self.on_select and valid:
                self.on_select(self.items[idx])
            if ev.btn == 3 and self.on_context:
                self.on_context(self.items[idx] if valid else None, ev)
            elif ev.btn == 1 and ev.clicks == 2 and valid and self.on_activate:
                self.on_activate(self.items[idx])
        return True

    def on_key(self, ev):
        if not self.items:
            return False
        if ev.key in ("ArrowUp", "ArrowDown"):
            step = -1 if ev.key == "ArrowUp" else 1
            self.sel = max(0, min(len(self.items) - 1, self.sel + step))
            if self.sel < self.sb.pos:
                self.sb.pos = self.sel
            if self.sel >= self.sb.pos + self._rows():
                self.sb.pos = self.sel - self._rows() + 1
            self.invalidate()
            if self.on_select:
                self.on_select(self.items[self.sel])
            return True
        if ev.key == "Enter" and 0 <= self.sel < len(self.items):
            if self.on_activate:
                self.on_activate(self.items[self.sel])
            return True
        return False


class IconGrid(Widget):
    """Large-icon view: desktop surface and the file manager both use it.
    Items: dicts {label, icon, data, shortcut}. Column-major on the desktop
    (fill top-to-bottom like Win95), row-major in windows."""
    focusable = True

    def __init__(self, x, y, w, h, on_activate=None, on_context=None,
                 desktop=False):
        super().__init__(x, y, w, h)
        self.items = []
        self.sel = set()
        self.desktop = desktop
        self.on_activate, self.on_context = on_activate, on_context
        self.sb = VScroll()
        self.band = None              # rubber band (x0, y0, x1, y1)
        self._press_item = None
        self.label_fg = T.LIGHT if desktop else T.TEXT
        self.bg = None if desktop else T.WINDOW_BG

    def set_items(self, items):
        self.items = items
        self.sel.clear()
        self.invalidate()

    # layout
    def _grid(self):
        if self.desktop:
            per_col = max(1, (self.h - 4) // T.CELL_H)
            return per_col, None
        per_row = max(1, (self.w - T.SCROLL_W - 8) // T.CELL_W)
        return None, per_row

    def _cell(self, i):
        per_col, per_row = self._grid()
        if self.desktop:
            c, r = divmod(i, per_col)
            return self.x + 4 + c * T.CELL_W, self.y + 4 + r * T.CELL_H
        r, c = divmod(i, per_row)
        return (self.x + 4 + c * T.CELL_W,
                self.y + 4 + (r - self.sb.pos) * T.CELL_H)

    def _rows_total(self):
        _, per_row = self._grid()
        if per_row:
            return (len(self.items) + per_row - 1) // per_row
        return 0

    def _item_at(self, px, py):
        for i in range(len(self.items)):
            cx, cy = self._cell(i)
            if cx <= px < cx + T.CELL_W and cy <= py < cy + T.CELL_H:
                return i
        return None

    def draw(self, d, img):
        if not self.desktop:
            # windows clip their icon grid: render to an own surface so
            # partial rows can't bleed over the widgets below (status bar)
            surf = Image.new("RGB", (max(1, self.w), max(1, self.h)),
                             self.bg or T.WINDOW_BG)
            sd = drawer(surf)
            ox, oy = self.x, self.y
            try:
                self.x = self.y = 0
                self._draw_into(sd, surf)
            finally:
                self.x, self.y = ox, oy
            img.paste(surf, (ox, oy))
        else:
            self._draw_into(d, img)
        if self.band:
            x0, y0, x1, y1 = self.band
            gx1, gy1 = self.x + self.w - 1, self.y + self.h - 1
            T.focus_rect(d, max(self.x, min(x0, x1)),
                         max(self.y, min(y0, y1)),
                         min(gx1, max(x0, x1)), min(gy1, max(y0, y1)),
                         on=self.label_fg, off=self.bg or T.DESKTOP)

    def _draw_into(self, d, img):
        if not self.desktop:
            T.sunken(d, self.x, self.y, self.x + self.w - 1,
                     self.y + self.h - 1, fill=self.bg)
            self.sb.total = self._rows_total()
            self.sb.page = max(1, (self.h - 8) // T.CELL_H)
            self.sb.clamp()
            self.sb.place(self.x + self.w - T.SCROLL_W - 2, self.y + 2,
                          self.h - 4)
        for i, it in enumerate(self.items):
            cx, cy = self._cell(i)
            if cy + T.CELL_H < self.y or cy > self.y + self.h:
                continue
            self._draw_item(d, img, it, cx, cy, i in self.sel)
        if not self.desktop and self.sb.total > self.sb.page:
            self.sb.draw(d)

    def _draw_item(self, d, img, it, cx, cy, selected):
        ix = cx + (T.CELL_W - T.ICON) // 2
        icons.paint(img, it.get("icon", "doc"), ix, cy + 2, T.ICON,
                    shortcut=it.get("shortcut", False))
        if selected:                    # navy tint over the icon's own pixels
            ic = icons.get(it.get("icon", "doc"), T.ICON,
                           it.get("shortcut", False))
            tint = ic.copy()
            px = tint.load()
            for yy in range(T.ICON):
                for xx in range(T.ICON):
                    r, g, b, a = px[xx, yy]
                    if a and (xx + yy) % 2 == 0:
                        px[xx, yy] = (T.SEL_BG[0], T.SEL_BG[1], T.SEL_BG[2], a)
            img.paste(tint, (ix, cy + 2), tint)
        label = it.get("label", "")
        lines = self._wrap(label)
        ty = cy + 2 + T.ICON + 3
        for ln in lines:
            tw = T.text_w(T.FONT, ln)
            tx = cx + (T.CELL_W - tw) // 2
            if selected:
                d.rectangle([tx - 2, ty - 1, tx + tw + 1, ty + 12],
                            fill=T.SEL_BG)
            elif not self.desktop:
                pass
            d.text((tx, ty), ln, font=T.FONT,
                   fill=T.SEL_TX if selected else self.label_fg)
            ty += 13

    def _wrap(self, label):
        maxw = T.CELL_W - 6
        if T.text_w(T.FONT, label) <= maxw:
            return [label]
        # break at the last space that fits, else hard split
        cut = len(label)
        while cut and T.text_w(T.FONT, label[:cut]) > maxw:
            cut -= 1
        sp = label.rfind(" ", 0, cut + 1)
        if sp > 0:
            first, rest = label[:sp], label[sp + 1:]
        else:
            first, rest = label[:cut], label[cut:]
        return [first, T.ellipsize(T.FONT, rest, maxw)]

    def on_mouse(self, ev):
        if not self.desktop and self.sb.total > self.sb.page:
            sev = ev.at(self.x, self.y)      # sb geometry is in surface coords
            if self.sb.hit(sev.x, sev.y) or self.sb.drag is not None:
                if self.sb.on_mouse(sev):
                    self.invalidate()
                if ev.press or self.sb.drag is not None:
                    return True
        if ev.wheel and not self.desktop:
            self.sb.pos += ev.wheel
            self.sb.clamp()
            self.invalidate()
            return True
        i = self._item_at(ev.x, ev.y)
        if ev.press and ev.btn in (1, 3):
            if i is not None:
                if ev.ctrl and ev.btn == 1:
                    self.sel.symmetric_difference_update({i})
                elif i not in self.sel:
                    self.sel = {i}
                self._press_item = i
            else:
                if not ev.ctrl:
                    self.sel.clear()
                if ev.btn == 1:
                    self.band = (ev.x, ev.y, ev.x, ev.y)
            self.invalidate()
            if ev.btn == 3 and self.on_context:
                self.band = None
                self.on_context(self.items[i] if i is not None else None, ev)
            elif (ev.btn == 1 and ev.clicks == 2 and i is not None
                  and self.on_activate):
                self.on_activate(self.items[i])
        elif ev.move and self.band:
            x0, y0 = self.band[0], self.band[1]
            self.band = (x0, y0, ev.x, ev.y)
            bx0, bx1 = min(x0, ev.x), max(x0, ev.x)
            by0, by1 = min(y0, ev.y), max(y0, ev.y)
            self.sel = {j for j in range(len(self.items))
                        if self._overlap(j, bx0, by0, bx1, by1)}
            self.invalidate()
        elif not ev.press and not ev.move:
            if self.band:
                self.band = None
                self.invalidate()
            self._press_item = None
        return True

    def _overlap(self, i, x0, y0, x1, y1):
        cx, cy = self._cell(i)
        return not (cx + T.CELL_W < x0 or cx > x1
                    or cy + T.CELL_H < y0 or cy > y1)

    def selected_items(self):
        return [self.items[i] for i in sorted(self.sel)
                if 0 <= i < len(self.items)]

    def on_key(self, ev):
        if ev.key == "Enter" and self.sel and self.on_activate:
            self.on_activate(self.items[sorted(self.sel)[0]])
            return True
        if ev.ctrl and ev.key == "a":
            self.sel = set(range(len(self.items)))
            self.invalidate()
            return True
        return False


# ── tabs ────────────────────────────────────────────────────────────────────

class TabBar(Widget):
    H = 21
    PAD = 18          # roomy default: 9px of air either side of the label
    MIN_PAD = 6       # below this the labels start to touch their borders

    def __init__(self, x, y, w, tabs, cb=None):
        super().__init__(x, y, w, self.H)
        self.tabs = tabs
        self.active = 0
        self.cb = cb

    def _tab_widths(self):
        """Per-tab widths, compressed so the strip always fits its own width.

        The strip is one unclipped row: a tab drawn past ``self.w`` is not just
        cosmetically clipped, it becomes unreachable, because Widget.hit() stops
        at the widget's edge and never routes the click here. Padding is
        therefore squeezed before that can happen. draw() and _tab_at() both
        read this one function so a click can never land on a different tab than
        the one under the pointer.
        """
        text = [T.text_w(T.FONT, label) for label in self.tabs]
        avail = max(0, self.w - 4)
        pad = self.PAD
        if sum(text) + pad * len(text) > avail and self.tabs:
            # Spend whatever room is left on padding, down to the floor.
            pad = max(self.MIN_PAD, (avail - sum(text)) // len(self.tabs))
        return [tw + pad for tw in text]

    def draw(self, d, img):
        tx = self.x + 2
        widths = self._tab_widths()
        bottom = self.y + self.H - 1
        d.line([(self.x, bottom - 1), (self.x + self.w - 1, bottom - 1)],
               fill=T.LIGHT)
        for i, label in enumerate(self.tabs):
            tw = widths[i]
            sel = i == self.active
            x0 = tx
            y0 = self.y + (0 if sel else 2)
            x1 = tx + tw - 1
            d.rectangle([x0, y0, x1, bottom], fill=T.FACE)
            d.line([(x0, bottom - (2 if sel else 0)), (x0, y0 + 2)],
                   fill=T.LIGHT)
            d.line([(x0 + 2, y0), (x1 - 2, y0)], fill=T.LIGHT)
            d.point((x0 + 1, y0 + 1), fill=T.LIGHT)
            d.line([(x1, y0 + 2), (x1, bottom - (2 if sel else 0))],
                    fill=T.DKSHADOW)
            d.point((x1 - 1, y0 + 1), fill=T.DKSHADOW)
            d.line([(x1 - 1, y0 + 2), (x1 - 1, bottom - (2 if sel else 0))],
                   fill=T.SHADOW)
            if sel:
                d.line([(x0 + 1, bottom - 1), (x1 - 1, bottom - 1)],
                       fill=T.FACE)
                d.line([(x0 + 1, bottom), (x1 - 1, bottom)], fill=T.FACE)
            # Centre the label in whatever width the tab ended up with, so a
            # compressed strip stays symmetric rather than left-heavy.
            d.text((tx + (tw - T.text_w(T.FONT, label)) // 2,
                    self.y + (4 if not sel else 3)), label,
                   font=T.FONT, fill=T.TEXT)
            tx += tw

    def _tab_at(self, px):
        tx = self.x + 2
        for i, tw in enumerate(self._tab_widths()):
            if tx <= px < tx + tw:
                return i
            tx += tw
        return None

    def on_mouse(self, ev):
        if ev.press and ev.btn == 1:
            i = self._tab_at(ev.x)
            if i is not None and i != self.active:
                self.active = i
                self.invalidate()
                if self.cb:
                    self.cb(i)
        return True


# ── menus ───────────────────────────────────────────────────────────────────

class MenuItem:
    def __init__(self, label, action=None, icon=None, submenu=None,
                 enabled=True, checked=False, context=None):
        self.label = label            # "-" = separator
        self.action = action
        self.icon = icon
        self.submenu = submenu        # list[MenuItem]
        self.enabled = enabled
        self.checked = checked
        self.context = context        # list[MenuItem] — right-click menu


def sep():
    return MenuItem("-")


class Menu:
    """One popup panel. The MenuHost stacks these for submenus."""
    ITEM_H = 17
    SEP_H = 8
    ARROW = 15                    # scroll-arrow strip height when overflowing

    def __init__(self, items, gx, gy, host, item_h=None, sidebar=None,
                 min_w=0, flip_x=None):
        self.items = items
        self.host = host
        self.item_h = item_h or self.ITEM_H
        self.sidebar = sidebar        # text drawn vertically in a navy band
        self.hot = -1
        self.first = 0                # first visible item (item-aligned scroll)
        pad_l = 24 if sidebar else 0
        w = min_w
        for it in items:
            iw = 22 + T.text_w(T.FONT, it.label) + 20
            iw += 12 if it.submenu else 0
            w = max(w, iw)
        self.w = w + pad_l + 4
        self.full_h = 4 + sum(self.SEP_H if it.label == "-" else self.item_h
                              for it in items)
        # keep on screen; a menu taller than the screen scrolls (F45)
        sw, sh = host.desk.size()
        avail = sh - T.TASKBAR_H
        self.scrollable = self.full_h > avail
        self.h = min(self.full_h, avail)
        self.x = max(0, min(gx, sw - self.w))
        if flip_x is not None and gx + self.w > sw:   # cascade left of parent
            self.x = max(0, flip_x - self.w)
        if self.scrollable:
            self.y = 0
        else:
            self.y = max(0, min(gy, sh - self.h))
            if gy + self.h > sh - T.TASKBAR_H:
                self.y = max(0, gy - self.h)   # open upward (start menu)

    def _pad_l(self):
        return 24 if self.sidebar else 0

    def _content(self):
        """Absolute (top, bottom) y of the scrollable item band."""
        top, bot = self.y + 2, self.y + self.h - 2
        if self.scrollable:
            top += self.ARROW
            bot -= self.ARROW
        return top, bot

    def _off(self):                   # pixel height of the items above self.first
        return sum(self.SEP_H if it.label == "-" else self.item_h
                   for it in self.items[:self.first])

    def _max_first(self):
        top, bot = self._content()
        vis = bot - top
        total, y = self.full_h - 4, 0
        for f, it in enumerate(self.items):
            if total - y <= vis:
                return f
            y += self.SEP_H if it.label == "-" else self.item_h
        return max(0, len(self.items) - 1)

    def scroll_by(self, step):
        self.first = max(0, min(self.first + step, self._max_first()))

    def _reveal(self, i):
        if not self.scrollable:
            return
        if i < self.first:
            self.first = i
        else:
            top, bot = self._content()
            vis = bot - top
            while self.first < i:
                span = sum(self.SEP_H if it.label == "-" else self.item_h
                           for it in self.items[self.first:i + 1])
                if span <= vis:
                    break
                self.first += 1
        self.first = max(0, min(self.first, self._max_first()))

    def move_hot(self, step, start=None):
        n = len(self.items)
        if not n:
            return
        i = self.hot if start is None else start
        for _ in range(n):
            i = (i + step) % n
            it = self.items[i]
            if it.label != "-" and it.enabled:
                self.hot = i
                self._reveal(i)
                return

    def item_rects(self):
        y = self._content()[0] - self._off()
        for it in self.items:
            h = self.SEP_H if it.label == "-" else self.item_h
            yield it, (self.x + 2 + self._pad_l(), y,
                       self.x + self.w - 3, y + h - 1)
            y += h

    def hit(self, gx, gy):
        return (self.x <= gx < self.x + self.w
                and self.y <= gy < self.y + self.h)

    def item_at(self, gx, gy):
        top, bot = self._content()
        if not (top <= gy < bot):
            return -1                             # frame / arrow strips are dead
        for i, (it, (x0, y0, x1, y1)) in enumerate(self.item_rects()):
            if (x0 <= gx <= x1 and y0 <= gy <= y1  # F60: bound x, not just y
                    and top <= y0 and y1 < bot     # F45: only fully-shown rows
                    and it.label != "-"):
                return i
        return -1

    def draw(self, fb, d):
        T.raised(d, self.x, self.y, self.x + self.w - 1, self.y + self.h - 1)
        if self.sidebar:
            d.rectangle([self.x + 2, self.y + 2, self.x + 2 + 21,
                         self.y + self.h - 3], fill=T.TITLE_A)
            from PIL import Image
            band = Image.new("RGB", (self.h - 6, 21), T.TITLE_A)
            bd = drawer(band)
            bd.text((6, 3), self.sidebar, font=T.BOLD, fill=T.FACE)
            band = band.transpose(Image.ROTATE_90)
            fb.paste(band, (self.x + 2, self.y + 3))
        top, bot = self._content()
        for i, (it, (x0, y0, x1, y1)) in enumerate(self.item_rects()):
            if y0 < top or y1 >= bot:          # scrolled out / partially clipped
                continue
            if it.label == "-":
                T.hsep(d, x0 + 2, x1 - 2, (y0 + y1) // 2)
                continue
            hot = i == self.hot and it.enabled
            if hot:
                d.rectangle([x0, y0, x1, y1], fill=T.SEL_BG)
            fg = (T.SEL_TX if hot else
                  (T.TEXT if it.enabled else T.DISABLED))
            tx = x0 + 22
            if it.icon:
                icons.paint(fb, it.icon, x0 + 3,
                            y0 + (self.item_h - 16) // 2, 16)
            elif it.checked:
                cx, cy = x0 + 8, y0 + self.item_h // 2
                for j in range(3):
                    d.line([(cx - 3 + j, cy + j), (cx - 3 + j, cy + j)],
                           fill=fg)
                for j in range(4):
                    d.point((cx + j, cy + 2 - j), fill=fg)
                    d.point((cx + j, cy + 1 - j), fill=fg)
            d.text((tx, y0 + (self.item_h - 13) // 2), it.label,
                   font=T.FONT, fill=fg)
            if it.submenu is not None:
                ax = x1 - 10
                ay = y0 + self.item_h // 2
                d.polygon([(ax, ay - 4), (ax, ay + 4), (ax + 4, ay)], fill=fg)
        if self.scrollable:
            cx = self.x + self.w // 2
            if self.first > 0:
                ay = self.y + 2 + self.ARROW // 2
                d.polygon([(cx - 4, ay + 2), (cx + 4, ay + 2), (cx, ay - 3)],
                          fill=T.TEXT)
            if self.first < self._max_first():
                ay = self.y + self.h - 2 - self.ARROW // 2
                d.polygon([(cx - 4, ay - 2), (cx + 4, ay - 2), (cx, ay + 3)],
                          fill=T.TEXT)


class MenuHost:
    """The desk-global stack of open popups. Gets first shot at input."""

    def __init__(self, desk):
        self.desk = desk
        self.stack = []
        self.bar = None               # a MenuBar to hover-switch across
        self._eat_release = False     # swallow the release of the opening click

    def open(self, items, gx, gy, item_h=None, sidebar=None, bar=None,
             min_w=0, flip_x=None):
        if bar is not None:
            self.bar = bar
        if not self.stack:            # the click that opens must not select
            self._eat_release = True
        m = Menu(items, gx, gy, self, item_h=item_h, sidebar=sidebar,
                 min_w=min_w, flip_x=flip_x)
        self.stack.append(m)
        self.desk.dirty = True
        return m

    def _open_submenu(self, m):
        if not (0 <= m.hot < len(m.items)):
            return
        it = m.items[m.hot]
        if it.submenu is None or not it.enabled:
            return
        while self.stack and self.stack[-1] is not m:
            self.stack.pop()
        sub = self._cascade(m, it)
        sub.move_hot(1, start=-1)
        self.desk.dirty = True

    def _cascade(self, m, it):        # open it.submenu anchored to its row in m
        rects = dict((id(x[0]), x[1]) for x in m.item_rects())
        x0, y0, x1, y1 = rects[id(it)]
        return self.open(it.submenu, x1 - 2, y0 - 2, flip_x=m.x + 2)

    def close_all(self):
        if self.stack:
            self.stack = []
            if self.bar:
                self.bar.menu_open = -1
                self.bar.invalidate()
            self.bar = None
            self.desk.dirty = True

    @property
    def active(self):
        return bool(self.stack)

    def draw(self, fb, d):
        for m in self.stack:
            m.draw(fb, d)

    def on_key(self, ev):
        if ev.key == "Escape":
            if len(self.stack) > 1:
                self.stack.pop()
                self.desk.dirty = True
            else:
                self.close_all()
            return True
        if not self.stack:
            return True
        m = self.stack[-1]            # keyboard nav (reaches scrolled tail, F45)
        if ev.key in ("ArrowDown", "ArrowUp"):
            m.move_hot(1 if ev.key == "ArrowDown" else -1)
            self.desk.dirty = True
        elif ev.key == "Home":
            m.move_hot(1, start=-1)
            self.desk.dirty = True
        elif ev.key == "End":
            m.move_hot(-1, start=len(m.items))
            self.desk.dirty = True
        elif ev.key == "ArrowRight":
            self._open_submenu(m)
        elif ev.key == "ArrowLeft":
            if len(self.stack) > 1:
                self.stack.pop()
                self.desk.dirty = True
        elif ev.key in ("Enter", " ") and 0 <= m.hot < len(m.items):
            it = m.items[m.hot]
            if not it.enabled:
                pass
            elif it.submenu is not None:
                self._open_submenu(m)
            elif it.action:
                self.close_all()
                it.action()
        return True                   # menus swallow keys while open

    def on_mouse(self, ev):
        if ev.press:
            self._eat_release = False      # a fresh press arms its own release
        # topmost menu that the pointer is over
        over = None
        for m in reversed(self.stack):
            if m.hit(ev.x, ev.y):
                over = m
                break
        if over is None:
            if ev.press:
                self.close_all()      # swallow the closing click, like Win95
                return True
            if ev.move and self.bar:
                self.bar.hover_switch(ev)
            return True
        if ev.wheel and over.scrollable:
            over.scroll_by(ev.wheel)
            self.desk.dirty = True
            return True
        i = over.item_at(ev.x, ev.y)
        if ev.press and ev.btn == 3:      # right-click opens an item's context
            it = over.items[i] if i >= 0 else None
            if it and it.enabled and it.context:
                over.hot = i
                self._eat_release = True  # swallow the matching right-release
                self.open(it.context, ev.x, ev.y)
            return True                   # a bare right-click never activates
        if ev.press and over.scrollable and i < 0:   # click a scroll arrow strip
            top, bot = over._content()
            if ev.y < top:
                over.scroll_by(-1)
                self.desk.dirty = True
            elif ev.y >= bot:
                over.scroll_by(1)
                self.desk.dirty = True
            return True
        if ev.move:
            self._eat_release = False      # a drag-select arms the release
            if i != over.hot:
                over.hot = i
                # entering an item trims deeper submenus and opens this one's
                while self.stack and self.stack[-1] is not over:
                    self.stack.pop()
                it = over.items[i] if i >= 0 else None
                if it and it.submenu is not None and it.enabled:
                    self._cascade(over, it)
                self.desk.dirty = True
        elif not ev.press:
            if self._eat_release:          # release of the opening click (F12/F44)
                self._eat_release = False
                return True                # keep the menu open, select nothing
            if ev.btn == 1 and i >= 0:
                it = over.items[i]
                if it.enabled and it.submenu is None and it.action:
                    self.close_all()
                    it.action()
        return True


class MenuBar(Widget):
    """In-window menu bar. items: [(label, lambda -> [MenuItem])]."""

    def __init__(self, window_w, items):
        super().__init__(0, 0, window_w, T.MENU_H)
        self.items = items
        self.menu_open = -1

    def _spans(self):
        x = self.x + 2
        for i, (label, _) in enumerate(self.items):
            w = T.text_w(T.FONT, label) + 14
            yield i, label, x, w
            x += w

    def draw(self, d, img):
        d.rectangle([self.x, self.y, self.x + self.w - 1,
                     self.y + self.h - 1], fill=T.FACE)
        for i, label, x, w in self._spans():
            if i == self.menu_open:
                d.rectangle([x, self.y + 1, x + w - 1, self.y + self.h - 2],
                            fill=T.SEL_BG)
            d.text((x + 7, self.y + 3), label, font=T.FONT,
                   fill=T.SEL_TX if i == self.menu_open else T.TEXT)

    def _open(self, i, label_x):
        gx, gy = self.window.client_origin()
        host = self.desk.menus
        host.close_all()              # close_all resets menu_open; set it after
        self.menu_open = i
        host.open(self.items[i][1](), gx + label_x,
                  gy + self.y + self.h, bar=self)
        self.invalidate()

    def on_mouse(self, ev):
        if ev.press and ev.btn == 1:
            for i, label, x, w in self._spans():
                if x <= ev.x < x + w:
                    self._open(i, x)
                    return True
        return True

    def hover_switch(self, gev):
        """While a bar menu is open, sliding along the bar switches menus."""
        gx, gy = self.window.client_origin()
        lx, ly = gev.x - gx, gev.y - gy
        if not (self.y <= ly < self.y + self.h):
            return
        for i, label, x, w in self._spans():
            if x <= lx < x + w and i != self.menu_open:
                self._open(i, x)
                return


class Dropdown(Widget):
    focusable = True

    def __init__(self, x, y, w, options, index=0, cb=None):
        super().__init__(x, y, w, 21)
        self.options = list(options)
        self.index = index
        self.cb = cb

    @property
    def value(self):
        return self.options[self.index] if self.options else ""

    def draw(self, d, img):
        x0, y0 = self.x, self.y
        x1, y1 = x0 + self.w - 1, y0 + self.h - 1
        T.sunken(d, x0, y0, x1, y1)
        d.text((x0 + 5, y0 + 4),
               T.ellipsize(T.FONT, self.value, self.w - 28),
               font=T.FONT, fill=T.TEXT)
        bx = x1 - 17
        T.raised(d, bx, y0 + 2, x1 - 2, y1 - 2)
        cx, cy = (bx + x1 - 2) // 2, (y0 + y1) // 2
        d.polygon([(cx - 3, cy - 1), (cx + 4, cy - 1), (cx, cy + 3)],
                  fill=T.TEXT)

    def on_mouse(self, ev):
        if ev.press and ev.btn == 1:
            gx, gy = self.window.client_origin()
            items = []
            for i, o in enumerate(self.options):
                items.append(MenuItem(o, action=lambda i=i: self._pick(i),
                                      checked=(i == self.index)))
            self.desk.menus.open(items, gx + self.x, gy + self.y + self.h,
                                 min_w=self.w)
        return True

    def _pick(self, i):
        self.index = i
        self.invalidate()
        if self.cb:
            self.cb(self.options[i])
