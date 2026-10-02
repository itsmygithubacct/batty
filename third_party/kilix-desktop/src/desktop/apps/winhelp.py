"""kilix desktop — Help. A two-pane topic viewer over a book of articles.

Left: a ListBox of topics. Right: a scrollable rich-text pane (wrapped
paragraphs, bold headings, bullet lists). Back/Forward walk visited topics.
"""
import theme as T
import widgets as W
import wm

TB_Y = 2                             # toolbar row
TB_H = 26
LIST_W = 158

# ── the book: key -> (title, [blocks]); block = (kind, text) ─────────────────
# kind: "h" heading (bold), "p" paragraph (wrapped), "b" bullet item.
BOOK = [
    ("welcome", "Welcome to kilix 95", [
        ("h", "Welcome to kilix 95"),
        ("p", "kilix 95 is a pixel desktop in the classic style: a teal "
              "wallpaper, icons you double-click, draggable windows with "
              "raised bevels, and a Start button in the corner."),
        ("p", "Pick a topic on the left to learn your way around. Use Back "
              "and Forward to retrace the topics you have read."),
        ("b", "Desktop basics — icons, selection, the taskbar."),
        ("b", "Using the Start menu — launch programs and games."),
        ("b", "Managing windows — move, size, minimize, close."),
        ("b", "Keyboard shortcuts — do it all from the keys."),
        ("b", "Copy and paste — the mouse, and full-screen programs."),
    ]),
    ("desktop", "Desktop basics", [
        ("h", "Desktop basics"),
        ("p", "The desktop is the teal surface behind every window. It holds "
              "shortcut icons; double-click one to open it."),
        ("b", "Single-click selects an icon; double-click opens it."),
        ("b", "Right-click the desktop for New, Refresh and other commands."),
        ("b", "Drag a rubber-band box to select several icons at once."),
        ("p", "The taskbar along the bottom shows a button for every open "
              "window. Click a button to raise that window or to restore it "
              "if it has been minimized."),
    ]),
    ("startmenu", "Using the Start menu", [
        ("h", "Using the Start menu"),
        ("p", "Click Start, or press Ctrl+Esc, to open the Start menu — the "
              "one place that reaches everything on the system."),
        ("b", "Programs holds the accessories and a Games submenu."),
        ("b", "Documents lists files you opened recently."),
        ("b", "Settings adjusts the desktop's look and behaviour."),
        ("b", "Run launches a command; Shut Down ends the session."),
    ]),
    ("windows", "Managing windows", [
        ("h", "Managing windows"),
        ("p", "Every program runs in a window framed by a title bar and a "
              "raised border."),
        ("b", "Drag the title bar to move a window."),
        ("b", "Drag an edge or corner to resize it."),
        ("b", "The title-bar buttons minimize, maximize and close."),
        ("b", "Double-click the title bar to maximize or restore."),
        ("p", "The active window has a navy title bar; click any window to "
              "bring it to the front and make it active."),
    ]),
    ("keys", "Keyboard shortcuts", [
        ("h", "Keyboard shortcuts"),
        ("p", "These work anywhere on the desktop:"),
        ("b", "Ctrl+Esc — open the Start menu."),
        ("b", "Alt+Tab — switch between open windows."),
        ("b", "Alt+F4 — close the active window."),
        ("b", "Ctrl+Alt+Q — quit the kilix 95 desktop."),
        ("p", "Inside a window:"),
        ("b", "Tab and Shift+Tab — move between controls."),
        ("b", "Enter — activate the default button or selected item."),
        ("b", "Escape — cancel a dialog or close a menu."),
    ]),
    ("copypaste", "Copy and paste", [
        ("h", "Copy and paste"),
        ("p", "In a terminal window, drag with the mouse to select text. The "
              "selection is copied the moment you let go — there is no "
              "separate Copy step. Right-click for a menu with Copy, Paste "
              "and Select all; middle-click to paste."),
        ("p", "A full-screen program (htop, a pager) takes the "
              "mouse for itself, so plain gestures reach the program instead. "
              "Hold Shift to talk to the terminal:"),
        ("b", "Shift+drag — select text (double or triple click with Shift "
              "selects a word or a line)."),
        ("b", "Ctrl+Shift+V, or Shift+middle-click — paste into the program."),
        ("b", "Ctrl+Shift+Right-click — open the copy/paste menu."),
        ("p", "If the highlight vanishes right after you select, the text was "
              "still copied — busy programs repaint the screen, which clears "
              "the highlight but not the clipboard. Paste it and see. And "
              "Ctrl+Shift+H opens the scrollback pager, where the mouse is "
              "never taken and ordinary drag-select always works."),
    ]),
    ("accessories", "The accessories", [
        ("h", "The accessories"),
        ("p", "kilix 95 ships a set of small programs, found under Start, "
              "then Programs, then Accessories."),
        ("b", "Calculator — a standard arithmetic calculator with memory."),
        ("b", "Paint — a bitmap editor for original pixel art."),
        ("b", "Minesweeper — clear the field without hitting a mine."),
        ("b", "Solitaire — the classic patience card game."),
        ("b", "Character Map — browse and copy special characters."),
        ("p", "Open any of them and press F1, or read this Help book, to "
              "learn more."),
    ]),
    ("tips", "Tips", [
        ("h", "Tips of the day"),
        ("b", "Right-click almost anything for a menu of what you can do."),
        ("b", "You can drag files onto the desktop to make shortcuts."),
        ("b", "Minimized windows are not closed — their taskbar button "
              "brings them right back."),
        ("b", "Hold Ctrl while clicking icons to select several at once."),
        ("b", "The address bar in the File Manager accepts a typed path."),
    ]),
]
_BLOCKS = {k: b for k, _, b in BOOK}
if T.FLAVOR == "xp":
    BOOK = [(key, title.replace("kilix 95", T.PRODUCT_NAME),
             [(kind, text.replace("kilix 95", T.PRODUCT_NAME)
                     .replace("teal", "blue")) for kind, text in blocks])
            for key, title, blocks in BOOK]
    _BLOCKS = {key: blocks for key, _, blocks in BOOK}


