"""kilix desktop — Character Map. Pick glyphs from a font and copy them.

A sunken grid of glyphs (paged), a magnified preview of the hovered/selected
cell, a "Characters to copy" field the grid feeds, and a Copy button that puts
the field on the desktop clipboard. Arrow keys walk the grid; Enter picks.
"""
import unicodedata

import theme as T
import widgets as W
import wm

COLS, ROWS = 16, 8                  # cells per page
CW, CH = 22, 22                     # cell size
FIRST = 0x20                        # printable ASCII space onward
LAST = 0x2AF                        # end of the last selectable page
BIG = T.FONT.font_variant(size=44)  # magnifier face


class _Grid(W.Widget):
    focusable = True

    def __init__(self, x, y, on_pick):
        super().__init__(x, y, COLS * CW + 4, ROWS * CH + 4)
        self.base = FIRST
        self.sel = 0                # selected cell index
        self.hover = -1             # hovered cell index, or -1
        self.on_pick = on_pick

    def cp(self, i):
        return self.base + i if 0 <= i < COLS * ROWS else -1

    def active_cp(self):
        return self.cp(self.hover if self.hover >= 0 else self.sel)

    def _cell_at(self, px, py):
        cx, cy = px - self.x - 2, py - self.y - 2
        if 0 <= cx < COLS * CW and 0 <= cy < ROWS * CH:
            return (cy // CH) * COLS + cx // CW
        return -1

    def page(self, step):
        base = self.base + step * COLS * ROWS
        if FIRST <= base <= LAST:
            self.base = base
            self.invalidate()

    def draw(self, d, img):
        T.sunken(d, self.x, self.y, self.x + self.w - 1, self.y + self.h - 1)
        ix, iy = self.x + 2, self.y + 2
        for i in range(COLS * ROWS):
            col, row = i % COLS, i // COLS
            x0, y0 = ix + col * CW, iy + row * CH
            if i == self.sel:
                d.rectangle([x0, y0, x0 + CW - 1, y0 + CH - 1], fill=T.SEL_BG)
            elif i == self.hover:
                d.rectangle([x0, y0, x0 + CW - 1, y0 + CH - 1], fill=T.FACE)
            ch = chr(self.base + i)
            tw = T.text_w(T.FONT, ch)
            fill = T.SEL_TX if i == self.sel else T.TEXT
            d.text((x0 + (CW - tw) // 2, y0 + 5), ch, font=T.FONT, fill=fill)
        for c in range(1, COLS):    # grid rules
            d.line([(ix + c * CW, iy), (ix + c * CW, iy + ROWS * CH - 1)],
                   fill=T.LTGRAY)
        for r in range(1, ROWS):
            d.line([(ix, iy + r * CH), (ix + COLS * CW - 1, iy + r * CH)],
                   fill=T.LTGRAY)

    def on_mouse(self, ev):
        if ev.wheel:
            self.page(ev.wheel)
            return True
        i = self._cell_at(ev.x, ev.y)
        if ev.move and ev.btn == 0:
            if i != self.hover:
                self.hover = i
                self.invalidate()
            return True
        if ev.press and i >= 0:
            self.sel = i
            self.on_pick(self.cp(i))
            self.invalidate()
            return True
        return False

    def on_key(self, ev):
        row, col = self.sel // COLS, self.sel % COLS
        if ev.key == "ArrowLeft":
            col -= 1
        elif ev.key == "ArrowRight":
            col += 1
        elif ev.key == "ArrowUp":
            row -= 1
        elif ev.key == "ArrowDown":
            row += 1
        elif ev.key in ("Enter", " "):
            self.on_pick(self.cp(self.sel))
            return True
        else:
            return False
        self.sel = max(0, min(COLS * ROWS - 1, row * COLS + col))
        self.hover = -1
        self.invalidate()
        return True


class CharMap(wm.Window):
    def __init__(self, desk, arg=None):
        super().__init__(desk, "Character Map", 476, 356, icon="charmap",
                         resizable=False)
        cw, ch = self.client_size()
        self.grid = self.add(_Grid(6, 24, self._pick))
        px = self.grid.x + self.grid.w + 10          # preview column
        self.px = px
        self.b_prev = self.add(W.Button(px, 118, 44, 23, "◄",
                                        cb=lambda: self.grid.page(-1)))
        self.b_next = self.add(W.Button(px + 48, 118, 44, 23, "►",
                                        cb=lambda: self.grid.page(+1)))
        self.copy = self.add(W.TextField(126, ch - 56, cw - 216))
        self.b_copy = self.add(W.Button(cw - 84, ch - 58, 78, 24, "Copy",
                                        cb=self._copy, default=True))
        self.set_focus(self.grid)

    def _pick(self, cp):
        if cp < 0:
            return
        self.copy.insert(chr(cp))
        self.copy.invalidate()

    def _copy(self):
        text = self.copy.text
        if text:
            self.desk.set_clipboard(text)

    def draw_client(self, d, img):
        cw, ch = self.client_size()
        d.text((8, 6), "Font: DejaVu Sans (kilix 95)", font=T.FONT,
               fill=T.TEXT)
        # magnified preview of the active glyph
        px = self.px
        T.sunken(d, px, 24, px + 92, 92)
        cp = self.grid.active_cp()
        if cp >= 0:
            ch_ = chr(cp)
            tw = T.text_w(BIG, ch_)
            d.text((px + 2 + (89 - tw) // 2, 34), ch_, font=BIG, fill=T.TEXT)
        d.text((px, 98), self._codepoint(cp), font=T.FONT, fill=T.TEXT)
        d.text((8, ch - 53), "Characters to copy:", font=T.FONT, fill=T.TEXT)
        # status well: codepoint + Unicode name of the active glyph
        T.sunken(d, 2, ch - 20, cw - 3, ch - 3, fill=T.FACE)
        d.text((8, ch - 17), self._status(cp), font=T.FONT, fill=T.TEXT)

    def _codepoint(self, cp):
        return f"U+{cp:04X}" if cp >= 0 else ""

    def _status(self, cp):
        if cp < 0:
            return ""
        name = unicodedata.name(chr(cp), "")
        return f"U+{cp:04X}" + (f"   {name}" if name else "")

    def on_key(self, ev):
        if ev.ctrl and ev.key == "c" and self.focus is not self.copy:
            self._copy()
            return True
        return super().on_key(ev)
