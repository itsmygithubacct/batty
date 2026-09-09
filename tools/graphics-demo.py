#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Display real Kitty and Sixel images, with protocol replies and pixel-level detail."""
import argparse
import base64
import fcntl
import math
import os
from pathlib import Path
import re
import select
import signal
import struct
import sys
import termios
import time
import tty

CSI = b'\x1b['
ST = b'\x1b\\'
BACKGROUND = (19, 23, 30)
CSI_REPLY = re.compile(rb'\x1b\[([0-?]*)([ -/]*)([@-~])')


def kitty_chunks(pixels, width, height, image_id=42):
    """Yield a direct RGBA upload and placement; only the first chunk has metadata."""
    if width < 1 or height < 1 or len(pixels) != width * height * 4:
        raise ValueError('RGBA byte count does not match the image dimensions')
    encoded = base64.b64encode(pixels)
    header = f'a=T,t=d,f=32,s={width},v={height},i={image_id},p=1,C=1,z=1,'
    for offset in range(0, len(encoded), 4096):
        chunk = encoded[offset:offset + 4096]
        more = int(offset + len(chunk) < len(encoded))
        controls = (header if offset == 0 else '') + f'm={more}'
        yield b'\x1b_G' + controls.encode('ascii') + b';' + chunk + ST


def sixel_run(values):
    """Encode a color plane with DEC repeat counts and omit trailing blank pixels."""
    end = len(values)
    while end and not values[end - 1]:
        end -= 1
    output = bytearray()
    offset = 0
    while offset < end:
        value = values[offset]
        following = offset + 1
        while following < end and values[following] == value:
            following += 1
        count = following - offset
        if count > 3:
            output.extend(f'!{count}'.encode('ascii'))
            output.append(value + 63)
        else:
            output.extend(bytes([value + 63]) * count)
        offset = following
    return output


