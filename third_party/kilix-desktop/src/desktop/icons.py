"""kilix desktop — the icon set, drawn in code.

No Microsoft artwork is bundled (kilix is published); every icon is original
pixel art in the Windows 95 16-color idiom. Icons are authored on a 16×16
grid and rendered at an integer scale (16 px for menus/title bars, 32 px for
the desktop and file manager) so they stay crisp and chunky at both sizes.

    icons.get("folder", 32)      -> cached RGBA Image
    icons.paint(img, "folder", x, y, 32[, shortcut=True])
"""
from PIL import Image, ImageDraw

# the 16-color palette (single-char keys used by the drawing code)
K = (0, 0, 0)
W = (255, 255, 255)
G = (128, 128, 128)
S = (192, 192, 192)
R = (255, 0, 0)
DR = (128, 0, 0)
Y = (255, 255, 0)
DY = (128, 128, 0)
B = (0, 0, 255)
DB = (0, 0, 128)
C = (0, 255, 255)
T = (0, 128, 128)
N = (0, 255, 0)
DN = (0, 128, 0)


class P:
    """A 16×16 logical canvas rendered at integer scale u (1 → 16px, 2 → 32px).
    Coordinates are inclusive, in 16-space; everything lands on whole device
    pixels so the art stays sharp at every size."""

    def __init__(self, u):
        self.u = u
        self.img = Image.new("RGBA", (16 * u, 16 * u), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.img)

    def rect(self, x0, y0, x1, y1, fill=None, outline=None):
        u = self.u
        self.d.rectangle([x0 * u, y0 * u, (x1 + 1) * u - 1, (y1 + 1) * u - 1],
                         fill=fill, outline=outline, width=u)

    def px(self, x, y, c):
        self.rect(x, y, x, y, fill=c)

    def hline(self, x0, x1, y, c):
        self.rect(x0, y, x1, y, fill=c)

    def vline(self, x, y0, y1, c):
        self.rect(x, y0, x, y1, fill=c)

    def poly(self, pts, fill=None, outline=None):
        u = self.u
        # +0.5u centers vertices on logical pixels; good enough for our shapes
        sp = [((x + 0.5) * u, (y + 0.5) * u) for x, y in pts]
        self.d.polygon(sp, fill=fill, outline=outline, width=u)


# ── the icons (each draws into a P) ─────────────────────────────────────────

def _computer(p):
    p.rect(1, 1, 13, 10, fill=S, outline=K)        # monitor shell
    p.rect(3, 3, 11, 8, fill=T, outline=K)         # screen
    p.hline(4, 6, 4, C)                            # glint
    p.rect(6, 11, 8, 11, fill=S)                   # neck
    p.rect(2, 12, 12, 14, fill=S, outline=K)       # base
    p.hline(4, 8, 13, G)                           # drive slot
    p.px(11, 13, DN)                               # power led


def _display(p):
    _computer(p)
    p.rect(5, 4, 9, 7, fill=S, outline=K)          # a little window on screen
    p.hline(5, 9, 4, DB)


def _drive(p):
    p.rect(1, 5, 14, 11, fill=S, outline=K)
    p.hline(2, 13, 6, W)
    p.hline(3, 9, 9, G)                            # slot
    p.px(12, 9, DN)                                # led


def _folder(p, open_=False):
    p.rect(1, 3, 6, 4, fill=Y, outline=K)          # tab
    p.rect(1, 4, 14, 13, fill=Y, outline=K)        # body
    p.hline(2, 13, 5, W)                           # top hilight
    if open_:
        p.poly([(3, 7), (15, 7), (12, 13), (0, 13)], fill=Y, outline=K)
        p.hline(4, 13, 8, W)


def _folder_open(p):
    _folder(p, open_=True)


def _doc(p, deco=None):
    p.rect(3, 1, 12, 14, fill=W, outline=K)
    p.poly([(9, 1), (12, 4), (9, 4)], fill=S, outline=K)   # dog-ear
    if deco == "text":
        for y in (6, 8, 10, 12):
            p.hline(5, 10, y, B)
    elif deco == "image":
        p.rect(5, 5, 10, 11, fill=C, outline=K)
        p.poly([(5, 11), (7, 8), (10, 11)], fill=DN)       # hill
        p.px(9, 6, Y)                                      # sun


