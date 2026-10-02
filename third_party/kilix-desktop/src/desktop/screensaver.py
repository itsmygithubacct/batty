"""kilix desktop — idle screensavers.

A few classic full-framebuffer savers. Each is a small object with a
``step(dt) -> PIL RGB frame`` at the size it was built for; the Desk blits
the frame straight to the terminal while idle, and any input wakes it.
No animation loop of their own — the main loop drives the cadence.
"""
import random

from PIL import Image

import theme as T
import widgets as W


class _Saver:
    name = "saver"

    def __init__(self, size):
        self.w, self.h = size
        self.buf = Image.new("RGB", size, (0, 0, 0))

    def step(self, dt):               # pragma: no cover - overridden
        return self.buf


def _fade(shift):
    """LUT that decays a channel toward black (for motion trails)."""
    return lambda p: (p * shift) >> 8


class Mystify(_Saver):
    """Bouncing polylines leaving a fading trail (the Win95 'Mystify')."""
    name = "mystify"
    COLORS = [(255, 0, 255), (0, 255, 255), (255, 255, 0), (64, 160, 255)]

    def __init__(self, size, lines=2, verts=4):
        super().__init__(size)
        self.shapes = []
        for i in range(lines):
            pts = [[random.uniform(0, self.w), random.uniform(0, self.h),
                    random.uniform(-70, 70), random.uniform(-70, 70)]
                   for _ in range(verts)]
            self.shapes.append((pts, self.COLORS[i % len(self.COLORS)]))
        self._lut = _fade(205)        # ~0.80 per step

    def step(self, dt):
        self.buf = self.buf.point(self._lut)
        d = W.drawer(self.buf)
        for pts, color in self.shapes:
            for v in pts:
                v[0] += v[2] * dt
                v[1] += v[3] * dt
                if v[0] < 0 or v[0] > self.w:
                    v[2] = -v[2]
                    v[0] = min(max(v[0], 0), self.w)
                if v[1] < 0 or v[1] > self.h:
                    v[3] = -v[3]
                    v[1] = min(max(v[1], 0), self.h)
            poly = [(v[0], v[1]) for v in pts]
            d.line(poly + poly[:1], fill=color)
        return self.buf


class Starfield(_Saver):
    """A warp-speed starfield, stars streaming out from the centre."""
    name = "starfield"

    def __init__(self, size, n=200):
        super().__init__(size)
        self.cx, self.cy = self.w / 2, self.h / 2
        self.stars = [self._spawn() for _ in range(n)]

    def _spawn(self):
        return [random.uniform(-self.w, self.w),
                random.uniform(-self.h, self.h),
                random.uniform(0.1, 1.0)]

    def step(self, dt):
        buf = Image.new("RGB", (self.w, self.h), (0, 0, 0))
        d = W.drawer(buf)
        for s in self.stars:
            s[2] -= dt * 0.45
            if s[2] <= 0.03:
                s[:] = self._spawn()
                s[2] = random.uniform(0.6, 1.0)
            sx = self.cx + s[0] / s[2]
            sy = self.cy + s[1] / s[2]
            if 0 <= sx < self.w and 0 <= sy < self.h:
                b = min(255, int(50 + (1 - s[2]) * 240))
                r = 0 if s[2] > 0.5 else 1
                d.ellipse([sx - r, sy - r, sx + r, sy + r], fill=(b, b, b))
        self.buf = buf
        return buf


class Matrix(_Saver):
    """Falling green code rain."""
    name = "matrix"
    GLYPHS = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ<>*+-=/\\|"

    def __init__(self, size):
        super().__init__(size)
        self.cw, self.lh = 9, 14
        self.cols = max(1, self.w // self.cw)
        self.drops = [random.randint(-self.h // self.lh, 0)
                      for _ in range(self.cols)]
        self.speed = [random.uniform(6, 18) for _ in range(self.cols)]
        self._acc = [0.0] * self.cols
        self._lut = _fade(150)        # ~0.59: long fading tails

    def step(self, dt):
        self.buf = self.buf.point(self._lut)
        d = W.drawer(self.buf)
        for c in range(self.cols):
            self._acc[c] += self.speed[c] * dt
            if self._acc[c] < 1.0:
                continue
            self._acc[c] -= int(self._acc[c])
            self.drops[c] += 1
            if self.drops[c] * self.lh > self.h and random.random() < 0.08:
                self.drops[c] = 0
            x = c * self.cw
            y = self.drops[c] * self.lh
            d.text((x, y), random.choice(self.GLYPHS), font=T.FONT,
                   fill=(190, 255, 190))
            d.text((x, y - self.lh), random.choice(self.GLYPHS), font=T.FONT,
                   fill=(0, 150, 0))
        return self.buf


SAVERS = [Mystify, Starfield, Matrix]


def pick(size):
    """A random saver, ready to step()."""
    return random.choice(SAVERS)(size)
