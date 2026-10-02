"""kilix desktop theme: flavor identity, palette, metrics, fonts, bevels.

Everything visual that is shared between modules lives here so the look can
be tuned in one place. The default flavor is the classic Windows 95 look; an
XP flavor switches identity, colors, chrome accents and taskbar treatment at
runtime, selected by KILIX_DESKTOP_FLAVOR (or KILIX_FLAVOR). Text is rendered
with PIL in binary (non-antialiased) mode for the crisp bitmap-font feel.

Fonts: real MS fonts are never bundled (kilix is published); if you own them,
drop .ttf files into desktop/assets/fonts/ (gitignored) and they are picked
up by preference. Otherwise DejaVu Sans at 11 px is a close enough stand-in.
"""
import os

from PIL import ImageFont

_DEFAULT = object()

_BASE_METRICS = {
    "TITLE_H": 18,              # title bar height (inside the frame)
    "BORDER": 4,                # resizable window frame thickness
    "MENU_H": 19,               # in-window menu bar height
    "TASKBAR_H": 28,
    "SCROLL_W": 16,             # scrollbar thickness
    "ICON": 32,                 # large icon size (desktop, file manager)
    "ICON_S": 16,               # small icon size (title bars, menus, lists)
    "CELL_W": 75, "CELL_H": 66, # desktop / file-manager icon grid cell
    "START_W": 58,
    "WALL_IMAGE": None,
}

_FLAVORS = {
    "95": {
        "aliases": ("95", "win95", "windows95", "classic"),
        "PRODUCT_NAME": "kilix 95",
        "STYLE_NAME": "Windows 95-style",
        "RUNTIME_ID": "kilix95",
        "START_LABEL": "Start",
        "START_ICON": "flame",
        "START_SIDEBAR": "kilix 95",
        "FACE": (192, 192, 192),
        "LIGHT": (255, 255, 255),
        "LTGRAY": (223, 223, 223),
        "SHADOW": (128, 128, 128),
        "DKSHADOW": (0, 0, 0),
        "DESKTOP": (0, 128, 128),
        "TITLE_A": (0, 0, 128),
        "TITLE_A2": (0, 0, 128),
        "TITLE_A_TX": (255, 255, 255),
        "TITLE_I": (128, 128, 128),
        "TITLE_I2": (128, 128, 128),
        "TITLE_I_TX": (192, 192, 192),
        "WINDOW_BG": (255, 255, 255),
        "TEXT": (0, 0, 0),
        "DISABLED": (128, 128, 128),
        "SEL_BG": (0, 0, 128),
        "SEL_TX": (255, 255, 255),
        "INFO_BG": (255, 255, 225),
        "TASKBAR_A": (192, 192, 192),
        "TASKBAR_B": (192, 192, 192),
        "TRAY_BG": (192, 192, 192),
        "START_A": (192, 192, 192),
        "START_B": (192, 192, 192),
        "START_TX": (0, 0, 0),
    },
    "xp": {
        "aliases": ("xp", "winxp", "windowsxp", "luna"),
        "PRODUCT_NAME": "kilix XP",
        "STYLE_NAME": "Windows XP-style",
        "RUNTIME_ID": "kilixxp",
        "START_LABEL": "start",
        "START_ICON": "flame",
        "START_SIDEBAR": "kilix XP",
        "FACE": (235, 238, 226),
        "LIGHT": (255, 255, 255),
        "LTGRAY": (247, 248, 238),
        "SHADOW": (118, 134, 168),
        "DKSHADOW": (54, 71, 112),
        "DESKTOP": (78, 151, 218),
        "TITLE_A": (0, 78, 210),
        "TITLE_A2": (78, 156, 255),
        "TITLE_A_TX": (255, 255, 255),
        "TITLE_I": (113, 139, 184),
        "TITLE_I2": (184, 204, 235),
        "TITLE_I_TX": (235, 242, 255),
        "WINDOW_BG": (255, 255, 255),
        "TEXT": (0, 0, 0),
        "DISABLED": (132, 130, 132),
        "SEL_BG": (43, 103, 205),
        "SEL_TX": (255, 255, 255),
        "INFO_BG": (255, 255, 225),
        "TASKBAR_A": (58, 132, 247),
        "TASKBAR_B": (14, 81, 198),
        "TRAY_BG": (146, 196, 244),
        "START_A": (102, 196, 74),
        "START_B": (36, 139, 41),
        "START_TX": (255, 255, 255),
        "START_W": 64,
        # No bundled XP wallpaper in the builtin desktop; fall back to the solid
        # DESKTOP colour. (The external kilix-95 checkout ships its own.)
        "WALL_IMAGE": None,
    },
}