def _doc_text(p):
    _doc(p, "text")


def _doc_image(p):
    _doc(p, "image")


def _doc_audio(p):
    _doc(p)                                        # a document with a note
    p.rect(6, 9, 6, 12, fill=DB)                   # stems
    p.rect(10, 8, 10, 11, fill=DB)
    p.rect(6, 8, 10, 9, fill=DB)                   # beam
    p.rect(4, 11, 6, 13, fill=K)                   # note heads
    p.rect(8, 10, 10, 12, fill=K)


def _exe(p):
    p.rect(1, 2, 14, 13, fill=S, outline=K)
    p.rect(2, 3, 13, 4, fill=DB)                   # title bar
    p.px(13, 3, R)
    p.rect(4, 7, 8, 11, fill=W, outline=K)         # content + play arrow
    p.poly([(10, 7), (13, 9), (10, 11)], fill=DN, outline=K)


def _terminal(p):
    p.rect(1, 2, 14, 13, fill=S, outline=K)
    p.rect(2, 3, 13, 12, fill=K)
    p.hline(3, 4, 4, W)                            # C:\>
    p.px(6, 4, W)
    p.rect(8, 4, 9, 4, fill=S)                     # block cursor
    p.hline(3, 7, 6, G)                            # dim scrollback
    p.hline(3, 5, 8, G)


def _mux(p):
    _terminal(p)
    p.rect(9, 8, 14, 14, fill=S, outline=K)         # attached terminal
    p.rect(10, 9, 13, 13, fill=K)
    p.hline(11, 12, 10, N)
    p.px(13, 10, N)
    p.rect(6, 10, 8, 11, fill=B, outline=K)         # link between panes


def _memory(p):
    """A compact RAM module / memory chip."""
    p.rect(2, 3, 13, 12, fill=DN, outline=K)
    p.hline(3, 12, 4, N)
    p.rect(4, 6, 11, 10, fill=K)
    p.rect(5, 7, 6, 9, fill=DB)
    p.rect(8, 7, 9, 9, fill=DB)
    p.rect(11, 7, 11, 9, fill=DB)
    for x in (3, 5, 7, 9, 11, 13):
        p.px(x, 2, Y)
        p.px(x, 13, Y)


def _dosbox(p):
    p.rect(1, 1, 14, 10, fill=S, outline=K)        # beige monitor shell
    p.rect(3, 2, 12, 8, fill=K)                    # black CRT
    p.hline(4, 6, 3, N)                            # green "C:\>" prompt
    p.px(7, 3, N)
    p.rect(9, 3, 10, 3, fill=N)                    # block cursor
    p.hline(4, 8, 5, DN)                           # dim scrollback
    p.hline(4, 6, 6, DN)
    p.rect(6, 11, 9, 11, fill=S)                   # neck
    p.rect(3, 12, 12, 14, fill=S, outline=K)       # base
    p.px(11, 13, N)                                # power led


def _settings(p):
    p.rect(1, 2, 14, 13, fill=S, outline=K)
    p.hline(2, 13, 3, W)
    for i, (x, ky) in enumerate(((4, 6), (8, 10), (12, 8))):
        p.vline(x, 4, 12, G)                       # slider tracks
        p.rect(x - 1, ky - 1, x + 1, ky + 1, fill=S, outline=K)  # knobs
        p.px(x - 1, ky - 1, W)


def _notepad(p):
    p.rect(3, 2, 12, 14, fill=W, outline=K)
    for x in (5, 8, 11):                           # spiral binding
        p.px(x, 1, G)
        p.px(x, 2, K)
    for y in (5, 7, 9, 11):
        p.hline(5, 10, y, B)
    p.hline(5, 13, 13, R)                          # margin flourish


def _browser(p):
    p.rect(4, 1, 11, 1, fill=DB)                   # a chunky globe
    p.rect(2, 3, 13, 3, fill=DB)
    p.rect(1, 4, 14, 11, fill=DB)
    p.rect(2, 12, 13, 12, fill=DB)
    p.rect(4, 13, 11, 14, fill=DB)
    p.rect(3, 2, 12, 2, fill=DB)
    p.poly([(4, 3), (7, 3), (6, 6), (3, 6)], fill=DN)      # landmasses
    p.poly([(9, 5), (12, 4), (12, 8), (10, 9)], fill=DN)
    p.poly([(5, 8), (8, 9), (6, 12)], fill=DN)
    p.hline(1, 14, 7, C)                           # equator
    p.vline(7, 1, 14, C)                           # meridian


