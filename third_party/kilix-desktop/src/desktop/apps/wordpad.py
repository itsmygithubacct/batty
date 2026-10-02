"""kilix desktop — WordPad. Real rich text: paragraphs of styled runs, drawn
word-wrapped with the correct DejaVu variant per run (no antialiasing, manual
underline). Bold/Italic/Underline/size/colour apply to the selection or the
caret's typing style; documents round-trip losslessly through a JSON .krt and
flatten to plain .txt."""
import json
import os

import filedialog
import theme as T
import widgets as W
import wm

TB_Y = T.MENU_H + 2               # toolbar row (below the menu bar)
TB_H = 26
RULER_H = 14
STATUS_H = 20
TA_Y = TB_Y + TB_H + RULER_H + 1
SIZES = ["8", "9", "10", "11", "12", "14", "16", "18", "20", "24"]
COLORS = [("Black", (0, 0, 0)), ("Maroon", (128, 0, 0)),
          ("Red", (255, 0, 0)), ("Green", (0, 128, 0)),
          ("Olive", (128, 128, 0)), ("Blue", (0, 0, 255)),
          ("Navy", (0, 0, 128)), ("Purple", (128, 0, 128)),
          ("Teal", (0, 128, 128)), ("Gray", (128, 128, 128))]

STYLE_KEYS = ("bold", "italic", "underline", "size", "color")
DEFAULT = {"bold": False, "italic": False, "underline": False,
           "size": 11, "color": (0, 0, 0)}

# ── run model helpers (a paragraph is a list of run dicts) ──────────────────
_FONTS = {}


MAX_SIZE = 128                    # clamp so a loaded .krt can't grow _FONTS


def _font(bold, italic, size):
    key = (bool(bold), bool(italic), max(1, min(int(size), MAX_SIZE)))
    f = _FONTS.get(key)
    if f is None:
        suff = {(0, 0): "", (1, 0): "-Bold", (0, 1): "-Oblique",
                (1, 1): "-BoldOblique"}[(int(bool(bold)), int(bool(italic)))]
        f = T._find_font(["DejaVuSans%s.ttf" % suff,
                          "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf"
                          % suff], key[2])
        _FONTS[key] = f
    return f


def _run(text, style):
    r = {"text": text}
    r.update({k: style[k] for k in STYLE_KEYS})
    r["color"] = tuple(r["color"])[:3]
    return r


def _style_of(run):
    s = {k: run[k] for k in STYLE_KEYS}
    s["color"] = tuple(s["color"])[:3]
    return s


def _same(a, b):
    return all(a[k] == b[k] for k in STYLE_KEYS)


def _ptext(para):
    return "".join(r["text"] for r in para)


def _plen(para):
    return sum(len(r["text"]) for r in para)


def _segments(para, a, b):
    """Non-mutating (run, slice) list for char range [a, b) of a paragraph."""
    out, pos = [], 0
    for r in para:
        n = len(r["text"])
        s, e = max(a, pos), min(b, pos + n)
        if s < e:
            out.append((r, r["text"][s - pos:e - pos]))
        pos += n
    return out


def _split_at(para, off):
    """Ensure a run boundary at char offset off; return that boundary index."""
    pos = 0
    for i, r in enumerate(para):
        n = len(r["text"])
        if off == pos:
            return i
        if off < pos + n:
            cut = off - pos
            left = dict(r, text=r["text"][:cut])
            right = dict(r, text=r["text"][cut:])
            para[i:i + 1] = [left, right]
            return i + 1
        pos += n
    return len(para)


def _normalize(para):
    out = []
    for r in para:
        if r["text"] == "":
            continue
        if out and _same(out[-1], r):
            out[-1]["text"] += r["text"]
        else:
            out.append(dict(r))
    para[:] = out