_ALIASES = {
    alias: key
    for key, meta in _FLAVORS.items()
    for alias in meta["aliases"]
}
FLAVOR = "95"


def normalize_flavor(name):
    key = str(name or "95").strip().lower().replace(" ", "")
    return _ALIASES.get(key, "95")


def flavor_name():
    return FLAVOR


def flavor_label(name=None):
    key = normalize_flavor(name or FLAVOR)
    return _FLAVORS[key]["PRODUCT_NAME"]


def flavor_options():
    return tuple((key, _FLAVORS[key]["PRODUCT_NAME"]) for key in ("95", "xp"))


def apply_flavor(name):
    """Switch the exported theme globals in-place and return the active key."""
    global FLAVOR
    key = normalize_flavor(name)
    meta = dict(_BASE_METRICS)
    meta.update(_FLAVORS[key])
    for k, v in meta.items():
        if k != "aliases":
            globals()[k] = v
    FLAVOR = key
    return FLAVOR


apply_flavor(os.environ.get("KILIX_DESKTOP_FLAVOR")
             or os.environ.get("KILIX_FLAVOR") or "95")

_here = os.path.dirname(os.path.abspath(__file__))


def _find_font(names, size):
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _candidates(bold):
    out = []
    assets = os.path.join(_here, "assets", "fonts")
    if os.path.isdir(assets):   # user-supplied fonts win (never committed)
        pref = [f for f in sorted(os.listdir(assets))
                if f.lower().endswith((".ttf", ".otf"))
                and (("bold" in f.lower()) == bold)]
        out += [os.path.join(assets, f) for f in pref]
    out += ["DejaVuSans-Bold.ttf"] if bold else ["DejaVuSans.ttf"]
    out += ["/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf"
            % ("-Bold" if bold else "")]
    return out


FONT   = _find_font(_candidates(False), 11)
BOLD   = _find_font(_candidates(True), 11)
SMALL  = _find_font(_candidates(False), 10)


def text_w(font, s):
    try:
        return int(font.getlength(s))
    except AttributeError:               # very old PIL
        return font.getsize(s)[0]


def ellipsize(font, s, max_w):
    if text_w(font, s) <= max_w:
        return s
    while s and text_w(font, s + "…") > max_w:
        s = s[:-1]
    return s + "…"