def _run(p):
    p.rect(1, 2, 14, 13, fill=S, outline=K)
    p.rect(2, 3, 13, 4, fill=DB)
    p.rect(3, 7, 10, 9, fill=W, outline=K)         # the Run field
    p.rect(4, 8, 6, 8, fill=DB)
    p.poly([(11, 10), (14, 12), (11, 14)], fill=DN, outline=K)


def _shutdown(p):
    p.rect(4, 2, 11, 3, fill=S, outline=K)
    p.rect(2, 4, 13, 13, fill=S, outline=K)        # the big red switch
    p.rect(4, 6, 11, 11, fill=R, outline=K)
    p.vline(7, 7, 9, W)
    p.px(8, 7, W)


def _flame(p):
    """kilix's flame — used for the Start button and About boxes."""
    p.poly([(7, 0), (11, 4), (13, 8), (12, 12), (9, 14), (5, 14), (2, 12),
            (2, 8), (4, 4)], fill=R, outline=DR)
    p.poly([(7, 4), (10, 8), (10, 11), (8, 13), (6, 13), (4, 11), (5, 7)],
           fill=Y)
    p.poly([(7, 8), (9, 11), (7, 13), (5, 11)], fill=W)


def _home(p):
    p.poly([(7, 1), (14, 8), (1, 8)], fill=DR, outline=K)  # roof
    p.rect(3, 8, 12, 14, fill=S, outline=K)
    p.rect(6, 10, 9, 14, fill=DY, outline=K)       # door
    p.rect(10, 10, 11, 11, fill=C, outline=K)      # window
    p.rect(11, 2, 12, 5, fill=DR, outline=K)       # chimney


def _question(p):
    p.rect(3, 1, 12, 2, fill=DB)
    p.rect(2, 3, 6, 5, fill=DB)
    p.rect(9, 3, 13, 6, fill=DB)
    p.rect(7, 7, 11, 8, fill=DB)
    p.rect(6, 9, 9, 10, fill=DB)
    p.rect(6, 13, 9, 14, fill=DB)


def _info(p):
    p.rect(4, 1, 11, 1, fill=W, outline=None)
    p.rect(2, 2, 13, 12, fill=W)
    p.rect(1, 4, 14, 10, fill=W)
    p.rect(4, 13, 11, 13, fill=W)
    p.rect(5, 14, 7, 14, fill=W)                   # speech tail
    p.rect(7, 3, 8, 4, fill=B)                     # the i
    p.rect(6, 6, 8, 6, fill=B)
    p.rect(7, 7, 8, 10, fill=B)
    p.rect(6, 11, 9, 11, fill=B)


def _warn(p):
    p.poly([(7, 0), (8, 0), (15, 14), (0, 14)], fill=Y, outline=K)
    p.rect(7, 4, 8, 9, fill=K)
    p.rect(7, 11, 8, 12, fill=K)


def _error(p):
    p.rect(4, 1, 11, 1, fill=R)
    p.rect(2, 2, 13, 3, fill=R)
    p.rect(1, 4, 14, 11, fill=R)
    p.rect(2, 12, 13, 13, fill=R)
    p.rect(4, 14, 11, 14, fill=R)
    for i in range(5):                             # the X
        p.rect(5 + i, 5 + i, 6 + i, 6 + i, fill=W)
        p.rect(9 - i, 5 + i, 10 - i, 6 + i, fill=W)


def _arrow(p, dx):
    if dx < 0:
        p.poly([(10, 2), (10, 13), (3, 7)], fill=K)
        p.rect(9, 6, 13, 9, fill=K)
    else:
        p.poly([(5, 2), (5, 13), (12, 7)], fill=K)
        p.rect(2, 6, 6, 9, fill=K)


def _arrow_left(p):
    _arrow(p, -1)


def _arrow_right(p):
    _arrow(p, +1)