# ── the rich editor widget ──────────────────────────────────────────────────
class RichTextArea(W.Widget):
    focusable = True

    def __init__(self, x, y, w, h):
        super().__init__(x, y, w, h)
        self.paras = [[]]                 # list of paragraphs (list of runs)
        self.caret = (0, 0)               # (para, char offset)
        self.anchor = None                # (para, char offset) or None
        self.style = dict(DEFAULT)        # typing style at the caret
        self.goal_x = 0
        self.sb = W.VScroll()
        self.on_change = None             # doc edited
        self.on_state = None              # caret/selection/doc touched
        self._ver = 0
        self._laid = None                 # (w, ver) the layout was built for
        self._vlines = []

    # ── document I/O ─────────────────────────────────────────────────────────
    def set_doc(self, paras):
        self.paras = paras or [[]]
        for p in self.paras:
            _normalize(p)
        self.caret, self.anchor = (0, 0), None
        self.style = dict(DEFAULT)
        self.sb.pos = 0
        self._edited()
        self.invalidate()

    def set_plain(self, text):
        self.set_doc([[_run(ln, DEFAULT)] if ln else []
                      for ln in text.replace("\r\n", "\n").split("\n")])

    def plain_text(self):
        return "\n".join(_ptext(p) for p in self.paras)

    def to_obj(self):
        return {"kilix_rich": 1, "paras": [
            [{"text": r["text"], "bold": r["bold"], "italic": r["italic"],
              "underline": r["underline"], "size": r["size"],
              "color": list(r["color"])} for r in p] for p in self.paras]}

    def from_obj(self, obj):
        paras = []
        for p in obj.get("paras", []):
            runs = []
            for r in p:
                st = {"bold": bool(r.get("bold")),
                      "italic": bool(r.get("italic")),
                      "underline": bool(r.get("underline")),
                      "size": int(r.get("size", 11)),
                      "color": tuple(r.get("color", (0, 0, 0)))[:3]}
                runs.append(_run(str(r.get("text", "")), st))
            paras.append(runs)
        self.set_doc(paras)

    # ── selection helpers ────────────────────────────────────────────────────
    def _sel(self):
        if self.anchor is None or self.anchor == self.caret:
            return None
        return tuple(sorted([self.anchor, self.caret]))

    def counts(self):
        text = self.plain_text()
        return len(text.split()), len(text), len(self.paras)

    # ── style query / application ────────────────────────────────────────────
    def active(self, key):
        sel = self._sel()
        if not sel:
            return bool(self.style[key])
        (ar, ac), (br, bc) = sel
        found = False
        for pi in range(ar, br + 1):
            a = ac if pi == ar else 0
            b = bc if pi == br else _plen(self.paras[pi])
            for r, _ in _segments(self.paras[pi], a, b):
                found = True
                if not r[key]:
                    return False
        return found

    def current_size(self):
        sel = self._sel()
        if not sel:
            return self.style["size"]
        (ar, ac), (br, bc) = sel
        sizes = set()
        for pi in range(ar, br + 1):
            a = ac if pi == ar else 0
            b = bc if pi == br else _plen(self.paras[pi])
            for r, _ in _segments(self.paras[pi], a, b):
                sizes.add(r["size"])
        return sizes.pop() if len(sizes) == 1 else self.style["size"]

    def set_style(self, key, value):
        sel = self._sel()
        if not sel:
            self.style = dict(self.style)
            self.style[key] = value
            self.invalidate()
            if self.on_state:
                self.on_state()
            return
        (ar, ac), (br, bc) = sel
        for pi in range(ar, br + 1):
            a = ac if pi == ar else 0
            b = bc if pi == br else _plen(self.paras[pi])
            if a < b:
                para = self.paras[pi]
                i = _split_at(para, a)
                j = _split_at(para, b)
                for r in para[i:j]:
                    r[key] = value
                _normalize(para)
        self._edited()
        self.invalidate()
        if self.on_state:
            self.on_state()

    def toggle(self, key):
        self.set_style(key, not self.active(key))

    # ── editing ──────────────────────────────────────────────────────────────
    def _del_sel(self):
        sel = self._sel()
        if not sel:
            return False
        (ar, ac), (br, bc) = sel
        pa = self.paras[ar]
        ia = _split_at(pa, ac)
        left = pa[:ia]
        pb = self.paras[br]
        ib = _split_at(pb, bc)
        right = pb[ib:]
        merged = [dict(r) for r in left] + [dict(r) for r in right]
        _normalize(merged)
        self.paras[ar:br + 1] = [merged]
        self.caret, self.anchor = (ar, ac), None
        return True

    def _sel_text(self):
        sel = self._sel()
        if not sel:
            return ""
        (ar, ac), (br, bc) = sel
        if ar == br:
            return _ptext(self.paras[ar])[ac:bc]
        parts = [_ptext(self.paras[ar])[ac:]]
        parts += [_ptext(self.paras[pi]) for pi in range(ar + 1, br)]
        parts += [_ptext(self.paras[br])[:bc]]
        return "\n".join(parts)

    def insert(self, s):
        self._del_sel()
        s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
        pi, off = self.caret
        para = self.paras[pi]
        i = _split_at(para, off)
        left, right = para[:i], para[i:]
        st = self.style
        parts = s.split("\n")
        if len(parts) == 1:
            body = [_run(parts[0], st)] if parts[0] else []
            newp = left + body + right
            _normalize(newp)
            self.paras[pi] = newp
            self.caret = (pi, off + len(parts[0]))
        else:
            first = left + ([_run(parts[0], st)] if parts[0] else [])
            _normalize(first)
            newparas = [first]
            for mid in parts[1:-1]:
                p = [_run(mid, st)] if mid else []
                newparas.append(p)
            last = ([_run(parts[-1], st)] if parts[-1] else []) + right
            _normalize(last)
            newparas.append(last)
            self.paras[pi:pi + 1] = newparas
            self.caret = (pi + len(parts) - 1, len(parts[-1]))
        self.anchor = None
        self._edited()
        return True

    def _backspace(self):
        if self._del_sel():
            return True
        pi, off = self.caret
        if off > 0:
            para = self.paras[pi]
            j = _split_at(para, off - 1)
            i = _split_at(para, off)
            del para[j:i]
            _normalize(para)
            self.caret = (pi, off - 1)
            return True
        if pi > 0:
            prev = self.paras[pi - 1]
            plen = _plen(prev)
            merged = [dict(r) for r in prev] + [dict(r) for r in self.paras[pi]]
            _normalize(merged)
            self.paras[pi - 1:pi + 1] = [merged]
            self.caret = (pi - 1, plen)
            return True
        return False

    def _delete(self):
        if self._del_sel():
            return True
        pi, off = self.caret
        para = self.paras[pi]
        if off < _plen(para):
            j = _split_at(para, off)
            i = _split_at(para, off + 1)
            del para[j:i]
            _normalize(para)
            return True
        if pi < len(self.paras) - 1:
            merged = [dict(r) for r in para] + \
                     [dict(r) for r in self.paras[pi + 1]]
            _normalize(merged)
            self.paras[pi:pi + 2] = [merged]
            return True
        return False

    # ── layout (word wrap) ───────────────────────────────────────────────────
    def _maxw(self):
        return max(8, self.w - T.SCROLL_W - 8)

    def _measure(self, para, a, b):
        return sum(T.text_w(_font(r["bold"], r["italic"], r["size"]), t)
                   for r, t in _segments(para, a, b))

    def _wrap_para(self, para, maxw):
        text = _ptext(para)
        n = len(text)
        if n == 0:
            return [(0, 0)]
        ends, i = [], 0
        while i < n:                       # chunk = word plus trailing spaces
            j = i
            while j < n and text[j] != " ":
                j += 1
            while j < n and text[j] == " ":
                j += 1
            ends.append(j)
            i = j
        res, ls, cur = [], 0, 0
        for e in ends:
            if cur == ls:
                cur = e
            elif self._measure(para, ls, e) <= maxw:
                cur = e
            else:
                res.append((ls, cur))
                ls, cur = cur, e
        res.append((ls, cur))
        return res

    def _ensure_layout(self):
        if self._laid == (self.w, self._ver):
            return
        maxw = self._maxw()
        vls, top = [], 0
        for pi, para in enumerate(self.paras):
            for a, b in self._wrap_para(para, maxw):
                asc = desc = 0
                for r, _ in _segments(para, a, b):
                    am, dm = _font(r["bold"], r["italic"], r["size"]).getmetrics()
                    asc, desc = max(asc, am), max(desc, dm)
                if asc == 0:
                    asc, desc = _font(False, False, 11).getmetrics()
                h = asc + desc + 2
                vls.append({"pi": pi, "a": a, "b": b, "asc": asc,
                            "desc": desc, "h": h, "top": top})
                top += h
        self._vlines = vls
        self._total = top
        self._laid = (self.w, self._ver)

    def _locate(self, pi, off):
        self._ensure_layout()
        for k, vl in enumerate(self._vlines):
            if vl["pi"] != pi:
                continue
            if vl["a"] <= off < vl["b"]:
                return k
            if off == vl["b"]:
                nxt = self._vlines[k + 1] if k + 1 < len(self._vlines) else None
                if not (nxt and nxt["pi"] == pi and nxt["a"] == off):
                    return k
        return 0

    def _xtable(self, vl):
        """(x_local, offset) pairs for every caret position in a visual line."""
        para = self.paras[vl["pi"]]
        xs, offs, base, pos = [0], [vl["a"]], 0, vl["a"]
        for r, txt in _segments(para, vl["a"], vl["b"]):
            f = _font(r["bold"], r["italic"], r["size"])
            for j in range(1, len(txt) + 1):
                xs.append(base + T.text_w(f, txt[:j]))
                offs.append(pos + j)
            base = xs[-1]
            pos += len(txt)
        return xs, offs

    def _x_of(self, vl, off):
        return self._measure(self.paras[vl["pi"]], vl["a"], off)

    def _reveal(self):
        self._ensure_layout()
        page = self.h - 4
        self.sb.total, self.sb.page = self._total, page
        k = self._locate(*self.caret)
        vl = self._vlines[k]
        if vl["top"] < self.sb.pos:
            self.sb.pos = vl["top"]
        if vl["top"] + vl["h"] > self.sb.pos + page:
            self.sb.pos = vl["top"] + vl["h"] - page
        self.sb.clamp()

    # ── drawing ──────────────────────────────────────────────────────────────
    def draw(self, d, img):
        from PIL import Image
        x0, y0 = self.x, self.y
        x1, y1 = x0 + self.w - 1, y0 + self.h - 1
        T.sunken(d, x0, y0, x1, y1)
        self._ensure_layout()
        page = self.h - 4
        self.sb.total, self.sb.page = self._total, page
        self.sb.clamp()
        self.sb.place(x1 - T.SCROLL_W - 1, y0 + 2, self.h - 4)
        maxw = self._maxw()
        sel = self._sel()
        focused = self.window and self.window.focus is self
        clip = Image.new("RGB", (maxw, max(1, self.h - 4)), T.WINDOW_BG)
        cd = W.drawer(clip)
        ox, oy = x0 + 4, y0 + 2
        for vl in self._vlines:
            vy = vl["top"] - self.sb.pos
            if vy + vl["h"] < 0 or vy > self.h - 4:
                continue
            para = self.paras[vl["pi"]]
            baseline = vy + vl["asc"] + 1
            # selection background
            if sel:
                (ar, ac), (br, bc) = sel
                if ar <= vl["pi"] <= br:
                    sa = ac if vl["pi"] == ar else vl["a"]
                    sb = bc if vl["pi"] == br else vl["b"]
                    sa, sb = max(sa, vl["a"]), min(sb, vl["b"])
                    tail = 4 if (vl["pi"] < br and sb == vl["b"]) else 0
                    if sb >= sa:
                        sx0 = self._x_of(vl, sa)
                        sx1 = self._x_of(vl, sb) + tail
                        cd.rectangle([sx0, vy, sx1 - 1, vy + vl["h"] - 1],
                                     fill=T.SEL_BG)
            # runs
            x = 0
            for r, txt in _segments(para, vl["a"], vl["b"]):
                f = _font(r["bold"], r["italic"], r["size"])
                cd.text((x, baseline), txt, font=f, fill=r["color"],
                        anchor="ls")
                wpx = T.text_w(f, txt)
                if r["underline"]:
                    cd.line([(x, baseline + 1), (x + wpx - 1, baseline + 1)],
                            fill=r["color"])
                x += wpx
            # selected text redrawn white
            if sel:
                (ar, ac), (br, bc) = sel
                if ar <= vl["pi"] <= br:
                    sa = max(ac if vl["pi"] == ar else vl["a"], vl["a"])
                    sb = min(bc if vl["pi"] == br else vl["b"], vl["b"])
                    xx = self._x_of(vl, sa)
                    for r, txt in _segments(para, sa, sb):
                        f = _font(r["bold"], r["italic"], r["size"])
                        cd.text((xx, baseline), txt, font=f, fill=T.SEL_TX,
                                anchor="ls")
                        wpx = T.text_w(f, txt)
                        if r["underline"]:
                            cd.line([(xx, baseline + 1),
                                     (xx + wpx - 1, baseline + 1)],
                                    fill=T.SEL_TX)
                        xx += wpx
        img.paste(clip, (ox, oy))
        # caret
        if focused and self.window.caret_on:
            k = self._locate(*self.caret)
            if self._vlines:
                vl = self._vlines[k]
                cx = ox + self._x_of(vl, self.caret[1])
                cy = oy + vl["top"] - self.sb.pos
                if oy <= cy <= y1 - 2 and cx <= x1 - T.SCROLL_W - 2:
                    d.line([(cx, max(oy, cy)),
                            (cx, min(y1 - 2, cy + vl["h"] - 1))], fill=T.TEXT)
        self.sb.draw(d)

    # ── input ────────────────────────────────────────────────────────────────
    def _move(self, pi, off, keep):
        pi = max(0, min(pi, len(self.paras) - 1))
        off = max(0, min(off, _plen(self.paras[pi])))
        if keep:
            if self.anchor is None:
                self.anchor = self.caret
        else:
            self.anchor = None
        self.caret = (pi, off)

    def _hit(self, lx, ly):
        self._ensure_layout()
        cy = ly - 2 + self.sb.pos
        target = None
        for vl in self._vlines:
            if vl["top"] <= cy < vl["top"] + vl["h"]:
                target = vl
                break
        if target is None:
            target = (self._vlines[-1] if cy >= 0 else self._vlines[0]) \
                if self._vlines else None
        if target is None:
            return 0, 0
        xs, offs = self._xtable(target)
        px = lx - 4
        best, bd = 0, None
        for i, xv in enumerate(xs):
            dd = abs(xv - px)
            if bd is None or dd < bd:
                bd, best = dd, i
        return target["pi"], offs[best]

    def on_mouse(self, ev):
        lx, ly = ev.x - self.x, ev.y - self.y
        if self.sb.hit(ev.x, ev.y) or self.sb.drag is not None:
            if self.sb.on_mouse(ev, line=30):
                self.invalidate()
            if ev.press or self.sb.drag is not None:
                return True
        if ev.wheel:
            self.sb.pos += ev.wheel * 45
            self.sb.clamp()
            self.invalidate()
            return True
        pi, off = self._hit(lx, ly)
        if ev.press and ev.btn == 1:
            self._move(pi, off, ev.shift)
            if not ev.shift:
                self.anchor = self.caret
            self._after(True)
        elif ev.move and (ev.btn & 1):
            self.caret = (pi, off)
            self._reveal()
            self.invalidate()
            if self.on_state:
                self.on_state()
        elif not ev.press and not ev.move and self.anchor == self.caret:
            self.anchor = None
            self.invalidate()
        return True

    def on_key(self, ev):
        k = ev.key
        pi, off = self.caret
        para = self.paras[pi]
        moved = True
        if k == "ArrowLeft":
            if off > 0:
                self._move(pi, off - 1, ev.shift)
            elif pi > 0:
                self._move(pi - 1, _plen(self.paras[pi - 1]), ev.shift)
        elif k == "ArrowRight":
            if off < _plen(para):
                self._move(pi, off + 1, ev.shift)
            elif pi < len(self.paras) - 1:
                self._move(pi + 1, 0, ev.shift)
        elif k in ("ArrowUp", "ArrowDown"):
            self._vmove(-1 if k == "ArrowUp" else 1, ev.shift)
        elif k in ("PageUp", "PageDown"):
            self._vmove(-8 if k == "PageUp" else 8, ev.shift)
        elif k == "Home":
            self._ensure_layout()
            vl = self._vlines[self._locate(pi, off)]
            self._move(pi, vl["a"], ev.shift)
        elif k == "End":
            self._ensure_layout()
            vl = self._vlines[self._locate(pi, off)]
            self._move(pi, vl["b"], ev.shift)
        elif ev.ctrl and k == "a":
            self.anchor = (0, 0)
            self.caret = (len(self.paras) - 1, _plen(self.paras[-1]))
        elif ev.ctrl and k in ("c", "x"):
            t = self._sel_text()
            if t:
                if self.desk:
                    self.desk.set_clipboard(t)
                if k == "x":
                    self._del_sel()
                    self._edited()
                    moved = True
        elif ev.ctrl and k == "v":
            self.insert(self.desk.clipboard if self.desk else "")
        elif k == "Enter":
            self.insert("\n")
        elif k == "Tab":
            self.insert("    ")
        elif k == "Backspace":
            self._backspace()
        elif k == "Delete":
            self._delete()
        elif ev.text and not ev.ctrl and not ev.alt:
            self.insert(ev.text)
        else:
            return False
        if k not in ("ArrowUp", "ArrowDown", "PageUp", "PageDown"):
            self._ensure_layout()
            vl = self._vlines[self._locate(*self.caret)]
            self.goal_x = self._x_of(vl, self.caret[1])
        self._after(moved)
        return True

    def _vmove(self, dv, keep):
        self._ensure_layout()
        if not self._vlines:
            return
        k = self._locate(*self.caret)
        nk = max(0, min(k + dv, len(self._vlines) - 1))
        vl = self._vlines[nk]
        xs, offs = self._xtable(vl)
        best, bd = 0, None
        for i, xv in enumerate(xs):
            dd = abs(xv - self.goal_x)
            if bd is None or dd < bd:
                bd, best = dd, i
        self._move(vl["pi"], offs[best], keep)

    def _after(self, moved):
        if moved and self.anchor is None:
            self._sync_style()
        self._reveal()
        self.invalidate()
        if self.on_state:
            self.on_state()

    def _sync_style(self):
        pi, off = self.caret
        para = self.paras[pi]
        if not para:
            return
        src = off - 1 if off > 0 else 0
        for r, _ in _segments(para, src, src + 1):
            self.style = _style_of(r)
            return

    def _edited(self):
        self._ver += 1
        if self.on_change:
            self.on_change()


