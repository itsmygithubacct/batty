"""kilix desktop — Minesweeper. A custom-drawn minefield over a Window."""
import random
import time

import theme as T
import widgets as W
import wm

CELL = 16
FR = 3                     # frame inset around panel/field (2px bevel + pad)
M = 9                      # outer margin
PANEL_H = 34
GAP = 7

# (rows, cols, mines)
DIFFS = {"Beginner": (9, 9, 10), "Intermediate": (16, 16, 40),
         "Expert": (16, 30, 99)}

# classic number colors, index = adjacent-mine count
NUM = {1: T.SEL_BG, 2: (0, 128, 0), 3: (255, 0, 0), 4: (0, 0, 128),
       5: (128, 0, 0), 6: (0, 128, 128), 7: (0, 0, 0), 8: (128, 128, 128)}

# LED metrics + seven-segment table
DW, DH, DGAP, DPAD = 11, 19, 3, 3
LED_ON, LED_OFF, LED_BG = (255, 0, 0), (64, 0, 0), (0, 0, 0)
SEG = {"0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg",
       "5": "acdfg", "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg",
       "-": "g", " ": ""}


class Mines(wm.Window):
    def __init__(self, desk, arg=None):
        self.rows, self.cols, self.n_mines = DIFFS["Beginner"]
        self.diff = "Beginner"
        self._layout()
        super().__init__(desk, "Minesweeper", self._cw + 2 * T.BORDER,
                         self._ch + 2 * T.BORDER + T.TITLE_H, icon="mines",
                         resizable=False)
        self.menubar = self.add(W.MenuBar(self._cw, [
            ("Game", self._game_menu), ("Help", self._help_menu)]))
        self.face = self.add(_Face(self.face_x, self.face_y, self))
        self.field = self.add(_Field(self.fx0, self.fy0,
                                     self.fw + 2 * FR, self.fh + 2 * FR, self))
        self.start_t = 0.0
        self.elapsed = 0
        self.running = False
        self.new_game()
        desk.tick_hooks.append(self._tick)
        self.on_close = self._teardown

    # ── geometry ────────────────────────────────────────────────────────────
    def _layout(self):
        self.fw, self.fh = self.cols * CELL, self.rows * CELL
        self.px0, self.py0 = M, T.MENU_H + M
        self.px1 = M + FR + self.fw + FR - 1
        self.py1 = self.py0 + FR + PANEL_H + FR - 1
        self.fx0 = M
        self.fy0 = self.py1 + 1 + GAP
        self.fy1 = self.fy0 + FR + self.fh + FR - 1
        self._cw = self.px1 + 1 + M
        self._ch = self.fy1 + 1 + M
        self.face_x = (self.px0 + self.px1) // 2 - 13
        self.face_y = self.py0 + ((self.py1 - self.py0) - 26) // 2
        self.led_y = self.py0 + ((self.py1 - self.py0) - (DH + 2 * DPAD)) // 2
        self.led_l = self.px0 + FR + 2
        self.led_r = self.px1 - FR - 1 - (3 * DW + 2 * DGAP + 2 * DPAD)

    def _resize(self):
        self._layout()
        self.w = self._cw + 2 * T.BORDER
        self.h = self._ch + 2 * T.BORDER + T.TITLE_H
        self.surface = None
        self.menubar.w = self._cw
        self.face.x, self.face.y = self.face_x, self.face_y
        self.field.x, self.field.y = self.fx0, self.fy0
        self.field.w, self.field.h = self.fw + 2 * FR, self.fh + 2 * FR
        self.new_game()
        self.desk.dirty = True

    # ── game state ──────────────────────────────────────────────────────────
    def new_game(self):
        self.mines = set()
        self.counts = [[0] * self.cols for _ in range(self.rows)]
        self.shown = [[False] * self.cols for _ in range(self.rows)]
        self.mark = [[0] * self.cols for _ in range(self.rows)]   # 0/1flag/2?
        self.placed = False
        self.dead = self.won = False
        self.exploded = None
        self.revealed = 0
        self.running = False
        self.elapsed = 0
        if getattr(self, "field", None):
            self.field.reset_press()
        self.invalidate()

    def _compute_counts(self):
        self.counts = [[0] * self.cols for _ in range(self.rows)]
        for (mr, mc) in self.mines:
            for r, c in self._neighbors(mr, mc):
                self.counts[r][c] += 1

    def _place(self, safe):
        cells = [(r, c) for r in range(self.rows) for c in range(self.cols)
                 if (r, c) != safe]
        self.mines = set(random.sample(cells, min(self.n_mines, len(cells))))
        self._compute_counts()
        self.placed = True

    def _neighbors(self, r, c):
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr or dc:
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < self.rows and 0 <= nc < self.cols:
                        yield nr, nc

    def reveal(self, r, c):
        if self.dead or self.won or self.shown[r][c] or self.mark[r][c] == 1:
            return
        if not self.placed:
            self._place((r, c))
        if not self.running:
            self.start_t, self.running = time.time(), True
        if (r, c) in self.mines:
            self.exploded = (r, c)
            self.dead, self.running = True, False
            self.invalidate()
            return
        stack = [(r, c)]
        while stack:
            rr, cc = stack.pop()
            if self.shown[rr][cc] or self.mark[rr][cc] == 1:
                continue
            self.shown[rr][cc] = True
            self.mark[rr][cc] = 0
            self.revealed += 1
            if self.counts[rr][cc] == 0:
                stack.extend(self._neighbors(rr, cc))
        if self.revealed == self.rows * self.cols - len(self.mines):
            self.won, self.running = True, False
            for (mr, mc) in self.mines:
                self.mark[mr][mc] = 1
        self.invalidate()

    def cycle_mark(self, r, c):
        if self.dead or self.won or self.shown[r][c]:
            return
        self.mark[r][c] = (self.mark[r][c] + 1) % 3
        self.invalidate()

    def chord(self, r, c):
        # reveal a number's neighbors when its flag count matches (may detonate)
        if self.dead or self.won or not self.shown[r][c]:
            return
        n = self.counts[r][c]
        if n == 0:
            return
        flags = sum(1 for nr, nc in self._neighbors(r, c)
                    if self.mark[nr][nc] == 1)
        if flags != n:
            return
        for nr, nc in self._neighbors(r, c):
            if not self.shown[nr][nc] and self.mark[nr][nc] != 1:
                self.reveal(nr, nc)

    def flags(self):
        return sum(row.count(1) for row in self.mark)

    def face_key(self):
        if self.dead:
            return "dead"
        if self.won:
            return "cool"
        if getattr(self, "field", None) and (self.field.push is not None
                                             or self.field.chord_cells):
            return "oh"
        return "smile"

    # ── loop / draw ───────────────────────────────────────────────────────────
    def _tick(self, now):
        if not self.running:
            return
        e = min(int(now - self.start_t), 999)
        if e != self.elapsed:
            self.elapsed = e
            self.invalidate()

    def _teardown(self):
        if self._tick in self.desk.tick_hooks:
            self.desk.tick_hooks.remove(self._tick)

    def draw_client(self, d, img):
        T.sunken(d, self.px0, self.py0, self.px1, self.py1, fill=T.FACE)
        _leds(d, self.led_l, self.led_y, self.n_mines - self.flags())
        _leds(d, self.led_r, self.led_y, self.elapsed)

    # ── menus ─────────────────────────────────────────────────────────────────
    def _game_menu(self):
        MI, sep = W.MenuItem, W.sep
        out = [MI("New", action=self.new_game), sep()]
        for name in ("Beginner", "Intermediate", "Expert"):
            out.append(MI(name, checked=self.diff == name,
                          action=lambda n=name: self._set_diff(n)))
        out += [sep(), MI("Exit", action=self.request_close)]
        return out

    def _help_menu(self):
        return [W.MenuItem(
            "About Minesweeper…", icon="mines",
            action=lambda: wm.msgbox(
                self.desk, "About Minesweeper",
                "kilix 95 Minesweeper\n"
                "Left-click clears a square, right-click flags a mine.\n"
                "Middle-click a number to clear around it.\n"
                "Clear every safe square to win.",
                icon="mines"))]

    def _set_diff(self, name):
        self.diff = name
        self.rows, self.cols, self.n_mines = DIFFS[name]
        self._resize()

    def request_close(self):
        self.close()


