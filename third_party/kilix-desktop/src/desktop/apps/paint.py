"""kilix desktop — Paint. A bitmap editor over a PIL RGB canvas.

Left tool palette (pencil/brush/line/rect/ellipse/fill/eraser/eyedropper),
a bottom 28-swatch color bar with a fore/back indicator, and a sunken,
scrollable canvas. Freehand tools stroke live; shape tools rubber-band a
preview committed on release; the bucket floods with PIL's flood fill.
"""
import os

from PIL import Image, ImageDraw

import filedialog
import theme as T
import widgets as W
import wm

# save format by extension; unknown → PNG
FORMATS = {".png": "PNG", ".bmp": "BMP", ".gif": "GIF",
           ".jpg": "JPEG", ".jpeg": "JPEG"}
OPEN_FILTERS = [("Images", "*.png;*.bmp;*.gif;*.jpg;*.jpeg"),
                ("All Files", "*.*")]
SAVE_FILTERS = [("PNG Image", "*.png")]

TOP = T.MENU_H
M = 3
TCW, TCH, TCOLS = 24, 22, 2
TOOLS = ["pencil", "brush", "line", "rect", "frect", "ellipse", "fill",
         "eraser", "dropper"]
TROWS = (len(TOOLS) + TCOLS - 1) // TCOLS
TOOLS_W = TCOLS * TCW
PAL_H = 44
WIDTHS = {"pencil": 1, "brush": 5, "eraser": 12}
GRAY = (128, 128, 128)                 # the workspace surround around the sheet

# the classic 28-color paint palette, column-major (top row then bottom row)
COLORS = [
    (0, 0, 0), (255, 255, 255), (128, 128, 128), (192, 192, 192),
    (128, 0, 0), (255, 0, 0), (128, 128, 0), (255, 255, 0),
    (0, 128, 0), (0, 255, 0), (0, 128, 128), (0, 255, 255),
    (0, 0, 128), (0, 0, 255), (128, 0, 128), (255, 0, 255),
    (128, 128, 64), (255, 255, 128), (0, 64, 64), (0, 255, 128),
    (0, 64, 128), (128, 128, 255), (64, 0, 255), (255, 0, 128),
    (128, 64, 0), (255, 128, 64), (0, 0, 0), (255, 255, 255),
]


