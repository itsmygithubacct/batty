"""kilix desktop — Solitaire (Klondike). Deal-1 stock, drag runs, foundations.

The whole board is custom-drawn in draw_client; input is handled in on_mouse
with the desk's press-capture (mouse_owner) carrying every move/release of a
card drag back here. Cards, suits and backs are original pixel art.
"""
import random

from PIL import ImageFont

import theme as T
import widgets as W
import wm

SUITS = ("spade", "heart", "diamond", "club")   # 0 3 black · 1 2 red
RANKS = ("", "A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
RED = (170, 0, 0)
BLACK = (0, 0, 0)
FELT = (0, 120, 64)
FELT_D = (0, 80, 44)

CW, CH = 48, 66
GAPX = 10
MARGIN = 14
FAN_DOWN = 5
FAN_UP = 16
FAN_X = 14                 # draw-three waste horizontal offset
STEP = CW + GAPX
BASE_CLIENT_W = 2 * MARGIN + 7 * CW + 6 * GAPX
BASE_CLIENT_H = 456 - 2 * T.BORDER - T.TITLE_H


def _red(c):
    return c.suit in (1, 2)


def _overlap(a, b):
    """Intersection area of two (x0,y0,x1,y1) rects (0 if disjoint)."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0


class Card:
    __slots__ = ("rank", "suit", "up")

    def __init__(self, rank, suit, up=False):
        self.rank, self.suit, self.up = rank, suit, up


# ── suit pips (drawn in code) ───────────────────────────────────────────────
def _circ(d, cx, cy, r, col):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)


def _heart(d, cx, cy, r, col):
    d.ellipse([cx - r, cy - r, cx, cy], fill=col)
    d.ellipse([cx, cy - r, cx + r, cy], fill=col)
    d.polygon([(cx - r, cy - r * 0.35), (cx + r, cy - r * 0.35),
               (cx, cy + r)], fill=col)


def _diamond(d, cx, cy, r, col):
    d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)],
              fill=col)


def _spade(d, cx, cy, r, col):
    d.polygon([(cx, cy - r), (cx - r, cy + r * 0.4), (cx + r, cy + r * 0.4)],
              fill=col)
    d.ellipse([cx - r, cy - r * 0.1, cx, cy + r * 0.9], fill=col)
    d.ellipse([cx, cy - r * 0.1, cx + r, cy + r * 0.9], fill=col)
    d.polygon([(cx - r * 0.5, cy + r), (cx + r * 0.5, cy + r),
               (cx, cy + r * 0.15)], fill=col)


def _club(d, cx, cy, r, col):
    rr = r * 0.6
    _circ(d, cx, cy - r * 0.4, rr, col)
    _circ(d, cx - r * 0.55, cy + r * 0.3, rr, col)
    _circ(d, cx + r * 0.55, cy + r * 0.3, rr, col)
    d.polygon([(cx - r * 0.35, cy + r), (cx + r * 0.35, cy + r),
               (cx, cy + r * 0.1)], fill=col)


_PIP = (_spade, _heart, _diamond, _club)


class Solitaire(wm.Window):
    def __init__(self, desk, arg=None):
        super().__init__(desk, "Solitaire", 432, 456, icon="cards")
        self.min_w = self.min_h = 432
        cw, _ = self.client_size()
        self.menubar = self.add(W.MenuBar(cw, [("Game", self._game_menu),
                                               ("Help", self._help_menu)]))
        self.stock = []           # face-down deck
        self.waste = []           # dealt cards (face up)
        self.found = [[], [], [], []]
        self.tab = [[], [], [], [], [], [], []]
        self.drag = None          # active pick-up
        self.won = False
        self._layout()
        st = getattr(desk.shell, "state", None) or {}
        self.draw3 = bool(st.get("sol_draw3", False))
        self.fan = 0              # top waste cards shown fanned
        self.new_game()

    def on_resize(self):
        self.menubar.w = self.client_size()[0]
        self._layout()
        self.invalidate()

    # ── geometry ────────────────────────────────────────────────────────────
    @staticmethod
    def _font_at(base, px):
        path = getattr(base, "path", None)
        if not path:
            return base
        return ImageFont.truetype(path, max(1, int(round(px))))

    def _layout(self):
        cw, ch = self.client_size()
        scale = max(1.0, min(cw / BASE_CLIENT_W, ch / BASE_CLIENT_H))
        self.scale = scale
        self.card_w = max(CW, int(round(CW * scale)))
        self.card_h = max(CH, int(round(CH * scale)))
        self.gap_x = max(GAPX, int(round(GAPX * scale)))
        self.fan_down = max(FAN_DOWN, int(round(FAN_DOWN * scale)))
        self.fan_up = max(FAN_UP, int(round(FAN_UP * scale)))
        self.fan_x = max(FAN_X, int(round(FAN_X * scale)))
        self.step = self.card_w + self.gap_x
        board_w = 7 * self.card_w + 6 * self.gap_x
        natural_margin = max(MARGIN, int(round(MARGIN * scale)))
        self.margin = max(natural_margin, (cw - board_w) // 2)
        self.top_y = T.MENU_H + max(14, int(round(14 * scale)))
        self.tab_y = self.top_y + self.card_h + max(18, int(round(18 * scale)))
        self.rank_font = T.FONT if scale == 1 else self._font_at(T.FONT, 11 * scale)

    def _col_x(self, i):
        return self.margin + i * self.step

    def _card_y(self, pile, j):
        y = self.tab_y
        for k in range(j):
            y += self.fan_up if pile[k].up else self.fan_down
        return y

    def _tab_hit(self, i, py):
        pile = self.tab[i]
        if not pile:
            return None
        hit = None
        for j in range(len(pile)):
            top = self._card_y(pile, j)
            bot = (self._card_y(pile, j + 1) if j + 1 < len(pile)
                   else top + self.card_h)
            if top <= py < bot:
                hit = j
        return hit

    # ── deck ────────────────────────────────────────────────────────────────
    def new_game(self, seed=None):
        deck = [Card(r, s) for s in range(4) for r in range(1, 14)]
        random.Random(seed).shuffle(deck)
        self.found = [[], [], [], []]
        self.tab = [[] for _ in range(7)]
        for i in range(7):
            for j in range(i + 1):
                c = deck.pop()
                c.up = (j == i)
                self.tab[i].append(c)
        self.stock = deck
        self.waste = []
        self.fan = 0
        self.drag = None
        self.won = False
        self.invalidate()

    # ── rules ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _can_found(card, f):
        if not f:
            return card.rank == 1
        return f[-1].suit == card.suit and card.rank == f[-1].rank + 1

    @staticmethod
    def _can_stack(card, onto):
        if onto is None:
            return card.rank == 13
        return (onto.up and _red(onto) != _red(card)
                and onto.rank == card.rank + 1)

    @staticmethod
    def _valid_run(run):
        for a, b in zip(run, run[1:]):
            if not (a.up and b.up and _red(a) != _red(b)
                    and a.rank == b.rank + 1):
                return False
        return bool(run) and run[0].up

    def _after_take(self, src):
        if src is self.waste and self.fan > 1:
            self.fan -= 1                    # one fewer fanned card exposed
        if src in self.tab and src and not src[-1].up:
            src[-1].up = True

    def _waste_fan(self):
        """How many top waste cards are shown fanned (1 unless draw-three)."""
        return max(1, min(self.fan, len(self.waste))) if self.waste else 0

    def _waste_x(self):
        """Left x of the top (playable) waste card."""
        return self._col_x(1) + (self._waste_fan() - 1) * self.fan_x

    def _check_win(self):
        if all(len(f) == 13 for f in self.found) and not self.won:
            self.won = True
            wm.msgbox(self.desk, "Solitaire", "You won!\nDeal a new game?",
                      icon="cards", buttons=("New Game", "Close"),
                      cb=lambda a: self.new_game() if a == "New Game"
                      else None)

    def send_to_foundation(self, src):
        """Top card of src → a matching foundation. Returns True if moved."""
        if not src or not src[-1].up:
            return False
        card = src[-1]
        for f in self.found:
            if self._can_found(card, f):
                f.append(src.pop())
                self._after_take(src)
                self.invalidate()
                self._check_win()
                return True
        return False

    def move_run(self, src, k, dst):
        """Move src[k:] onto tableau list dst if legal. Returns True if moved."""
        run = src[k:]
        if not self._valid_run(run):
            return False
        if not self._can_stack(run[0], dst[-1] if dst else None):
            return False
        dst.extend(run)
        del src[k:]
        self._after_take(src)
        self.invalidate()
        return True

    def deal_stock(self):
        if self.stock:
            n = min(3 if self.draw3 else 1, len(self.stock))
            for _ in range(n):
                c = self.stock.pop()
                c.up = True
                self.waste.append(c)
            self.fan = n
        elif self.waste:
            self.stock = [Card(c.rank, c.suit) for c in reversed(self.waste)]
            self.waste = []
            self.fan = 0
        self.invalidate()

    # ── input ─────────────────────────────────────────────────────────────────
    def on_mouse(self, gev):
        cox, coy = self.client_origin()
        ev = gev.at(cox, coy)
        cw, ch = self.client_size()
        if self.drag is not None:
            if ev.move:
                self.drag["x"] = ev.x - self.drag["gx"]
                self.drag["y"] = ev.y - self.drag["gy"]
                self.invalidate()
            elif not ev.press and not ev.wheel:
                self._drop(ev.x, ev.y)
            return
        inside = 0 <= ev.x < cw and 0 <= ev.y < ch
        if gev.press and inside and ev.y >= T.MENU_H:
            self._board_press(ev)
            return
        super().on_mouse(gev)

    def _board_press(self, ev):
        x, y = ev.x, ev.y
        # stock
        if (self._col_x(0) <= x < self._col_x(0) + self.card_w
                and self.top_y <= y < self.top_y + self.card_h):
            self.deal_stock()
            return
        # waste (only the top card is playable; fanned in draw-three)
        wx = self._waste_x()
        if (self.waste and wx <= x < wx + self.card_w
                and self.top_y <= y < self.top_y + self.card_h):
            if ev.clicks >= 2 and self.send_to_foundation(self.waste):
                return
            self._pick(self.waste, len(self.waste) - 1, wx, self.top_y, x, y)
            return
        # foundations (drag a card back off)
        for fi in range(4):
            fx = self._col_x(3 + fi)
            if (fx <= x < fx + self.card_w
                    and self.top_y <= y < self.top_y + self.card_h
                    and self.found[fi]):
                self._pick(self.found[fi], len(self.found[fi]) - 1,
                           fx, self.top_y, x, y)
                return
        # tableau
        for i in range(7):
            tx = self._col_x(i)
            if not (tx <= x < tx + self.card_w):
                continue
            j = self._tab_hit(i, y)
            if j is None or not self.tab[i][j].up:
                return
            if (ev.clicks >= 2 and j == len(self.tab[i]) - 1
                    and self.send_to_foundation(self.tab[i])):
                return
            if self._valid_run(self.tab[i][j:]):
                self._pick(self.tab[i], j, tx, self._card_y(self.tab[i], j),
                           x, y)
            return

    def _pick(self, src, k, ox, oy, x, y):
        self.drag = {"src": src, "k": k, "gx": x - ox, "gy": y - oy,
                     "x": ox, "y": oy}
        self.invalidate()

    def _drop(self, x, y):
        d = self.drag
        self.drag = None
        src, k = d["src"], d["k"]
        run = src[k:]
        rr = (d["x"], d["y"], d["x"] + self.card_w,
              d["y"] + self.card_h)                       # dragged run's top
        best = None                                       # (area, kind, idx)
        if len(run) == 1:                                 # single card → home
            for fi in range(4):
                fx = self._col_x(3 + fi)
                if not self._can_found(run[0], self.found[fi]):
                    continue
                a = _overlap(rr, (fx, self.top_y,
                                  fx + self.card_w,
                                  self.top_y + self.card_h))
                if a and (best is None or a > best[0]):
                    best = (a, "found", fi)
        for i in range(7):                                # any valid run → tab
            pile = self.tab[i]
            if not (self._valid_run(run)
                    and self._can_stack(run[0], pile[-1] if pile else None)):
                continue
            tx = self._col_x(i)
            bot = self._card_y(pile, len(pile)) + self.card_h
            a = _overlap(rr, (tx, self.tab_y, tx + self.card_w, bot))
            if a and (best is None or a > best[0]):
                best = (a, "tab", i)
        if best and best[1] == "found":
            self.send_to_foundation_at(src, best[2])
        elif best:
            self.move_run(src, k, self.tab[best[2]])
        self.invalidate()

    def send_to_foundation_at(self, src, fi):
        card = src[-1]
        if self._can_found(card, self.found[fi]):
            self.found[fi].append(src.pop())
            self._after_take(src)
            self._check_win()
            return True
        return False

    # ── drawing ─────────────────────────────────────────────────────────────
    def draw_client(self, d, img):
        cw, ch = self.client_size()
        d.rectangle([0, T.MENU_H, cw - 1, ch - 1], fill=FELT)
        self._slot(d, self._col_x(0), self.top_y)      # stock frame
        if self.stock:
            self._back(d, self._col_x(0), self.top_y)
        else:
            s = self.scale
            d.ellipse([self._col_x(0) + int(round(16 * s)),
                       self.top_y + int(round(24 * s)),
                       self._col_x(0) + int(round(32 * s)),
                       self.top_y + int(round(40 * s))], outline=FELT_D)
        self._slot(d, self._col_x(1), self.top_y)      # waste
        w, fan = self.waste, self.fan
        if self.drag and self.drag["src"] is self.waste:
            w = w[:-1]                                 # dragged card revealed
            if fan > 1:
                fan -= 1                               # mirror _after_take
        if w:
            n = max(1, min(fan, len(w)))
            base = len(w) - n
            for k in range(n):
                self._face(d, self._col_x(1) + k * self.fan_x, self.top_y,
                           w[base + k])
        for fi in range(4):                            # foundations
            fx = self._col_x(3 + fi)
            self._slot(d, fx, self.top_y)
            f = self.found[fi]
            if self.drag and self.drag["src"] is f:
                f = f[:-1]                             # dragged card revealed
            if f:
                self._face(d, fx, self.top_y, f[-1])
            else:
                _PIP[fi](d, fx + self.card_w // 2,
                         self.top_y + self.card_h // 2,
                         max(11, int(round(11 * self.scale))), FELT_D)
        for i in range(7):                             # tableau
            tx = self._col_x(i)
            if not self.tab[i]:
                self._slot(d, tx, self.tab_y)
            skip = (self.drag["src"], self.drag["k"]) if self.drag else None
            for j, c in enumerate(self.tab[i]):
                if skip and skip[0] is self.tab[i] and j >= skip[1]:
                    break
                self._card(d, tx, self._card_y(self.tab[i], j), c)
        if self.drag:                                  # floating pick-up
            dx, dy = self.drag["x"], self.drag["y"]
            for n, c in enumerate(self.drag["src"][self.drag["k"]:]):
                self._face(d, dx, dy + n * self.fan_up, c)

    def _slot(self, d, x, y):
        d.rectangle([x, y, x + self.card_w - 1, y + self.card_h - 1],
                    outline=FELT_D)
        self._round(d, x, y)

    def _round(self, d, x, y):
        for cx, cy in ((x, y), (x + self.card_w - 1, y),
                       (x, y + self.card_h - 1),
                       (x + self.card_w - 1, y + self.card_h - 1)):
            d.point((cx, cy), fill=FELT)

    def _card(self, d, x, y, c):
        if c.up:
            self._face(d, x, y, c)
        else:
            self._back(d, x, y)

    def _back(self, d, x, y):
        pad = max(3, int(round(3 * self.scale)))
        dot_step = max(6, int(round(6 * self.scale)))
        dot_pad = max(6, int(round(6 * self.scale)))
        d.rectangle([x, y, x + self.card_w - 1, y + self.card_h - 1],
                    fill=(0, 0, 150),
                    outline=BLACK)
        d.rectangle([x + pad, y + pad,
                     x + self.card_w - pad - 1,
                     y + self.card_h - pad - 1], outline=(120, 160, 255))
        for gy in range(y + dot_pad, y + self.card_h - dot_pad + 1, dot_step):
            for gx in range(x + dot_pad, x + self.card_w - dot_pad + 1,
                            dot_step):
                d.point((gx, gy), fill=(120, 160, 255))
        self._round(d, x, y)

    def _face(self, d, x, y, c):
        d.rectangle([x, y, x + self.card_w - 1, y + self.card_h - 1],
                    fill=T.WINDOW_BG,
                    outline=BLACK)
        self._round(d, x, y)
        col = RED if _red(c) else BLACK
        r = RANKS[c.rank]
        s = self.scale
        rank_font = self.rank_font
        d.text((x + max(3, int(round(3 * s))),
                y + max(2, int(round(2 * s)))), r, font=rank_font, fill=col)
        _PIP[c.suit](d, x + max(6, int(round(6 * s))),
                     y + max(20, int(round(20 * s))),
                     max(3, int(round(3 * s))), col)
        _PIP[c.suit](d, x + self.card_w // 2,
                     y + self.card_h // 2 + max(3, int(round(3 * s))),
                     max(11, int(round(11 * s))), col)
        rw = T.text_w(rank_font, r)
        d.text((x + self.card_w - max(4, int(round(4 * s))) - rw,
                y + self.card_h - max(15, int(round(15 * s)))),
               r, font=rank_font, fill=col)

    # ── menus ─────────────────────────────────────────────────────────────────
    def _game_menu(self):
        MI, sep = W.MenuItem, W.sep
        deal_label = "Draw one" if self.draw3 else "Draw three"
        return [MI("New Game", action=lambda: self.new_game()),
                sep(),
                MI(deal_label, action=self._toggle_draw3),
                sep(),
                MI("Close", action=self.request_close)]

    def _toggle_draw3(self):
        self.draw3 = not self.draw3
        st = getattr(self.desk.shell, "state", None)
        if st is not None:
            st["sol_draw3"] = self.draw3
            self.desk.shell._save_state()
        self.new_game()

    def _help_menu(self):
        return [W.MenuItem(
            "About Solitaire…", icon="cards",
            action=lambda: wm.msgbox(
                self.desk, "About Solitaire",
                "kilix 95 Solitaire\nKlondike, deal one.\n"
                "Click the stock to deal; drag runs between piles;\n"
                "double-click to send a card home.", icon="cards"))]