def _games(p):
    p.rect(1, 5, 14, 10, fill=S, outline=K)        # a gamepad
    p.rect(0, 6, 0, 9, fill=S)
    p.rect(15, 6, 15, 9, fill=S)
    p.rect(3, 7, 6, 8, fill=K)                     # d-pad
    p.rect(4, 6, 5, 9, fill=K)
    p.px(11, 6, R)                                 # buttons
    p.px(12, 7, Y)
    p.px(10, 7, B)
    p.px(11, 8, DN)
    p.hline(2, 13, 5, W)


def _amp(p):
    # a little Winamp-style player: title strip, scope window, lightning
    p.rect(1, 2, 14, 13, fill=G, outline=K)
    p.rect(2, 3, 13, 4, fill=DB)                   # title strip
    p.px(13, 3, S)
    p.rect(2, 5, 9, 10, fill=K)                    # vis window
    for x, h in ((3, 2), (4, 4), (5, 3), (6, 5), (7, 2), (8, 4)):
        p.vline(x, 10 - h, 9, N)                   # spectrum bars
    p.poly([(12, 4), (10, 8), (11, 8), (10, 12), (13, 7), (12, 7), (13, 4)],
           fill=Y)                                 # lightning bolt
    p.rect(2, 11, 13, 12, fill=S)                  # transport strip
    for x in (3, 5, 7, 9, 11):
        p.px(x, 11, K)


def _tank(p):
    # a little artillery tank (Bashed Earth)
    p.rect(9, 4, 14, 5, fill=DN)                   # barrel, aimed up-right
    p.px(13, 3, DN)
    p.px(14, 3, DN)
    p.rect(5, 6, 10, 8, fill=DN, outline=K)        # turret
    p.rect(2, 9, 13, 11, fill=N, outline=K)        # hull
    p.rect(1, 12, 14, 14, fill=G, outline=K)       # tracks
    for x in (3, 6, 9, 12):
        p.px(x, 13, K)                             # road wheels
    p.hline(2, 13, 12, S)


def _lander(p):
    # a lunar lander descending on a lit thruster (Terminal Lander)
    p.rect(6, 2, 9, 3, fill=S, outline=K)          # ascent stage
    p.rect(4, 4, 11, 8, fill=S, outline=K)         # descent module body
    p.hline(5, 10, 4, W)                           # top hilight
    p.rect(6, 5, 9, 7, fill=DB, outline=K)         # window
    p.px(7, 6, C)                                  # glint
    for x, y in ((4, 8), (3, 9), (2, 10), (2, 11)):
        p.px(x, y, G)                              # left leg
    p.hline(1, 3, 12, K)                           # left foot pad
    for x, y in ((11, 8), (12, 9), (13, 10), (13, 11)):
        p.px(x, y, G)                              # right leg
    p.hline(12, 14, 12, K)                         # right foot pad
    p.rect(7, 9, 8, 11, fill=Y)                    # thruster flame
    p.px(7, 12, R)
    p.px(8, 12, R)                                 # flame tip


def _brokeout(p):
    # a Breakout playfield: colored brick rows (separated by gaps), a ball,
    # and a paddle. No black outlines — thin bricks would swallow them.
    for x0, x1, col in ((1, 4, R), (6, 9, Y), (11, 14, N)):     # brick row 1
        p.rect(x0, 2, x1, 4, fill=col)
    for x0, x1, col in ((1, 4, C), (6, 9, DB), (11, 14, DR)):   # brick row 2
        p.rect(x0, 5, x1, 7, fill=col)
    p.rect(7, 10, 8, 11, fill=W)                                # the ball
    p.rect(4, 13, 11, 14, fill=S, outline=G)                    # the paddle


def _lights(p):
    # A miniature Lights Out switchboard with the center cross illuminated.
    p.rect(1, 1, 14, 14, fill=G, outline=K)
    p.hline(2, 13, 2, W)
    for row, y in enumerate((4, 8, 12)):
        for col, x in enumerate((4, 8, 12)):
            on = row == 1 or col == 1
            p.rect(x - 1, y - 1, x + 1, y + 1,
                   fill=Y if on else DB, outline=K)
            p.px(x, y, W if on else B)