class _Face(W.Button):
    def __init__(self, x, y, win):
        super().__init__(x, y, 26, 26, cb=win.new_game)
        self.win = win

    def draw(self, d, img):
        x0, y0, x1, y1 = self.x, self.y, self.x + 25, self.y + 25
        (T.pressed if self.down else T.raised)(d, x0, y0, x1, y1)
        o = 1 if self.down else 0
        cx, cy = x0 + 13 + o, y0 + 13 + o
        k = self.win.face_key()
        d.ellipse([cx - 9, cy - 9, cx + 8, cy + 8], fill=(255, 255, 0),
                  outline=T.TEXT)
        if k == "cool":
            d.rectangle([cx - 7, cy - 3, cx - 1, cy], fill=T.TEXT)
            d.rectangle([cx + 1, cy - 3, cx + 7, cy], fill=T.TEXT)
            d.line([(cx - 8, cy - 3), (cx - 7, cy - 3)], fill=T.TEXT)
        elif k == "dead":
            for ex in (cx - 5, cx + 2):
                d.line([(ex, cy - 4), (ex + 3, cy - 1)], fill=T.TEXT)
                d.line([(ex + 3, cy - 4), (ex, cy - 1)], fill=T.TEXT)
        else:
            d.rectangle([cx - 5, cy - 4, cx - 4, cy - 2], fill=T.TEXT)
            d.rectangle([cx + 4, cy - 4, cx + 5, cy - 2], fill=T.TEXT)
        if k == "oh":
            d.ellipse([cx - 2, cy + 2, cx + 2, cy + 6], outline=T.TEXT)
        elif k == "dead":
            d.arc([cx - 5, cy + 4, cx + 5, cy + 12], 200, 340, fill=T.TEXT)
        else:
            d.arc([cx - 5, cy - 2, cx + 5, cy + 6], 20, 160, fill=T.TEXT)