def _blend(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _hgradient(d, x0, y0, x1, y1, a, b):
    span = max(1, x1 - x0)
    for x in range(x0, x1 + 1):
        d.line([(x, y0), (x, y1)], fill=_blend(a, b, (x - x0) / span))


def _vgradient(d, x0, y0, x1, y1, a, b):
    span = max(1, y1 - y0)
    for y in range(y0, y1 + 1):
        d.line([(x0, y), (x1, y)], fill=_blend(a, b, (y - y0) / span))


# ── bevels ──────────────────────────────────────────────────────────────────
# All take an ImageDraw and an inclusive pixel rect (x0, y0, x1, y1).

def _edge(d, x0, y0, x1, y1, tl, br):
    d.line([(x0, y1), (x0, y0), (x1, y0)], fill=tl)          # top + left
    d.line([(x1, y0 + 1), (x1, y1), (x0 + 1, y1)], fill=br)  # right + bottom


def _fill_or_default(fill, default):
    return default if fill is _DEFAULT else fill


def raised(d, x0, y0, x1, y1, fill=_DEFAULT):
    """Button/taskbar face: white TL, black BR, gray inner BR."""
    fill = _fill_or_default(fill, FACE)
    if fill is not None:
        d.rectangle([x0, y0, x1, y1], fill=fill)
    _edge(d, x0, y0, x1, y1, LIGHT, DKSHADOW)
    _edge(d, x0 + 1, y0 + 1, x1 - 1, y1 - 1, fill or FACE, SHADOW)


def raised_thin(d, x0, y0, x1, y1, fill=_DEFAULT):
    """One-pixel raised edge (taskbar top, toolbar buttons at rest)."""
    fill = _fill_or_default(fill, FACE)
    if fill is not None:
        d.rectangle([x0, y0, x1, y1], fill=fill)
    _edge(d, x0, y0, x1, y1, LIGHT, SHADOW)


def pressed(d, x0, y0, x1, y1, fill=_DEFAULT):
    """A pushed-in button."""
    fill = _fill_or_default(fill, FACE)
    if fill is not None:
        d.rectangle([x0, y0, x1, y1], fill=fill)
    _edge(d, x0, y0, x1, y1, DKSHADOW, LIGHT)
    _edge(d, x0 + 1, y0 + 1, x1 - 1, y1 - 1, SHADOW, fill or FACE)


def sunken(d, x0, y0, x1, y1, fill=_DEFAULT):
    """Text fields, list boxes, the taskbar clock well."""
    fill = _fill_or_default(fill, WINDOW_BG)
    if fill is not None:
        d.rectangle([x0, y0, x1, y1], fill=fill)
    _edge(d, x0, y0, x1, y1, SHADOW, LIGHT)
    _edge(d, x0 + 1, y0 + 1, x1 - 1, y1 - 1, DKSHADOW, FACE)


def frame(d, x0, y0, x1, y1):
    """Window outer frame (the 2px 3D edge of the 4px border)."""
    _edge(d, x0, y0, x1, y1, LTGRAY, DKSHADOW)
    _edge(d, x0 + 1, y0 + 1, x1 - 1, y1 - 1, LIGHT, SHADOW)


def groove(d, x0, y0, x1, y1):
    """Etched-in group box / separator edge."""
    _edge(d, x0, y0, x1, y1, SHADOW, LIGHT)
    _edge(d, x0 + 1, y0 + 1, x1 - 1, y1 - 1, LIGHT, SHADOW)


def hsep(d, x0, x1, y):
    """Menu separator."""
    d.line([(x0, y), (x1, y)], fill=SHADOW)
    d.line([(x0, y + 1), (x1, y + 1)], fill=LIGHT)


def titlebar(d, x0, y0, x1, y1, active=True):
    """Paint a window title bar for the active flavor (flat 95, gradient XP)."""
    if FLAVOR == "xp":
        _hgradient(d, x0, y0, x1, y1,
                   TITLE_A if active else TITLE_I,
                   TITLE_A2 if active else TITLE_I2)
        d.line([(x0, y0), (x1, y0)], fill=_blend(LIGHT, TITLE_A2, 0.35))
    else:
        d.rectangle([x0, y0, x1, y1], fill=TITLE_A if active else TITLE_I)


def taskbar(d, x0, y0, x1, y1):
    """Paint the taskbar background for the active flavor."""
    if FLAVOR == "xp":
        _vgradient(d, x0, y0, x1, y1, TASKBAR_A, TASKBAR_B)
        d.line([(x0, y0), (x1, y0)], fill=(125, 176, 255))
    else:
        d.rectangle([x0, y0, x1, y1], fill=FACE)
        d.line([(x0, y0), (x1, y0)], fill=LIGHT)


def start_button(d, x0, y0, x1, y1, is_pressed=False):
    """Paint the Start button and return (content_offset, text_color)."""
    if FLAVOR == "xp":
        a, b = (START_B, START_A) if is_pressed else (START_A, START_B)
        _vgradient(d, x0, y0, x1, y1, a, b)
        d.rectangle([x0, y0, x1, y1], outline=(20, 86, 27))
        d.line([(x0 + 1, y0 + 1), (x1 - 1, y0 + 1)], fill=(160, 229, 136))
        return (1 if is_pressed else 0), START_TX
    if is_pressed:
        pressed(d, x0, y0, x1, y1)
        return 1, TEXT
    raised(d, x0, y0, x1, y1)
    return 0, TEXT


def focus_rect(d, x0, y0, x1, y1, on=_DEFAULT, off=_DEFAULT):
    """Dotted keyboard-focus marquee."""
    on = _fill_or_default(on, TEXT)
    off = _fill_or_default(off, FACE)
    for x in range(x0, x1 + 1):
        d.point((x, y0), fill=on if (x + y0) % 2 == 0 else off)
        d.point((x, y1), fill=on if (x + y1) % 2 == 0 else off)
    for y in range(y0, y1 + 1):
        d.point((x0, y), fill=on if (x0 + y) % 2 == 0 else off)
        d.point((x1, y), fill=on if (x1 + y) % 2 == 0 else off)