def sixel_bytes(pixels, width, height):
    """Encode a 256-color Sixel raster; zero alpha remains transparent.

    Sixel has no fractional alpha. Partial alpha is composited on the demo's
    dark background, while Kitty receives the original unpremultiplied RGBA.
    """
    if width < 1 or height < 1 or len(pixels) != width * height * 4:
        raise ValueError('RGBA byte count does not match the image dimensions')
    indices = []
    used = set()
    for offset in range(0, len(pixels), 4):
        r, g, b, alpha = pixels[offset:offset + 4]
        if not alpha:
            indices.append(-1)
            continue
        if alpha != 255:
            r = (r * alpha + BACKGROUND[0] * (255 - alpha) + 127) // 255
            g = (g * alpha + BACKGROUND[1] * (255 - alpha) + 127) // 255
            b = (b * alpha + BACKGROUND[2] * (255 - alpha) + 127) // 255
        index = ((r * 7 + 127) // 255 << 5) | ((g * 7 + 127) // 255 << 2) | ((b * 3 + 127) // 255)
        indices.append(index)
        used.add(index)
    # P2=1 preserves unpainted pixels. Raster Pan=Pad=1 requests square pixels.
    output = bytearray(f'\x1bP0;1;0q"1;1;{width};{height}'.encode('ascii'))
    for index in sorted(used):
        r, g, b = (index >> 5) & 7, (index >> 2) & 7, index & 3
        output.extend(f'#{index};2;{round(r * 100 / 7)};{round(g * 100 / 7)};{round(b * 100 / 3)}'.encode('ascii'))
    for y in range(0, height, 6):
        planes = {}
        for bit in range(min(6, height - y)):
            start = (y + bit) * width
            for x, index in enumerate(indices[start:start + width]):
                if index < 0:
                    continue
                if index not in planes:
                    planes[index] = bytearray(width)
                planes[index][x] |= 1 << bit
        for plane, index in enumerate(sorted(planes)):
            if plane:
                output.append(ord('$'))
            output.extend(f'#{index}'.encode('ascii'))
            output.extend(sixel_run(planes[index]))
        if y + 6 < height:
            output.append(ord('-'))
    output.extend(ST)
    return bytes(output)


def pattern_rgba(width, height):
    """Generate gradients, single-pixel lines, rings, clear holes and an alpha ramp."""
    pixels = bytearray(width * height * 4)
    for y in range(height):
        v = y / max(1, height - 1)
        for x in range(width):
            u = x / max(1, width - 1)
            r, g, b, alpha = round(255 * u), round(255 * v), round(255 * (1 - u)), 255
            if 0.06 < u < 0.36 and 0.08 < v < 0.45:
                # These are actual single-pixel checker squares, not text cells.
                r = g = b = 245 if (x + y) % 2 else 16
            distance = math.hypot((u - 0.72) * width, (v - 0.30) * height)
            if distance < min(width, height) * 0.23:
                ring = round((math.sin(distance * 0.7) + 1) * 127.5)
                r, g, b = ring, 255 - ring, 240
            if x % 40 == 0 or y % 40 == 0:
                r = g = b = 245
            if v > 0.66:
                r, g, b = 50, 210, 255
                alpha = round(255 * u)
                if (x // 20 + y // 20) % 2 == 0 and u < 0.28:
                    alpha = 0
                if u > 0.78:
                    radius = math.hypot((u - 0.89) / 0.10, (v - 0.83) / 0.15)
                    alpha = round(255 * max(0, min(1, (1 - radius) * 6)))
            offset = (y * width + x) * 4
            pixels[offset:offset + 4] = bytes((r, g, b, alpha))
    return bytes(pixels)


class Replies:
    """Separate terminal replies from keyboard input, including split escape sequences."""
    def __init__(self):
        self.pending = bytearray()
        self.keys = bytearray()
        self.kitty = {}
        self.attributes = None
        self.cell_pixels = None
        self.sixel_mode = None
        self.sixel_used = False

    def feed(self, data):
        self.pending.extend(data)
        while self.pending:
            if self.pending[0] != 27:
                self.keys.append(self.pending.pop(0))
                continue
            if len(self.pending) < 2:
                break
            if self.pending[1] == ord('_'):
                end = self.pending.find(ST, 2)
                if end < 0:
                    break
                packet = bytes(self.pending[2:end])
                del self.pending[:end + 2]
                if not packet.startswith(b'G') or b';' not in packet:
                    continue
                controls, message = packet[1:].split(b';', 1)
                fields = dict(field.split(b'=', 1) for field in controls.split(b',') if b'=' in field)
                image_id = fields.get(b'i', b'')
                if image_id.isdigit():
                    self.kitty[int(image_id)] = message.decode('ascii', errors='replace')[:120]
                continue
            if self.pending[1] == ord('['):
                match = CSI_REPLY.match(self.pending)
                if match is None:
                    break
                parameters, intermediates, final = match.groups()
                del self.pending[:match.end()]
                if intermediates == b'$' and final == b'y':
                    numbers = parameters.split(b';')
                    if len(numbers) == 2 and numbers[0] == b'?80' and numbers[1] in (b'0', b'1', b'2', b'3', b'4'):
                        self.sixel_mode = int(numbers[1])
                    continue
                if intermediates:
                    continue
                if final == b'c' and parameters.startswith(b'?'):
                    self.attributes = parameters[1:].decode('ascii').split(';')
                elif final == b't':
                    numbers = parameters.split(b';')
                    if len(numbers) == 3 and numbers[0] == b'6' and all(number.isdigit() for number in numbers):
                        height, width = int(numbers[1]), int(numbers[2])
                        if 1 <= width <= 512 and 1 <= height <= 512:
                            self.cell_pixels = width, height
                continue
            del self.pending[:2]
        if len(self.pending) > 8192:
            self.pending.clear()

    def read(self, timeout):
        if select.select([sys.stdin], [], [], timeout)[0]:
            data = os.read(sys.stdin.fileno(), 8192)
            if not data:
                return False
            self.feed(data)
        return True

    def status(self, image_id, query_id):
        kitty = self.kitty.get(image_id, self.kitty.get(query_id))
        if kitty == 'OK':
            kitty_status = 'OK'
        elif kitty:
            kitty_status = kitty
        elif self.attributes is not None:
            kitty_status = 'no reply'
        else:
            kitty_status = 'unconfirmed'
        sixel_status = 'advertised' if self.attributes and '4' in self.attributes else 'unconfirmed'
        return f'Kitty: {kitty_status} | Sixel: {sixel_status}'


def geometry(replies):
    try:
        rows, cols, pixel_width, pixel_height = struct.unpack('HHHH', fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, bytes(8)))
        if rows and cols:
            if pixel_width >= cols and pixel_height >= rows:
                return cols, rows, pixel_width // cols, pixel_height // rows, False
            if replies.cell_pixels:
                return cols, rows, *replies.cell_pixels, False
            return cols, rows, 8, 16, True
    except OSError:
        pass
    return 100, 34, 8, 16, True


def move(row, col=1):
    return f'\x1b[{row};{col}H'.encode('ascii')


def text_at(row, col, text, width, style=b''):
    # Labels are ASCII and contain no caller-controlled terminal escapes.
    clean = ''.join(char if ' ' <= char <= '~' else '?' for char in text)
    return move(row, col) + style + clean[:max(0, width)].encode('ascii') + CSI + b'0m'


def load_image(path):
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise ValueError('--image requires Pillow; the procedural demo uses only Python itself') from exc
    with Image.open(path) as source:
        return ImageOps.exif_transpose(source).convert('RGBA')


def fit_pixels(width, height, image):
    if image is None:
        return pattern_rgba(width, height)
    from PIL import Image
    return image.resize((width, height), Image.Resampling.LANCZOS).tobytes()


def delete_kitty(image_id):
    return f'\x1b_Ga=d,d=I,i={image_id},q=2;'.encode('ascii') + ST


def restore_sixel_mode(replies):
    if not replies.sixel_used:
        return b''
    # Unsupported or unanswered DECRQM uses the documented reset baseline.
    return CSI + (b'?80h' if replies.sixel_mode in (1, 3) else b'?80l')


def draw(args, replies, image, image_id, query_id):
    cols, rows, cell_width, cell_height, guessed = geometry(replies)
    output = sys.stdout.buffer
    output.write(delete_kitty(image_id) + CSI + b'0m' + CSI + b'2J')
    if cols < 32 or rows < 16:
        output.write(text_at(1, 1, 'Resize to at least 32 columns and 16 rows. Q quits.', cols - 1))
        output.flush()
        return
    output.write(text_at(1, 1, 'BATTY / NATIVE GRAPHICS', cols - 1, CSI + b'1;38;2;125;211;252m'))
    output.write(text_at(2, 1, replies.status(image_id, query_id), cols - 1))
    note = 'Direct image pixels | Kitty: RGBA alpha | Sixel: 256 colors + clear pixels'
    output.write(text_at(3, 1, note, cols - 1))
    protocols = ['kitty', 'sixel'] if args.protocol == 'both' else [args.protocol]
    side_by_side = len(protocols) == 2 and cols >= 72
    if side_by_side:
        panel_cols, image_rows = (cols - 5) // 2, rows - 10
    elif len(protocols) == 2:
        panel_cols, image_rows = cols - 2, (rows - 13) // 2
    else:
        panel_cols, image_rows = cols - 2, rows - 10
    source_width, source_height = image.size if image else (args.width, args.height)
    scale = min(panel_cols * cell_width / source_width, max(1, image_rows) * cell_height / source_height,
                args.width / source_width, args.height / source_height, 1)
    width, height = max(1, int(source_width * scale)), max(1, int(source_height * scale))
    pixels = fit_pixels(width, height, image)
    occupied_rows = math.ceil(height / cell_height)
    for index, protocol in enumerate(protocols):
        col = 1 + index * (panel_cols + 3) if side_by_side else 1
        label_row = 5 if side_by_side or index == 0 else 8 + occupied_rows
        image_row = label_row + 1
        output.write(text_at(label_row, col, f'{protocol.upper()} / {width} x {height} pixels', panel_cols, CSI + b'1m'))
        # Sixel is below text in Batty, so use empty colored cells under its
        # raster. Kitty's positive z placement can reveal text through alpha.
        for row in range(occupied_rows):
            output.write(move(image_row + row, col))
            for cell in range(math.ceil(width / cell_width)):
                background = b'48;2;36;44;60m' if (cell // 4 + row // 2) % 2 else b'48;2;19;23;30m'
                character = b' ' if protocol == 'sixel' else b'pixels . '[cell % 9:cell % 9 + 1]
                output.write(CSI + background + CSI + b'38;2;135;155;180m' + character)
        output.write(CSI + b'0m' + move(image_row, col))
        if protocol == 'kitty':
            for chunk in kitty_chunks(pixels, width, height, image_id):
                output.write(chunk)
        else:
            # Explicit cursor mode keeps each raster in its labeled panel even
            # if a previous program enabled page mode (?80h).
            replies.sixel_used = True
            output.write(CSI + b'?80l' + sixel_bytes(pixels, width, height))
        description = 'RGBA: clear holes + smooth alpha' if protocol == 'kitty' else 'Sixel: clear holes; partial alpha flattened'
        output.write(text_at(image_row + occupied_rows, col, description, panel_cols))
    estimate = ' (estimated)' if guessed else ''
    output.write(text_at(rows - 2, 1, f'Cell {cell_width} x {cell_height} px{estimate} | 1px checker + rings + alpha ramp', cols - 1))
    output.write(text_at(rows - 1, 1, '1 Kitty | 2 Sixel | 3 Both | R redraw | Q quit', cols - 1))
    output.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=
        'Sixel uses cursor placement (DECSDM reset). The prior mode is restored '
        'when the terminal reports it; otherwise DECSDM remains reset.')
    parser.add_argument('--protocol', choices=['kitty', 'sixel', 'both'], default='both')
    parser.add_argument('--image', type=Path, help='display a PNG/JPEG/etc. using Pillow; preserve Kitty alpha')
    parser.add_argument('--width', type=int, default=640, help='maximum image width in pixels (default: 640)')
    parser.add_argument('--height', type=int, default=400, help='maximum image height in pixels (default: 400)')
    parser.add_argument('--seconds', type=float, default=0, help='exit after displaying for this duration; 0 waits for Q')
    parser.add_argument('--once', action='store_true', help='write one static protocol frame, suitable for a fixture file')
    args = parser.parse_args()
    if not 8 <= args.width <= 2048 or not 8 <= args.height <= 2048:
        parser.error('--width and --height must be between 8 and 2048')
    if not math.isfinite(args.seconds) or args.seconds < 0:
        parser.error('--seconds must be nonnegative')
    try:
        image = load_image(args.image) if args.image else None
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if not args.once and not (sys.stdin.isatty() and sys.stdout.isatty()):
        parser.error('Run inside a terminal, or use --once to write a protocol fixture.')
    image_id = 0x42000000 | (os.getpid() & 0xFFFFFF)
    query_id = image_id ^ 0x01000000
    replies = Replies()
    if args.once:
        draw(args, replies, image, image_id, query_id)
        return 0
    saved = termios.tcgetattr(sys.stdin.fileno())
    def stopped(signum, unused_frame):
        raise SystemExit(128 + signum)
    handlers = {sig: signal.signal(sig, stopped) for sig in (signal.SIGTERM, signal.SIGHUP)}
    failed = False
    try:
        tty.setraw(sys.stdin.fileno())
        output = sys.stdout.buffer
        output.write(CSI + b'?1049h' + CSI + b'?25l' + CSI + b'2J')
        output.write(f'\x1b_Gi={query_id},s=1,v=1,a=q,t=d,f=24;AAAA'.encode('ascii') + ST
                     + CSI + b'16t' + CSI + b'?80$p' + CSI + b'c')
        output.flush()
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            if not replies.read(max(0, deadline - time.monotonic())):
                return 0
            if replies.attributes is not None and replies.cell_pixels is not None and replies.sixel_mode is not None:
                break
        draw(args, replies, image, image_id, query_id)
        started = time.monotonic()
        previous_size = geometry(replies)
        previous_status = ''
        while not args.seconds or time.monotonic() - started < args.seconds:
            if not replies.read(0.1):
                break
            keys, replies.keys = replies.keys, bytearray()
            if ord('q') in keys or ord('Q') in keys:
                break
            if 3 in keys:
                raise KeyboardInterrupt
            redraw = False
            for key in keys:
                if key in b'123':
                    args.protocol = {ord('1'): 'kitty', ord('2'): 'sixel', ord('3'): 'both'}[key]
                    redraw = True
                elif key in b'rR':
                    redraw = True
            size = geometry(replies)
            if size != previous_size:
                previous_size = size
                redraw = True
            if redraw:
                draw(args, replies, image, image_id, query_id)
            status = replies.status(image_id, query_id)
            if status != previous_status:
                output.write(move(2) + CSI + b'2K' + text_at(2, 1, status, size[0] - 1))
                output.flush()
                previous_status = status
            response = replies.kitty.get(image_id)
            if response is not None and response != 'OK':
                failed = True
        return int(failed)
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            sys.stdout.buffer.write(delete_kitty(image_id) + restore_sixel_mode(replies)
                                    + CSI + b'0m' + CSI + b'?25h' + CSI + b'?1049l')
            sys.stdout.buffer.flush()
        finally:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, saved)
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