class _RichText(W.Widget):
    """Scrollable formatted-text pane: bold headings, wrapped paragraphs,
    bullets with a hanging indent. Content set as (kind, text) blocks."""
    focusable = True
    LH = 15
    PAD = 6

    def __init__(self, x, y, w, h):
        super().__init__(x, y, w, h)
        self.blocks = []
        self.sb = W.VScroll()
        self._lines = []              # (font, x_indent, text, bullet)
        self._key = None              # (id(blocks), width) cache guard

    def set_blocks(self, blocks):
        self.blocks = blocks
        self.sb.pos = 0
        self._key = None
        self.invalidate()

    def plain(self):
        """The rendered text as one string (for tests / simple search)."""
        return "\n".join(t for _, t in self.blocks)

    def _avail(self):
        return self.w - 2 * self.PAD - T.SCROLL_W - 2

    def _wrap(self, font, text, maxw):
        out, cur = [], ""
        for word in text.split():
            trial = word if not cur else cur + " " + word
            if T.text_w(font, trial) <= maxw:
                cur = trial
            else:
                if cur:
                    out.append(cur)
                cur = word
        if cur:
            out.append(cur)
        return out or [""]

    def _relayout(self):
        avail = self._avail()
        lines = []
        for kind, text in self.blocks:
            if kind == "h":
                for ln in self._wrap(T.BOLD, text, avail):
                    lines.append((T.BOLD, self.PAD, ln, False))
                lines.append((T.FONT, self.PAD, "", False))
            elif kind == "b":
                wrapped = self._wrap(T.FONT, text, avail - 14)
                for i, ln in enumerate(wrapped):
                    lines.append((T.FONT, self.PAD + 14, ln, i == 0))
                lines.append((T.FONT, self.PAD, "", False))
            else:
                for ln in self._wrap(T.FONT, text, avail):
                    lines.append((T.FONT, self.PAD, ln, False))
                lines.append((T.FONT, self.PAD, "", False))
        self._lines = lines

    def _rows(self):
        return max(1, (self.h - 2 * self.PAD) // self.LH)

    def draw(self, d, img):
        key = (id(self.blocks), self.w)
        if key != self._key:
            self._relayout()
            self._key = key
        x0, y0 = self.x, self.y
        x1, y1 = x0 + self.w - 1, y0 + self.h - 1
        T.sunken(d, x0, y0, x1, y1)
        self.sb.total, self.sb.page = len(self._lines), self._rows()
        self.sb.clamp()
        self.sb.place(x1 - T.SCROLL_W - 1, y0 + 2, self.h - 4)
        avail = self._avail()
        for i in range(self._rows()):
            idx = self.sb.pos + i
            if idx >= len(self._lines):
                break
            font, xoff, text, bullet = self._lines[idx]
            yy = y0 + self.PAD + i * self.LH
            if bullet:
                d.rectangle([x0 + self.PAD + 3, yy + 4,
                             x0 + self.PAD + 7, yy + 8], fill=T.TEXT)
            if text:
                d.text((x0 + xoff, yy), T.ellipsize(font, text, avail),
                       font=font, fill=T.TEXT)
        if self.sb.total > self.sb.page:
            self.sb.draw(d)

    def on_mouse(self, ev):
        if (self.sb.total > self.sb.page
                and (self.sb.hit(ev.x, ev.y) or self.sb.drag is not None)):
            if self.sb.on_mouse(ev):
                self.invalidate()
            return True
        if ev.wheel:
            self.sb.pos += ev.wheel * 3
            self.sb.clamp()
            self.invalidate()
            return True
        return True

    def on_key(self, ev):
        step = {"ArrowUp": -1, "ArrowDown": 1,
                "PageUp": -self._rows(), "PageDown": self._rows()}.get(ev.key)
        if step is None:
            return False
        self.sb.pos += step
        self.sb.clamp()
        self.invalidate()
        return True


class Help(wm.Window):
    def __init__(self, desk, arg=None):
        super().__init__(desk, f"Help Topics - {T.PRODUCT_NAME}", 560, 400, icon="help")
        self.min_w, self.min_h = 380, 240
        self.hist, self.hist_i = [], -1
        cw, ch = self.client_size()
        self.b_back = self.add(W.Button(4, TB_Y + 2, 66, 22, "Back",
                                        icon="back", cb=lambda: self._go(-1)))
        self.b_fwd = self.add(W.Button(72, TB_Y + 2, 82, 22, "Forward",
                                       icon="forward", cb=lambda: self._go(+1)))
        gy = TB_Y + TB_H + 2
        gh = ch - gy - 4
        self.topics = self.add(W.ListBox(2, gy, LIST_W, gh,
                                         on_select=self._pick,
                                         on_activate=self._pick))
        self.body = self.add(_RichText(LIST_W + 6, gy, cw - LIST_W - 8, gh))
        self.topics.set_items([(None, title, key) for key, title, _ in BOOK])
        self.set_focus(self.topics)
        self._navigate(BOOK[0][0])

    def on_resize(self):
        cw, ch = self.client_size()
        gy = TB_Y + TB_H + 2
        gh = ch - gy - 4
        self.topics.h = gh
        self.body.w, self.body.h = cw - LIST_W - 8, gh
        self.body._key = None

    def draw_client(self, d, img):
        cw, _ = self.client_size()
        T.raised_thin(d, 0, TB_Y, cw - 1, TB_Y + TB_H - 1)

    # ── navigation ──────────────────────────────────────────────────────────
    def _pick(self, item):
        if item[2] != self.topic:
            self._navigate(item[2])

    def _navigate(self, key):
        self.hist = self.hist[:self.hist_i + 1] + [key]
        self.hist_i = len(self.hist) - 1
        self._show(key)

    def _go(self, step):
        i = self.hist_i + step
        if 0 <= i < len(self.hist):
            self.hist_i = i
            self._show(self.hist[i])

    def _show(self, key):
        self.topic = key
        self.body.set_blocks(_BLOCKS[key])
        for i, it in enumerate(self.topics.items):
            if it[2] == key:
                self.topics.sel = i
                break
        self.b_back.enabled = self.hist_i > 0
        self.b_fwd.enabled = self.hist_i < len(self.hist) - 1
        self.invalidate()