class Paint(wm.Window):
    def __init__(self, desk, arg=None):
        super().__init__(desk, "untitled - Paint", 560, 420, icon="paint")
        self.min_w, self.min_h = 360, 260
        self.path = None
        self.modified = False
        self.tool = "pencil"
        self.fg, self.bg = (0, 0, 0), (255, 255, 255)
        cw, ch = self.client_size()
        self.menubar = self.add(W.MenuBar(cw, [
            ("File", self._file_menu), ("Image", self._image_menu),
            ("Help", self._help_menu)]))
        self.tools = self.add(_Tools(self))
        self.canvas = self.add(_Canvas(self))
        self.palette = self.add(_ColorBar(self))
        self._layout()
        self.canvas.new_image()

    def _layout(self):
        cw, ch = self.client_size()
        self.menubar.w = cw
        self.tools.x, self.tools.y = M, TOP + M
        self.tools.w, self.tools.h = TOOLS_W, TROWS * TCH
        self.palette.x, self.palette.y = M, ch - M - PAL_H
        self.palette.w, self.palette.h = cw - 2 * M, PAL_H
        self.canvas.x, self.canvas.y = 2 * M + TOOLS_W, TOP + M
        self.canvas.w = cw - self.canvas.x - M
        self.canvas.h = ch - 2 * M - PAL_H - TOP

    def on_resize(self):
        self._layout()

    # ── state fanout ────────────────────────────────────────────────────────
    def set_tool(self, tool):
        self.tool = tool
        self.tools.invalidate()

    def set_fg(self, c):
        self.fg = tuple(c[:3])
        self.palette.invalidate()

    def set_bg(self, c):
        self.bg = tuple(c[:3])
        self.palette.invalidate()

    # ── title / dirty ────────────────────────────────────────────────────────
    def _retitle(self):
        name = os.path.basename(self.path) if self.path else "untitled"
        star = "*" if self.modified else ""
        self.title = f"{star}{name} - Paint"
        self.invalidate()

    def mark_dirty(self):
        if not self.modified:
            self.modified = True
            self._retitle()

    def mark_clean(self):
        self.modified = False
        self._retitle()

    # ── file plumbing ────────────────────────────────────────────────────────
    def _load(self, path):
        path = os.path.expanduser(path)
        try:
            img = Image.open(path)
            img.load()
        except (OSError, ValueError, Image.DecompressionBombError) as e:
            wm.msgbox(self.desk, "Paint", str(e), icon="error")
            return
        self.canvas.set_image(img)
        self.path = path
        self.desk.shell.add_recent(path)
        self.mark_clean()

    def _save(self, then=None, path=None):
        target = os.path.expanduser(path) if path else self.path
        if not target:
            return self._save_as(then)
        fmt = FORMATS.get(os.path.splitext(target)[1].lower(), "PNG")
        try:
            self.canvas.img.save(target, fmt)
        except (OSError, KeyError, ValueError) as e:
            wm.msgbox(self.desk, "Paint", str(e), icon="error")
            return
        self.path = target
        self.desk.shell.add_recent(target)
        self.mark_clean()
        if then:
            then()

    def _save_as(self, then=None):
        def do(path):
            if path:
                self._save(then, path=path)
        filedialog.save_file(self.desk, "Save As", do, filters=SAVE_FILTERS,
                             filename=self.path or "untitled.png")

    def _open(self):
        def go():
            filedialog.open_file(
                self.desk, "Open", lambda p: p and self._load(p),
                filters=OPEN_FILTERS,
                start=os.path.dirname(self.path) if self.path else None)
        self._if_saved(go)

    def _new(self):
        def go():
            self.path = None
            self.canvas.new_image()
            self.mark_clean()
        self._if_saved(go)

    def _if_saved(self, then):
        if not self.modified:
            then()
            return

        def do(ans):
            if ans == "Yes":
                self._save(then)
            elif ans == "No":
                then()
        wm.msgbox(self.desk, "Paint",
                  "The image has changed.\nSave the changes?",
                  icon="warn", buttons=("Yes", "No", "Cancel"), cb=do)

    def request_close(self):
        self._if_saved(self.close)

    # ── menus ───────────────────────────────────────────────────────────────
    def _file_menu(self):
        MI, sep = W.MenuItem, W.sep
        return [MI("New", action=self._new),
                MI("Open…", action=self._open),
                MI("Save", action=self._save),
                MI("Save As…", action=self._save_as),
                sep(), MI("Close", action=self.request_close)]

    def _image_menu(self):
        return [W.MenuItem("Clear Image", action=self.canvas.clear)]

    def _help_menu(self):
        return [W.MenuItem(
            "About Paint…", icon="paint",
            action=lambda: wm.msgbox(
                self.desk, "About Paint",
                "kilix 95 Paint\nLeft-click draws with the foreground color,\n"
                "right-click with the background color.",
                icon="paint"))]

    def on_key(self, ev):
        if ev.ctrl and ev.key == "s":
            self._save()
            return True
        if ev.ctrl and ev.key == "o":
            self._open()
            return True
        if ev.ctrl and ev.key == "n":
            self._new()
            return True
        return super().on_key(ev)


# ── tool palette ────────────────────────────────────────────────────────────