class _Field(W.Widget):
    def __init__(self, x, y, w, h, win):
        super().__init__(x, y, w, h)
        self.win = win
        self.push = None                # cell previewed while the button is held
        self.chord_cells = None         # neighbors previewed while chording
        self.left = self.right = False  # held mouse buttons (for L+R chord)
        self.chording = False

    def reset_press(self):
        self.push = self.chord_cells = None
        self.left = self.right = self.chording = False

    def _cell(self, ev):
        c = (ev.x - self.x - FR) // CELL
        r = (ev.y - self.y - FR) // CELL
        if 0 <= r < self.win.rows and 0 <= c < self.win.cols:
            return r, c
        return None

    def _chord_preview(self, cell):
        w = self.win
        cells = set()
        if cell and w.shown[cell[0]][cell[1]] and w.counts[cell[0]][cell[1]]:
            for nr, nc in w._neighbors(*cell):
                if not w.shown[nr][nc] and w.mark[nr][nc] != 1:
                    cells.add((nr, nc))
        self.chord_cells = cells or None
        self.invalidate()

    def on_mouse(self, ev):
        w = self.win
        cell = self._cell(ev)
        if w.dead or w.won:
            self.reset_press()
            return True
        if ev.press:
            if (ev.btn == 1 and self.left) or (ev.btn == 3 and self.right):
                self.reset_press()                # same button re-pressed: prior release lost
            if ev.btn == 2:                       # middle-button chord
                self.chording = True
                self._chord_preview(cell)
                return True
            if ev.btn == 1:
                self.left = True
            elif ev.btn == 3:
                self.right = True
            if self.left and self.right:          # simultaneous L+R chord
                self.chording = True
                self.push = None
                self._chord_preview(cell)
            elif ev.btn == 1:
                self.push = cell
                self.invalidate()
            elif ev.btn == 3 and cell:
                w.cycle_mark(*cell)
            return True
        if ev.move:
            if self.chording:
                self._chord_preview(cell)
            elif self.push is not None:
                self.push = cell
                self.invalidate()
            return True
        # release (negative-space event)
        was_chording = self.chording
        if ev.btn == 1:
            self.left = False
        elif ev.btn == 3:
            self.right = False
        if was_chording:                          # fire once, on the first release
            self.chording = False
            self.left = self.right = False        # a chord clears both buttons (other release may be lost)
            self.push = self.chord_cells = None
            self.invalidate()
            if cell:
                w.chord(*cell)
        elif ev.btn == 1:
            hit = self.push
            self.push = None
            self.invalidate()
            if hit:
                w.reveal(*hit)
        return True

    def draw(self, d, img):
        w = self.win
        T.sunken(d, self.x, self.y, self.x + self.w - 1, self.y + self.h - 1,
                 fill=T.FACE)
        ox, oy = self.x + FR, self.y + FR
        for r in range(w.rows):
            for c in range(w.cols):
                self._draw_cell(d, ox + c * CELL, oy + r * CELL, r, c)

    def _draw_cell(self, d, x, y, r, c):
        w = self.win
        x1, y1 = x + CELL - 1, y + CELL - 1
        shown = w.shown[r][c]
        mine = (r, c) in w.mines
        if w.dead and mine and w.mark[r][c] != 1:
            shown = True
        if shown:
            if mine:
                if w.exploded == (r, c):
                    d.rectangle([x, y, x1, y1], fill=(255, 0, 0))
                else:
                    d.rectangle([x, y, x1, y1], fill=T.FACE)
                d.line([(x, y), (x1, y)], fill=T.SHADOW)
                d.line([(x, y), (x, y1)], fill=T.SHADOW)
                _mine(d, x, y)
            else:
                d.rectangle([x, y, x1, y1], fill=T.FACE)
                d.line([(x, y), (x1, y)], fill=T.SHADOW)
                d.line([(x, y), (x, y1)], fill=T.SHADOW)
                n = w.counts[r][c]
                if n:
                    s = str(n)
                    tw = T.text_w(T.BOLD, s)
                    d.text((x + (CELL - tw) // 2, y + 1), s, font=T.BOLD,
                           fill=NUM[n])
            return
        pushed = (self.push == (r, c) or
                  (self.chord_cells is not None and (r, c) in self.chord_cells)) \
            and w.mark[r][c] != 1
        if pushed:
            d.rectangle([x, y, x1, y1], fill=T.FACE)
            d.line([(x, y), (x1, y)], fill=T.SHADOW)
            d.line([(x, y), (x, y1)], fill=T.SHADOW)
            return
        T.raised(d, x, y, x1, y1)
        m = w.mark[r][c]
        if m == 1:
            _flag(d, x, y, w.dead and not mine)
        elif m == 2:
            tw = T.text_w(T.BOLD, "?")
            d.text((x + (CELL - tw) // 2, y + 1), "?", font=T.BOLD,
                   fill=T.SEL_BG)


# ── code-drawn art ──────────────────────────────────────────────────────────
def _mine(d, x, y):
    cx, cy = x + 8, y + 8
    d.line([(cx - 4, cy), (cx + 4, cy)], fill=T.TEXT)
    d.line([(cx, cy - 4), (cx, cy + 4)], fill=T.TEXT)
    d.line([(cx - 3, cy - 3), (cx + 3, cy + 3)], fill=T.TEXT)
    d.line([(cx - 3, cy + 3), (cx + 3, cy - 3)], fill=T.TEXT)
    d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=T.TEXT)
    d.point((cx - 1, cy - 1), fill=T.LIGHT)


def _flag(d, x, y, wrong=False):
    px = x + 8
    d.line([(px, y + 3), (px, y + 11)], fill=T.TEXT)
    d.polygon([(px, y + 3), (px, y + 7), (px - 4, y + 5)], fill=(255, 0, 0))
    d.rectangle([x + 4, y + 11, x + 11, y + 12], fill=T.TEXT)
    if wrong:
        d.line([(x + 2, y + 2), (x + 13, y + 13)], fill=(255, 0, 0))
        d.line([(x + 13, y + 2), (x + 2, y + 13)], fill=(255, 0, 0))


def _leds(d, x, y, value):
    if value < 0:
        s = "-" + f"{min(-value, 99):02d}"
    else:
        s = f"{min(value, 999):03d}"
    ww = 3 * DW + 2 * DGAP + 2 * DPAD
    T.sunken(d, x, y, x + ww - 1, y + DH + 2 * DPAD - 1, fill=LED_BG)
    for i, ch in enumerate(s):
        _digit(d, x + DPAD + i * (DW + DGAP), y + DPAD, ch)


def _digit(d, x, y, ch):
    on = SEG.get(ch, "")
    t, mid = 2, DH // 2
    segs = {
        "a": (x + t, y, x + DW - 1 - t, y + t - 1),
        "d": (x + t, y + DH - t, x + DW - 1 - t, y + DH - 1),
        "g": (x + t, y + mid - 1, x + DW - 1 - t, y + mid),
        "f": (x, y + t, x + t - 1, y + mid - 1),
        "b": (x + DW - t, y + t, x + DW - 1, y + mid - 1),
        "e": (x, y + mid, x + t - 1, y + DH - 1 - t),
        "c": (x + DW - t, y + mid, x + DW - 1, y + DH - 1 - t),
    }
    for name, box in segs.items():
        d.rectangle(box, fill=LED_ON if name in on else LED_OFF)