def _super_kilix(p):
    # Kilix the star-vault cat on a platform ledge: pointed ears, a cyan visor,
    # a teal knit sweater, standing on a brick block -- the side-scroller.
    p.rect(0, 13, 15, 15, fill=DY, outline=K)                   # brick platform
    p.vline(4, 13, 15, K); p.vline(9, 13, 15, K); p.vline(13, 13, 15, K)
    p.poly([(4, 4), (4, 1), (6, 3), (9, 3), (11, 1), (11, 7), (9, 8), (6, 8), (4, 6)],
           fill=R, outline=K)                                   # head + ears
    p.px(4, 2, Y); p.px(11, 2, Y)                               # ear tips
    p.rect(5, 4, 10, 6, fill=DB, outline=K)                     # visor housing
    p.rect(6, 5, 9, 5, fill=C); p.px(8, 5, W)                   # cyan visor glint
    p.rect(5, 8, 10, 12, fill=T, outline=K)                     # teal knit sweater
    p.hline(6, 9, 10, C); p.px(6, 9, W); p.px(9, 9, W)          # knit stripe
    p.px(6, 12, C); p.px(9, 12, C)                              # cyan boots


def _doom(p):
    # a cacodemon-ish grinning red sphere (all in-house pixel art)
    p.rect(4, 1, 11, 1, fill=DR)
    p.rect(2, 2, 13, 3, fill=R)
    p.rect(1, 4, 14, 11, fill=R)
    p.rect(2, 12, 13, 13, fill=R)
    p.rect(4, 14, 11, 14, fill=DR)
    p.rect(5, 3, 10, 3, fill=DR)                   # brow
    p.rect(5, 4, 10, 6, fill=W)                    # eye
    p.rect(7, 4, 8, 6, fill=DN)                    # green iris
    p.rect(3, 9, 12, 11, fill=DR)                  # mouth
    for x in (4, 6, 8, 10, 12):
        p.px(x, 9, W)                              # fangs
    for x in (5, 7, 9, 11):
        p.px(x, 11, W)


def _arrow_up(p):
    p.poly([(2, 9), (13, 9), (7, 2)], fill=Y, outline=K)   # explorer "up" folder
    p.rect(5, 9, 9, 13, fill=Y)
    p.vline(5, 9, 13, K)
    p.vline(9, 9, 13, K)
    p.hline(5, 9, 13, K)


def _calc(p):
    p.rect(2, 1, 13, 14, fill=S, outline=K)        # body
    p.rect(4, 3, 11, 5, fill=N, outline=K)         # LCD display
    p.hline(5, 8, 4, W)                            # display glint
    for y in (8, 10, 12):                          # button rows
        for x in (4, 6, 8, 10):
            p.px(x, y, K)
    p.px(10, 8, R)                                 # plus/equal accent
    p.px(10, 12, R)


def _cards(p):
    p.rect(1, 4, 8, 14, fill=W, outline=K)         # back card, green
    p.rect(2, 5, 7, 13, fill=DN)
    p.px(4, 7, W); p.px(6, 9, W); p.px(4, 11, W)   # back hatch
    p.rect(7, 2, 14, 12, fill=W, outline=K)        # front card, red
    p.rect(8, 3, 13, 11, fill=DR)
    p.px(10, 5, W); p.px(12, 7, W); p.px(10, 9, W)


def _charmap(p):
    p.rect(1, 1, 14, 14, fill=W, outline=K)         # glyph panel
    for gx in (5, 10):                              # grid rules
        p.vline(gx, 2, 13, S)
    for gy in (5, 10):
        p.hline(2, 13, gy, S)
    p.rect(6, 6, 9, 9, fill=DB)                     # highlighted cell
    p.hline(7, 8, 6, W)                             # the "A" glyph
    p.px(6, 7, W); p.px(9, 7, W)
    p.hline(6, 9, 8, W)
    p.px(6, 9, W); p.px(9, 9, W)


def _help(p):
    p.rect(2, 2, 13, 14, fill=Y, outline=K)        # yellow help book
    p.vline(4, 3, 13, DY)                          # spine groove
    p.rect(5, 2, 13, 3, fill=DY)                   # top shading
    p.px(12, 3, W)                                 # page glint
    p.hline(7, 9, 5, K)                            # a chunky "?"
    p.px(10, 6, K)
    p.px(10, 7, K)
    p.px(9, 8, K)
    p.px(8, 9, K)
    p.px(8, 10, K)
    p.px(8, 12, K)                                 # the dot