# ── the window ──────────────────────────────────────────────────────────────
class WordPad(wm.Window):
    def __init__(self, desk, path=None):
        super().__init__(desk, "Document - WordPad", 600, 430, icon="wordpad")
        self.min_w, self.min_h = 440, 260
        self.path = None
        self.modified = False
        self._find_term = ""
        self.n_words = self.n_chars = self.n_lines = 0
        cw, ch = self.client_size()
        self.menubar = self.add(W.MenuBar(cw, [
            ("File", self._file_menu), ("Edit", self._edit_menu),
            ("Format", self._format_menu), ("Help", self._help_menu)]))
        y = TB_Y + 2
        self.add(W.Button(4, y, 40, 22, "New", cb=self._new))
        self.add(W.Button(46, y, 44, 22, "Open", cb=self._open))
        self.add(W.Button(92, y, 44, 22, "Save", cb=self._save))
        self.b_bold = self.add(W.Button(146, y, 24, 22, "B",
                                        cb=lambda: self._fmt("bold")))
        self.b_ital = self.add(W.Button(172, y, 24, 22, "I",
                                        cb=lambda: self._fmt("italic")))
        self.b_undl = self.add(W.Button(198, y, 24, 22, "U",
                                        cb=lambda: self._fmt("underline")))
        self.size_dd = self.add(W.Dropdown(258, TB_Y + 3, 52, SIZES,
                                           index=SIZES.index("11"),
                                           cb=self._set_size))
        self.add(W.Button(314, y, 52, 22, "Color", cb=self._color_menu))
        self.rta = self.add(RichTextArea(2, TA_Y, cw - 4,
                                         ch - TA_Y - STATUS_H - 2))
        self.rta.on_change = self._changed
        self.rta.on_state = self._state
        self.set_focus(self.rta)
        self._refresh_tb()
        if path:
            self._load(path)

    # ── layout ───────────────────────────────────────────────────────────────
    def on_resize(self):
        cw, ch = self.client_size()
        self.menubar.w = cw
        self.rta.w = cw - 4
        self.rta.h = ch - TA_Y - STATUS_H - 2

    def draw_client(self, d, img):
        cw, ch = self.client_size()
        T.raised_thin(d, 0, TB_Y, cw - 1, TB_Y + TB_H - 1)
        d.text((228, TB_Y + 7), "Size", font=T.FONT, fill=T.TEXT)
        ry = TB_Y + TB_H
        T.sunken(d, 4, ry + 1, cw - 5, ry + RULER_H - 2, fill=T.WINDOW_BG)
        for x in range(8, cw - 8, 48):
            d.line([(x, ry + RULER_H - 6), (x, ry + RULER_H - 3)],
                   fill=T.SHADOW)
        for x in range(32, cw - 8, 48):
            d.point((x, ry + RULER_H - 4), fill=T.SHADOW)
        T.sunken(d, 2, ch - STATUS_H, cw - 3, ch - 3, fill=T.FACE)
        msg = (f"Words: {self.n_words}   Chars: {self.n_chars}   "
               f"Ln {self.rta.caret[0] + 1}, Col {self.rta.caret[1] + 1}")
        d.text((8, ch - STATUS_H + 3), msg, font=T.FONT, fill=T.TEXT)

    # ── toolbar plumbing ─────────────────────────────────────────────────────
    def _refresh_tb(self):
        self.b_bold.toggled = self.rta.active("bold")
        self.b_ital.toggled = self.rta.active("italic")
        self.b_undl.toggled = self.rta.active("underline")
        sz = str(self.rta.current_size())
        if sz in SIZES:
            self.size_dd.index = SIZES.index(sz)
        self.invalidate()

    def _state(self):
        self._recount()
        self._refresh_tb()

    def _recount(self):
        self.n_words, self.n_chars, self.n_lines = self.rta.counts()

    def _fmt(self, key):
        self.rta.toggle(key)
        self.set_focus(self.rta)
        self._refresh_tb()

    def _set_size(self, val):
        self.rta.set_style("size", int(val))
        self.set_focus(self.rta)
        self._refresh_tb()

    def _color_menu(self):
        gx, gy = self.client_origin()
        b = next(w for w in self.widgets
                 if isinstance(w, W.Button) and w.text == "Color")
        items = [W.MenuItem(name, action=lambda c=col: self._set_color(c))
                 for name, col in COLORS]
        self.desk.menus.open(items, gx + b.x, gy + b.y + b.h)

    def _set_color(self, col):
        self.rta.set_style("color", col)
        self.set_focus(self.rta)

    # ── find ─────────────────────────────────────────────────────────────────
    def find_next(self, term):
        if not term:
            return False
        rta = self.rta
        pi0, off0 = rta.caret
        n = len(rta.paras)
        for di in range(n + 1):
            pi = (pi0 + di) % n
            text = _ptext(rta.paras[pi])
            start = off0 if di == 0 else 0
            idx = text.find(term, start)
            if idx != -1:
                rta.anchor = (pi, idx)
                rta.caret = (pi, idx + len(term))
                rta._reveal()
                rta.invalidate()
                self._find_term = term
                self._state()
                return True
        return False

    def _find(self):
        def do(t):
            if t and not self.find_next(t):
                wm.msgbox(self.desk, "WordPad", f'Cannot find "{t}".',
                          icon="info")
        wm.inputbox(self.desk, "Find", "Find what:", self._find_term,
                    cb=do, icon="wordpad")

    # ── file plumbing ────────────────────────────────────────────────────────
    def _retitle(self):
        name = os.path.basename(self.path) if self.path else "Document"
        self.title = f"{'*' if self.modified else ''}{name} - WordPad"
        self.invalidate()

    def _changed(self):
        self._recount()
        if not self.modified:
            self.modified = True
        self._retitle()

    def _is_rich(self, path):
        return path.lower().endswith(".krt")     # only .krt is kilix rich JSON

    def _load(self, path):
        path = os.path.expanduser(path)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                data = f.read()
        except OSError as e:
            wm.msgbox(self.desk, "WordPad", str(e), icon="error")
            return
        try:
            if self._is_rich(path):
                self.rta.from_obj(json.loads(data))
            else:
                self.rta.set_plain(data)
        except (ValueError, KeyError, TypeError, AttributeError):
            self.rta.set_plain(data)     # malformed JSON (null/array/non-dict run)
        self.path = path
        self.modified = False
        self.desk.shell.add_recent(path)
        self._retitle()
        self._state()

    def _save(self, then=None, path=None):
        target = os.path.expanduser(path) if path else self.path
        if not target:
            return self._save_as(then)
        if not os.path.splitext(target)[1]:      # bare path → rich, not flattened
            target += ".krt"
        try:
            with open(target, "w", encoding="utf-8") as f:
                if self._is_rich(target):
                    json.dump(self.rta.to_obj(), f)
                else:
                    f.write(self.rta.plain_text())
        except OSError as e:
            wm.msgbox(self.desk, "WordPad", str(e), icon="error")
            return
        self.path = target
        self.modified = False
        self._retitle()
        if then:
            then()

    def _save_as(self, then=None):
        def do(path):
            if path:
                self._save(then, path=path)
        filedialog.save_file(
            self.desk, "Save As", do,
            start=os.path.dirname(self.path) if self.path else None,
            filters=[("WordPad Documents", "*.krt"), ("Text Files", "*.txt"),
                     ("All Files", "*.*")],
            filename=os.path.basename(self.path) if self.path else "document.krt")

    def _open(self):
        def go():
            filedialog.open_file(
                self.desk, "Open", lambda p: p and self._load(p),
                start=os.path.dirname(self.path) if self.path else None,
                filters=[("WordPad Documents", "*.krt;*.rtf"),
                         ("Text Files", "*.txt"), ("All Files", "*.*")])
        self._if_saved(go)

    def _new(self):
        def go():
            self.path = None
            self.rta.set_doc([[]])
            self.modified = False
            self._retitle()
            self._state()
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
        wm.msgbox(self.desk, "WordPad",
                  "The document has changed.\nSave the changes?",
                  icon="warn", buttons=("Yes", "No", "Cancel"), cb=do)

    def request_close(self):
        self._if_saved(self.close)

    # ── menus ────────────────────────────────────────────────────────────────
    def _file_menu(self):
        MI, sep = W.MenuItem, W.sep
        return [
            MI("New", action=self._new),
            MI("Open…", action=self._open),
            MI("Save", action=self._save),
            MI("Save As…", action=self._save_as),
            sep(),
            MI("Close", action=self.request_close),
        ]

    def _edit_menu(self):
        MI, sep = W.MenuItem, W.sep
        rta = self.rta

        def key(k, ctrl=True):
            return lambda: rta.on_key(W.Ev(kind="key", key=k, ctrl=ctrl))
        return [
            MI("Cut", action=key("x")),
            MI("Copy", action=key("c")),
            MI("Paste", action=key("v")),
            MI("Select All", action=key("a")),
            sep(),
            MI("Find…", action=self._find),
        ]

    def _format_menu(self):
        MI = W.MenuItem
        return [
            MI("Bold", checked=self.rta.active("bold"),
               action=lambda: self._fmt("bold")),
            MI("Italic", checked=self.rta.active("italic"),
               action=lambda: self._fmt("italic")),
            MI("Underline", checked=self.rta.active("underline"),
               action=lambda: self._fmt("underline")),
            W.sep(),
            MI("Color…", action=self._color_menu),
        ]

    def _help_menu(self):
        return [W.MenuItem(
            "About WordPad…", icon="wordpad",
            action=lambda: wm.msgbox(
                self.desk, "About WordPad",
                "kilix 95 WordPad\nRich text · Bold/Italic/Underline\n"
                "Colour · sizes · .krt rich format · .txt export.",
                icon="wordpad"))]

    def on_key(self, ev):
        if ev.ctrl and ev.key == "s":
            self._save(); return True
        if ev.ctrl and ev.key == "o":
            self._open(); return True
        if ev.ctrl and ev.key == "n":
            self._new(); return True
        if ev.ctrl and ev.key == "f":
            self._find(); return True
        if ev.ctrl and ev.key == "b":
            self._fmt("bold"); return True
        if ev.ctrl and ev.key == "i":
            self._fmt("italic"); return True
        if ev.ctrl and ev.key == "u":
            self._fmt("underline"); return True
        if ev.key == "F3" and self._find_term:
            self.find_next(self._find_term); return True
        return super().on_key(ev)
