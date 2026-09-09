#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise terminal color, glyphs and redraws; optionally display an image as cells."""
import argparse
import colorsys
import math
import os
from pathlib import Path
import select
import signal
import sys
import termios
import time
import tty

ESC = '\x1b['
RESET = ESC + '0m'
BACKGROUND = (19, 23, 30)


def color(value):
    return tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(value % 1, 0.82, 0.95))


def half_blocks(pixels, width, height):
    """One cell displays an upper foreground pixel and a lower background pixel."""
    lines = []
    for y in range(0, height, 2):
        row = []
        previous = None
        for x in range(width):
            top, bottom = pixels[y * width + x], pixels[(y + 1) * width + x]
            pair = (top, bottom)
            if pair != previous:
                row.append(f'{ESC}38;2;{top[0]};{top[1]};{top[2]}m'
                           f'{ESC}48;2;{bottom[0]};{bottom[1]};{bottom[2]}m')
                previous = pair
            row.append('▀')
        lines.append(''.join(row) + RESET)
    return lines


def pattern(width, height, phase, mode):
    pixels = []
    palette = [color(i / 256) for i in range(256)]
    for y in range(height):
        v = y / max(1, height - 1)
        for x in range(width):
            u = x / max(1, width - 1)
            if mode == 'gradient':
                pixels.append((round(255 * u), round(255 * v), round(255 * (1 - u))))
            elif mode == 'checker':
                pixels.append((235, 235, 235) if (x // 4 + y // 4) % 2 else (30, 35, 45))
            else:
                wave = (math.sin(u * 8 + phase) + math.sin(v * 9 - phase * 0.8)
                        + math.sin((u + v) * 6 + phase * 0.5)) / 6
                pixels.append(palette[round((wave + phase * 0.03) * 256) % 256])
    return pixels


def load_image(path):
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise ValueError('Image viewing requires Pillow (the animated demo does not).') from exc
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert('RGBA')
    background = Image.new('RGBA', image.size, BACKGROUND + (255,))
    background.alpha_composite(image)
    return background.convert('RGB')


def image_pixels(image, width, height):
    from PIL import Image
    fitted = image.copy()
    fitted.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new('RGB', (width, height), BACKGROUND)
    canvas.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
    return list(canvas.getdata())


def frame(width, rows, phase, mode, image, rate, paused):
    # Leave the last column unused to avoid triggering automatic line wrapping.
    width = max(1, min(width - 1, 160))
    height = max(2, min(rows - 7, 55) * 2)
    pixels = image_pixels(image, width, height) if mode == 'image' else pattern(width, height, phase, mode)
    title = 'BATTY / VISUAL TEST'
    sample = (ESC + '1mBold' + RESET + '  ' + ESC + '3mItalic' + RESET + '  '
              + ESC + '4mUnderline' + RESET)
    if width >= 60:
        sample += '  café  e\u0301  Ελληνικά  日本語'
    lines = [ESC + '1;38;2;125;211;252m' + title[:width] + RESET,
             ('24-bit color / half-block pixels / native terminal rendering')[:width],
             sample, '']
    lines += half_blocks(pixels, width, height)
    state = 'paused' if paused else f'{rate:.1f} updates/s (producer)'
    lines += [f'{mode} | {width} x {height} cell pixels | {state}'[:width],
              '1 plasma  2 gradient  3 checker  4 image | Space pause | Q quit'[:width]]
    return (RESET + ESC + 'K\r\n').join(lines) + RESET + ESC + 'K'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, help='PNG/JPEG/etc.; requires Pillow; rendered with ANSI half blocks')
    parser.add_argument('--mode', choices=['plasma', 'gradient', 'checker'], default='plasma')
    parser.add_argument('--fps', type=float, default=24, help='requested producer updates per second, 1–60')
    parser.add_argument('--seconds', type=float, default=0, help='exit after this duration; default waits for Q')
    parser.add_argument('--once', action='store_true', help='print one static frame without using the alternate screen')
    args = parser.parse_args()
    if not math.isfinite(args.fps) or not 1 <= args.fps <= 60:
        parser.error('--fps must be between 1 and 60')
    if not math.isfinite(args.seconds) or args.seconds < 0:
        parser.error('--seconds must be nonnegative')
    try:
        image = load_image(args.image) if args.image else None
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    mode = 'image' if image is not None else args.mode
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive and not args.once:
        parser.error('Run inside a terminal, or use --once to write a static ANSI frame.')
    def geometry():
        try:
            return os.get_terminal_size(sys.stdout.fileno())
        except OSError:
            return os.terminal_size((100, 34))
    if args.once:
        print(frame(*geometry(), 0, mode, image, 0, False))
        return 0
    saved = termios.tcgetattr(sys.stdin.fileno())
    def stopped(signum, unused_frame):
        raise SystemExit(128 + signum)
    old_handlers = {sig: signal.signal(sig, stopped) for sig in (signal.SIGTERM, signal.SIGHUP)}
    started = last_frame = last_rate = time.monotonic()
    phase = rate = 0.0
    frames = 0
    paused = False
    try:
        tty.setcbreak(sys.stdin.fileno())
        sys.stdout.write(ESC + '?1049h' + ESC + '?25l' + ESC + '2J')
        previous_geometry = None
        while not args.seconds or time.monotonic() - started < args.seconds:
            now = time.monotonic()
            if not paused:
                phase += now - last_frame
            last_frame = now
            size = geometry()
            if size.columns < 32 or size.lines < 12:
                content = 'Resize to at least 32 columns and 12 rows. Q quits.'
            else:
                content = frame(*size, phase, mode, image, rate, paused)
            if size != previous_geometry:
                sys.stdout.write(ESC + '2J')
                previous_geometry = size
            sys.stdout.write(ESC + 'H' + content)
            sys.stdout.flush()
            frames += 1
            if now - last_rate >= 1:
                rate = frames / (now - last_rate)
                frames = 0
                last_rate = now
            delay = max(0, 1 / args.fps - (time.monotonic() - now))
            if select.select([sys.stdin], [], [], delay)[0]:
                keys = os.read(sys.stdin.fileno(), 64)
                if not keys or b'q' in keys.lower():
                    break
                for key in keys:
                    if key == ord(' '): paused = not paused
                    elif key == ord('1'): mode = 'plasma'
                    elif key == ord('2'): mode = 'gradient'
                    elif key == ord('3'): mode = 'checker'
                    elif key == ord('4') and image is not None: mode = 'image'
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, saved)
        sys.stdout.write(RESET + ESC + '?25h' + ESC + '?1049l')
        sys.stdout.flush()
        for sig, handler in old_handlers.items(): signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