def _mines(p):
    p.rect(1, 1, 14, 14, fill=S, outline=G)        # raised minefield cell
    p.hline(1, 14, 1, W)                            # top hilight
    p.vline(1, 1, 14, W)
    p.hline(1, 14, 14, K)                           # bottom/right shadow
    p.vline(14, 1, 14, K)
    p.hline(4, 11, 7, K)                            # mine spikes
    p.vline(7, 4, 11, K)
    p.poly([(4, 4), (11, 11)], outline=K)
    p.poly([(11, 4), (4, 11)], outline=K)
    p.rect(5, 5, 9, 9, fill=K)                      # the black ball
    p.px(6, 6, W)                                   # glint


def _paint(p):
    p.poly([(2, 6), (7, 3), (12, 4), (14, 8), (12, 12), (6, 14), (2, 11)],
           fill=S, outline=K)                          # painter's palette
    p.rect(4, 9, 6, 11, fill=W, outline=K)             # thumb hole
    p.px(6, 6, R); p.px(7, 5, R)                        # dabs of color
    p.px(9, 5, Y); p.px(10, 6, Y)
    p.px(11, 8, B); p.px(12, 9, B)
    p.px(9, 10, N); p.px(10, 11, N)
    p.vline(13, 1, 4, DR)                               # brush handle
    p.px(13, 5, Y); p.px(13, 6, Y)                      # ferrule
    p.rect(12, 7, 14, 9, fill=K)                        # bristles


def _wordpad(p):
    p.rect(3, 1, 12, 14, fill=W, outline=K)                # page
    p.poly([(9, 1), (12, 4), (9, 4)], fill=S, outline=K)   # dog-ear
    p.vline(4, 6, 11, DB)                                   # blue "W"
    p.vline(10, 6, 11, DB)
    p.px(5, 11, DB); p.px(6, 10, DB); p.px(6, 11, DB)
    p.px(7, 8, DB); p.px(7, 9, DB)
    p.px(8, 10, DB); p.px(8, 11, DB); p.px(9, 11, DB)
    p.poly([(11, 1), (14, 4), (13, 5), (10, 2)], fill=B, outline=DB)  # pen
    p.px(10, 2, K)                                          # nib


def _recyclebin_empty(p):
    p.rect(4, 3, 11, 4, fill=S, outline=K)          # lid
    p.hline(6, 9, 2, S)                             # lid handle
    p.px(6, 2, K); p.px(9, 2, K)
    p.poly([(4, 5), (11, 5), (10, 14), (5, 14)], fill=S, outline=K)  # bin body
    p.vline(7, 6, 13, G)                            # ridges
    p.vline(6, 6, 13, G)
    p.vline(9, 6, 13, G)
    p.hline(5, 10, 6, W)                            # rim hilight


def _recyclebin_full(p):
    p.poly([(6, 0), (10, 1), (11, 3), (5, 3)], fill=W, outline=K)  # crumpled paper
    p.px(7, 1, S); p.px(9, 2, S)
    p.rect(4, 3, 11, 4, fill=S, outline=K)          # lid
    p.hline(6, 9, 2, S)                             # lid handle
    p.px(6, 2, K); p.px(9, 2, K)
    p.poly([(4, 5), (11, 5), (10, 14), (5, 14)], fill=S, outline=K)  # bin body
    p.vline(7, 6, 13, G)                            # ridges
    p.vline(6, 6, 13, G)
    p.vline(9, 6, 13, G)
    p.hline(5, 10, 6, W)                            # rim hilight


def _find(p):
    p.rect(1, 3, 4, 4, fill=Y, outline=K)      # folder tab
    p.rect(1, 4, 8, 10, fill=Y, outline=K)     # folder body
    p.hline(2, 7, 5, W)                         # top hilight
    p.rect(9, 7, 13, 11, fill=C, outline=K)    # magnifier lens
    p.hline(10, 12, 8, W)                       # glass glint
    p.rect(13, 11, 15, 13, fill=K)             # handle
    p.px(14, 12, K)


def _soundcp(p):
    p.rect(3, 1, 12, 14, fill=S, outline=K)        # speaker cabinet
    p.vline(4, 2, 13, W)                           # left bevel highlight
    p.rect(5, 3, 10, 9, fill=G, outline=K)         # woofer surround
    p.rect(6, 4, 9, 8, fill=DB, outline=K)         # woofer cone
    p.px(7, 5, C)                                   # cone glint
    p.rect(6, 11, 9, 13, fill=G, outline=K)         # tweeter
    p.px(7, 12, DB)