class _Tools(W.Widget):
    def __init__(self, paint):
        super().__init__(0, 0, TOOLS_W, TROWS * TCH)
        self.paint = paint

    def _cell(self, i):
        col, row = i % TCOLS, i // TCOLS
        return self.x + col * TCW, self.y + row * TCH

    def draw(self, d, img):
        for i, tool in enumerate(TOOLS):
            cx, cy = self._cell(i)
            sel = self.paint.tool == tool
            box = (cx, cy, cx + TCW - 1, cy + TCH - 1)
            (T.pressed if sel else T.raised)(d, *box)
            off = 1 if sel else 0
            _glyph(d, cx + TCW // 2 + off, cy + TCH // 2 + off, tool)

    def on_mouse(self, ev):
        if ev.press and ev.btn == 1:
            col = (ev.x - self.x) // TCW
            row = (ev.y - self.y) // TCH
            i = row * TCOLS + col
            if 0 <= col < TCOLS and 0 <= i < len(TOOLS):
                self.paint.set_tool(TOOLS[i])
        return True


def _glyph(d, mx, my, tool):
    K = T.TEXT
    if tool == "pencil":
        d.line([(mx - 5, my + 5), (mx + 3, my - 3)], fill=K, width=2)
        d.polygon([(mx + 2, my - 5), (mx + 5, my - 2), (mx + 4, my - 4)],
                  fill=K)
    elif tool == "brush":
        d.line([(mx - 5, my + 5), (mx + 2, my - 2)], fill=K, width=3)
        d.ellipse([mx + 1, my - 5, mx + 5, my - 1], fill=K)
    elif tool == "line":
        d.line([(mx - 5, my + 5), (mx + 5, my - 5)], fill=K, width=1)
    elif tool == "rect":
        d.rectangle([mx - 5, my - 4, mx + 5, my + 4], outline=K)
    elif tool == "frect":
        d.rectangle([mx - 5, my - 4, mx + 5, my + 4], fill=K, outline=K)
    elif tool == "ellipse":
        d.ellipse([mx - 5, my - 4, mx + 5, my + 4], outline=K)
    elif tool == "fill":
        d.polygon([(mx - 4, my), (mx - 1, my - 5), (mx + 4, my),
                   (mx + 1, my + 4), (mx - 4, my - 1)], outline=K)
        d.line([(mx + 4, my), (mx + 5, my + 4)], fill=K)
    elif tool == "eraser":
        d.polygon([(mx - 5, my + 1), (mx, my - 4), (mx + 5, my + 1),
                   (mx, my + 5)], outline=K)
    elif tool == "dropper":
        d.line([(mx - 5, my + 5), (mx + 2, my - 2)], fill=K, width=1)
        d.ellipse([mx + 1, my - 5, mx + 5, my - 1], outline=K)


# ── color bar ─────────────────────────────────────────────────────────────--

class _ColorBar(W.Widget):
    IND_W = 38
    SW = 13

    def __init__(self, paint):
        super().__init__(0, 0, 10, PAL_H)
        self.paint = paint

    def cell_rect(self, i):
        col, row = i // 2, i % 2
        sx = self.x + self.IND_W
        sy = self.y + (self.h - 2 * self.SW) // 2
        return sx + col * self.SW, sy + row * self.SW, self.SW, self.SW

    def draw(self, d, img):
        T.raised_thin(d, self.x, self.y, self.x + self.w - 1,
                      self.y + self.h - 1)
        iy = self.y + (self.h - 24) // 2
        ix = self.x + 5
        d.rectangle([ix + 8, iy + 8, ix + 22, iy + 22], fill=self.paint.bg)
        T.sunken(d, ix + 8, iy + 8, ix + 22, iy + 22, fill=None)
        d.rectangle([ix, iy, ix + 14, iy + 14], fill=self.paint.fg)
        T.sunken(d, ix, iy, ix + 14, iy + 14, fill=None)
        for i, c in enumerate(COLORS):
            x, y, w, h = self.cell_rect(i)
            T.sunken(d, x, y, x + w - 1, y + h - 1, fill=None)
            d.rectangle([x + 1, y + 1, x + w - 2, y + h - 2], fill=c)

    def on_mouse(self, ev):
        if ev.press and ev.btn in (1, 3):
            for i, c in enumerate(COLORS):
                x, y, w, h = self.cell_rect(i)
                if x <= ev.x < x + w and y <= ev.y < y + h:
                    (self.paint.set_fg if ev.btn == 1
                     else self.paint.set_bg)(c)
                    break
        return True


# ── canvas ──────────────────────────────────────────────────────────────────

class _Canvas(W.Widget):
    def __init__(self, paint):
        super().__init__(0, 0, 10, 10)
        self.paint = paint
        self.img = Image.new("RGB", (1, 1), (255, 255, 255))
        self.ox = self.oy = 0
        self.vsb, self.hsb = W.VScroll(), _HScroll()
        self.active = None
        self.start = self.last = (0, 0)
        self.col = (0, 0, 0)
        self.preview = None            # (tool, (x0,y0), (x1,y1))

    # ── image lifecycle ─────────────────────────────────────────────────────
    def new_image(self):
        iw, ih = max(1, self.w - 4), max(1, self.h - 4)
        self.img = Image.new("RGB", (iw, ih), (255, 255, 255))
        self.ox = self.oy = 0
        self.preview = None
        self.invalidate()

    def set_image(self, img):
        self.img = img.convert("RGB")
        self.ox = self.oy = 0
        self.preview = None
        self.invalidate()

    def clear(self):
        ImageDraw.Draw(self.img).rectangle(
            [0, 0, self.img.width, self.img.height], fill=self.paint.bg)
        self.paint.mark_dirty()
        self.invalidate()

    # ── geometry ────────────────────────────────────────────────────────────
    def _metrics(self):
        iw, ih = self.w - 4, self.h - 4
        vneed = self.img.height > ih
        hneed = self.img.width > iw
        vneed = self.img.height > ih - (T.SCROLL_W if hneed else 0)
        hneed = self.img.width > iw - (T.SCROLL_W if vneed else 0)
        view_w = iw - (T.SCROLL_W if vneed else 0)
        view_h = ih - (T.SCROLL_W if hneed else 0)
        return max(1, view_w), max(1, view_h), vneed, hneed

    def _sync(self, vw, vh):
        vx0, vy0 = self.x + 2, self.y + 2
        self.ox = max(0, min(self.ox, self.img.width - vw))
        self.oy = max(0, min(self.oy, self.img.height - vh))
        self.vsb.total, self.vsb.page, self.vsb.pos = (
            self.img.height, vh, self.oy)
        self.vsb.place(vx0 + vw, vy0, vh)
        self.hsb.total, self.hsb.page, self.hsb.pos = (
            self.img.width, vw, self.ox)
        self.hsb.place(vx0, vy0 + vh, vw)

    def draw(self, d, img):
        vw, vh, vneed, hneed = self._metrics()
        self._sync(vw, vh)
        T.sunken(d, self.x, self.y, self.x + self.w - 1, self.y + self.h - 1,
                 fill=T.FACE)
        base = self.img
        if self.preview:
            base = self.img.copy()
            _shape(ImageDraw.Draw(base), *self.preview, self.col)
        view = Image.new("RGB", (vw, vh), GRAY)
        view.paste(base, (-self.ox, -self.oy))
        img.paste(view, (self.x + 2, self.y + 2))
        if vneed:
            self.vsb.draw(d)
        if hneed:
            self.hsb.draw(d)

    # ── input ───────────────────────────────────────────────────────────────
    def _bmp(self, ev):
        bx = ev.x - (self.x + 2) + self.ox
        by = ev.y - (self.y + 2) + self.oy
        return (max(0, min(self.img.width - 1, bx)),
                max(0, min(self.img.height - 1, by)))

    def on_mouse(self, ev):
        vw, vh, vneed, hneed = self._metrics()
        self._sync(vw, vh)
        if ev.wheel:
            self.oy += ev.wheel * 32
            self.invalidate()
            return True
        if ev.press and ev.btn in (1, 3):
            if vneed and self.vsb.hit(ev.x, ev.y):
                self.active = "v"
                self._scroll(self.vsb, ev)
                return True
            if hneed and self.hsb.hit(ev.x, ev.y):
                self.active = "h"
                self._scroll(self.hsb, ev)
                return True
            self.active = "draw"
            self._begin(ev)
            return True
        if ev.move:
            if self.active == "v":
                self._scroll(self.vsb, ev)
            elif self.active == "h":
                self._scroll(self.hsb, ev)
            elif self.active == "draw":
                self._drag(ev)
            return True
        if not ev.press and not ev.move and not ev.wheel:
            if self.active == "v":
                self._scroll(self.vsb, ev)
            elif self.active == "h":
                self._scroll(self.hsb, ev)
            elif self.active == "draw":
                self._commit(ev)
            self.active = None
        return True

    def _scroll(self, sb, ev):
        if sb.on_mouse(ev, line=32):
            self.oy, self.ox = self.vsb.pos, self.hsb.pos
            self.invalidate()

    # ── drawing ──────────────────────────────────────────────────────────────
    def _begin(self, ev):
        p = self.paint
        b = self._bmp(ev)
        self.start = self.last = b
        self.col = p.bg if (ev.btn == 3 or p.tool == "eraser") else p.fg
        if p.tool == "fill":
            ImageDraw.floodfill(self.img, b, self.col)
            p.mark_dirty()
        elif p.tool == "dropper":
            c = self.img.getpixel(b)
            (p.set_bg if ev.btn == 3 else p.set_fg)(c)
        elif p.tool in WIDTHS:
            _stroke(ImageDraw.Draw(self.img), b, b, self.col, WIDTHS[p.tool])
            p.mark_dirty()
        else:
            self.preview = (p.tool, self.start, b)
        self.invalidate()

    def _drag(self, ev):
        p, b = self.paint, self._bmp(ev)
        if p.tool in WIDTHS:
            _stroke(ImageDraw.Draw(self.img), self.last, b, self.col,
                    WIDTHS[p.tool])
            self.last = b
            p.mark_dirty()
        elif self.preview:
            self.preview = (p.tool, self.start, b)
        self.invalidate()

    def _commit(self, ev):
        if self.preview:
            _shape(ImageDraw.Draw(self.img), *self.preview, self.col)
            self.preview = None
            self.paint.mark_dirty()
        self.invalidate()


def _stroke(draw, a, b, col, wd):
    draw.line([a, b], fill=col, width=wd)
    if wd > 2:
        r = wd // 2
        for x, y in (a, b):
            draw.ellipse([x - r, y - r, x + r, y + r], fill=col)


def _shape(draw, tool, a, b, col):
    box = [min(a[0], b[0]), min(a[1], b[1]),
           max(a[0], b[0]), max(a[1], b[1])]
    if tool == "line":
        draw.line([a, b], fill=col, width=1)
    elif tool == "rect":
        draw.rectangle(box, outline=col)
    elif tool == "frect":
        draw.rectangle(box, fill=col, outline=col)
    elif tool == "ellipse":
        draw.ellipse(box, outline=col)


class _HScroll:
    """A horizontal VScroll: same pixel model, x/y transposed."""

    def __init__(self):
        self.x = self.y = self.w = 0
        self.total = self.page = self.pos = 0
        self.drag = None

    def place(self, x, y, w):
        self.x, self.y, self.w = x, y, w

    def clamp(self):
        self.pos = max(0, min(self.pos, max(0, self.total - self.page)))

    def _thumb(self):
        span = self.w - 2 * T.SCROLL_W
        if self.total <= self.page or span <= 8:
            return None
        tw = max(8, span * self.page // self.total)
        tx = self.x + T.SCROLL_W + (span - tw) * self.pos // max(
            1, self.total - self.page)
        return tx, tw

    def hit(self, px, py):
        return (self.y <= py < self.y + T.SCROLL_W
                and self.x <= px < self.x + self.w)

    def draw(self, d):
        y0, y1 = self.y, self.y + T.SCROLL_W - 1
        d.rectangle([self.x, y0, self.x + self.w - 1, y1], fill=T.LTGRAY)
        for dx, left in ((0, True), (self.w - T.SCROLL_W, False)):
            bx = self.x + dx
            T.raised(d, bx, y0, bx + T.SCROLL_W - 1, y1)
            cx, cy = bx + T.SCROLL_W // 2, y0 + T.SCROLL_W // 2
            pts = ([(cx + 1, cy - 3), (cx + 1, cy + 3), (cx - 2, cy)] if left
                   else [(cx - 1, cy - 3), (cx - 1, cy + 3), (cx + 2, cy)])
            d.polygon(pts, fill=T.TEXT if self.total > self.page else T.SHADOW)
        t = self._thumb()
        if t:
            tx, tw = t
            T.raised(d, tx, y0, tx + tw - 1, y1)

    def on_mouse(self, ev, line=1):
        old = self.pos
        if ev.press and ev.btn == 1:
            if ev.x < self.x + T.SCROLL_W:
                self.pos -= line
            elif ev.x >= self.x + self.w - T.SCROLL_W:
                self.pos += line
            else:
                t = self._thumb()
                if t and t[0] <= ev.x < t[0] + t[1]:
                    self.drag = ev.x - t[0]
                elif t:
                    self.pos += -self.page if ev.x < t[0] else self.page
        elif ev.move and self.drag is not None:
            span = self.w - 2 * T.SCROLL_W
            t = self._thumb()
            if t:
                frac = (ev.x - self.drag - self.x - T.SCROLL_W) / max(
                    1, span - t[1])
                self.pos = int(frac * (self.total - self.page) + 0.5)
        elif not ev.press and not ev.move:
            self.drag = None
        self.clamp()
        return self.pos != old