def _taskmgr(p):
    p.rect(5, 1, 14, 10, fill=S, outline=K)        # back window
    p.rect(5, 1, 14, 2, fill=DB)                   # its (inactive) title bar
    p.rect(1, 5, 10, 14, fill=W, outline=K)        # front window, on top
    p.rect(1, 5, 10, 6, fill=DB)                   # active title bar
    for y in (9, 11, 13):                          # list rows
        p.hline(3, 8, y, G)


def _app(p):
    p.rect(2, 2, 13, 13, fill=S, outline=K)        # window shell
    p.rect(3, 3, 12, 5, fill=DB)                    # navy title bar
    p.px(11, 4, W)                                  # title button glint
    p.rect(4, 7, 11, 11, fill=W, outline=G)         # client area
    p.hline(5, 9, 8, B)                             # a couple lines of content
    p.hline(5, 10, 10, B)


ICONS = {
    "computer": _computer, "display": _display, "drive": _drive,
    "folder": _folder, "folder_open": _folder_open,
    "doc": _doc, "doc_text": _doc_text, "doc_image": _doc_image,
    "doc_audio": _doc_audio,
    "exe": _exe, "terminal": _terminal, "mux": _mux, "memory": _memory,
    "settings": _settings,
    "notepad": _notepad, "browser": _browser, "run": _run,
    "shutdown": _shutdown, "flame": _flame, "home": _home,
    "games": _games, "doom": _doom, "dosbox": _dosbox, "tank": _tank,
    "lander": _lander, "brokeout": _brokeout, "lights": _lights,
    "super-kilix": _super_kilix,
    "amp": _amp,
    "question": _question, "info": _info, "warn": _warn, "error": _error,
    "back": _arrow_left, "forward": _arrow_right, "up": _arrow_up,
    "calc": _calc, "cards": _cards, "charmap": _charmap, "help": _help,
    "mines": _mines, "paint": _paint, "wordpad": _wordpad,
    "recyclebin_empty": _recyclebin_empty, "recyclebin_full": _recyclebin_full,
    "find": _find, "taskmgr": _taskmgr, "soundcp": _soundcp,
    "app": _app,
}

_cache = {}


def get(name, size=32, shortcut=False):
    """Cached RGBA icon. Unknown names fall back to the blank document."""
    key = (name, size, shortcut)
    if key not in _cache:
        u = max(1, size // 16)
        p = P(u)
        ICONS.get(name, _doc)(p)
        img = p.img
        if img.size != (size, size):
            img = img.resize((size, size), Image.NEAREST)
        if shortcut:                              # little corner link-arrow
            o = P(u)
            o.rect(0, 9, 6, 15, fill=W, outline=K)
            o.poly([(2, 13), (5, 13), (5, 10)], fill=K)
            o.px(4, 12, K)
            img = img.copy()
            img.alpha_composite(o.img.resize((size, size), Image.NEAREST)
                                if o.img.size != (size, size) else o.img)
        _cache[key] = img
    return _cache[key]


def paint(dst, name, x, y, size=32, shortcut=False):
    """Paste icon `name` onto RGB image `dst` at (x, y)."""
    ic = get(name, size, shortcut)
    dst.paste(ic, (int(x), int(y)), ic)


def for_path(path, is_dir=False):
    """Pick an icon name for a filesystem path."""
    if is_dir:
        return "folder"
    low = path.lower()
    if low.endswith((".krt", ".rtf")):
        return "wordpad"
    if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
                     ".ico", ".ppm", ".tiff")):
        return "doc_image"
    if low.endswith((".txt", ".md", ".rst", ".log", ".conf", ".cfg", ".ini",
                     ".json", ".yaml", ".yml", ".toml", ".py", ".sh", ".c",
                     ".h", ".go", ".rs", ".js", ".ts", ".html", ".css")):
        return "doc_text"
    if low.endswith((".mp3", ".flac", ".ogg", ".oga", ".opus", ".wav",
                     ".aiff", ".aif", ".aifc", ".m4a", ".aac", ".wma",
                     ".m3u", ".m3u8", ".pls")):
        return "doc_audio"
    if low.endswith(".desktop"):
        return "exe"
    return "doc"
