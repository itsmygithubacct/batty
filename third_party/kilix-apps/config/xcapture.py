"""Event-driven X11 RGB capture using XDamage and MIT-SHM.

The XDamage connection is owned by python-xlib so its socket integrates with a
normal ``select`` loop. Pixel reads use a separate Xlib connection and
``XShmGetImage``. Captures are bucketed rectangles, not full-screen polls; a
small LRU of MIT-SHM images avoids reallocating common strip/cursor sizes.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
from collections import OrderedDict

from PIL import Image
from Xlib import X
from Xlib import display as xdisplay
from Xlib.ext import damage
from Xlib.protocol import rq

try:
    from Xlib.ext import xfixes
except ImportError:  # pragma: no cover - old python-xlib fallback
    xfixes = None


ZPIXMAP = 2
ALL_PLANES = ctypes.c_ulong(-1).value
IPC_PRIVATE = 0
IPC_CREAT = 0o1000
IPC_RMID = 0
MAX_DAMAGE_RECTS = 8


_XFIXES_RECTANGLE = rq.Struct(
    rq.Int16("x"), rq.Int16("y"),
    rq.Card16("width"), rq.Card16("height"))


class _XFixesCreateRegion(rq.Request):
    """XFixes v2 requests missing from older python-xlib releases."""

    _request = rq.Struct(
        rq.Card8("opcode"),
        rq.Opcode(5),
        rq.RequestLength(),
        rq.Card32("region"),
        rq.List("rectangles", _XFIXES_RECTANGLE),
    )


class _XFixesDestroyRegion(rq.Request):
    _request = rq.Struct(
        rq.Card8("opcode"),
        rq.Opcode(10),
        rq.RequestLength(),
        rq.Card32("region"),
    )


class _XFixesFetchRegion(rq.ReplyRequest):
    _request = rq.Struct(
        rq.Card8("opcode"),
        rq.Opcode(19),
        rq.RequestLength(),
        rq.Card32("region"),
    )
    _reply = rq.Struct(
        rq.ReplyCode(),
        rq.Pad(1),
        rq.Card16("sequence_number"),
        rq.ReplyLength(),
        rq.Int16("x"),
        rq.Int16("y"),
        rq.Card16("width"),
        rq.Card16("height"),
        rq.Pad(16),
        rq.List("rectangles", _XFIXES_RECTANGLE),
    )


class _XImageFunctions(ctypes.Structure):
    _fields_ = [(name, ctypes.c_void_p) for name in (
        "create_image", "destroy_image", "get_pixel", "put_pixel",
        "sub_image", "add_pixel")]


class _XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int), ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int), ("format", ctypes.c_int),
        ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int), ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int), ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong), ("obdata", ctypes.c_void_p),
        ("f", _XImageFunctions),
    ]


class _XShmSegmentInfo(ctypes.Structure):
    _fields_ = [
        ("shmseg", ctypes.c_ulong),
        ("shmid", ctypes.c_int),
        ("shmaddr", ctypes.c_void_p),
        ("readOnly", ctypes.c_int),
    ]


class CaptureUnavailable(RuntimeError):
    pass


class _ShmImage:
    def __init__(self, owner, width, height):
        self.owner = owner
        self.width, self.height = width, height
        self.info = _XShmSegmentInfo()
        self.info.shmid = -1
        self.attached = False
        self.removed = False
        self.image = owner.xext.XShmCreateImage(
            owner.display, owner.visual, owner.depth, ZPIXMAP, None,
            ctypes.byref(self.info), width, height)
        if not self.image:
            raise CaptureUnavailable("XShmCreateImage failed")
        self.size = self.image.contents.bytes_per_line * height
        self.info.shmid = owner.libc.shmget(IPC_PRIVATE, self.size,
                                           IPC_CREAT | 0o600)
        if self.info.shmid < 0:
            self._free_image()
            self._raise_errno("shmget")
        address = owner.libc.shmat(self.info.shmid, None, 0)
        if address == ctypes.c_void_p(-1).value:
            owner.libc.shmctl(self.info.shmid, IPC_RMID, None)
            self._free_image()
            self._raise_errno("shmat")
        self.info.shmaddr = address
        self.info.readOnly = 0
        self.image.contents.data = address
        if not owner.xext.XShmAttach(owner.display, ctypes.byref(self.info)):
            self._mark_for_removal()
            self.close()
            raise CaptureUnavailable("XShmAttach failed")
        self.attached = True
        owner.x11.XSync(owner.display, 0)
        # Mark immediately: the segment survives until both X server and this
        # process detach, but cannot leak if the process crashes.
        self._mark_for_removal()

    def _mark_for_removal(self):
        if self.info.shmid >= 0 and not self.removed:
            result = self.owner.libc.shmctl(
                self.info.shmid, IPC_RMID, None)
            if result == 0 or ctypes.get_errno() in (errno.EINVAL, errno.EIDRM):
                self.removed = True

    @staticmethod
    def _raise_errno(operation):
        value = ctypes.get_errno()
        raise OSError(value, f"{operation}: {os.strerror(value)}")

    def _free_image(self):
        if not getattr(self, "image", None):
            return
        # XDestroyImage is a macro. Call its function-table entry after
        # clearing data so it does not free a SysV shared-memory address.
        pointer = self.image.contents.f.destroy_image
        self.image.contents.data = None
        if pointer:
            ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(_XImage))(
                pointer)(self.image)
        else:  # defensive fallback for an unusual Xlib
            self.owner.x11.XFree(self.image)
        self.image = None

    def capture(self, drawable, x, y):
        if not self.owner.xext.XShmGetImage(
                self.owner.display, drawable, self.image, x, y, ALL_PLANES):
            raise CaptureUnavailable("XShmGetImage failed")
        raw = ctypes.string_at(self.info.shmaddr, self.size)
        image = self.image.contents
        if image.bits_per_pixel == 32:
            if (image.red_mask, image.green_mask, image.blue_mask) == \
                    (0xFF0000, 0xFF00, 0xFF):
                mode = "BGRX"
            elif (image.red_mask, image.green_mask, image.blue_mask) == \
                    (0xFF, 0xFF00, 0xFF0000):
                mode = "RGBX"
            else:
                raise CaptureUnavailable("unsupported 32-bit XImage masks")
        elif image.bits_per_pixel == 24:
            mode = "BGR" if image.red_mask == 0xFF0000 else "RGB"
        else:
            raise CaptureUnavailable(
                f"unsupported XImage depth: {image.bits_per_pixel} bpp")
        return Image.frombuffer(
            "RGB", (self.width, self.height), raw, "raw", mode,
            image.bytes_per_line, 1).tobytes()

    def close(self):
        self._mark_for_removal()
        if getattr(self, "attached", False):
            self.owner.xext.XShmDetach(self.owner.display,
                                       ctypes.byref(self.info))
            self.owner.x11.XSync(self.owner.display, 0)
            self.attached = False
        address = getattr(self.info, "shmaddr", None)
        if address and address != ctypes.c_void_p(-1).value:
            self.owner.libc.shmdt(address)
            self.info.shmaddr = None
        self._free_image()


class XDamageCapture:
    """Maintain a full RGB snapshot from event-driven rectangular captures."""

    def __init__(self, display_name: str, width: int, height: int,
                 *, draw_cursor: bool = True, max_buffers: int = 4):
        if width <= 0 or height <= 0:
            raise ValueError("capture dimensions must be positive")
        self.display_name = display_name
        self.width, self.height = width, height
        self.draw_cursor = draw_cursor
        self.max_buffers = max(1, max_buffers)
        self.events = None
        self.damage_id = None
        self.damage_region = None
        self.display = None
        self._buffers = OrderedDict()
        self._closed = False
        try:
            self.events = xdisplay.Display(display_name)
            if not self.events.has_extension("DAMAGE"):
                raise CaptureUnavailable("XDamage is unavailable")
            if xfixes is None or not self.events.has_extension("XFIXES"):
                raise CaptureUnavailable("XFixes regions are unavailable")
            self.events.damage_query_version()
            version = self.events.xfixes_query_version()
            if version.major_version < 2:
                raise CaptureUnavailable("XFixes v2 regions are unavailable")
            protocol = self.events.display
            self.damage_region = protocol.allocate_resource_id()
            _XFixesCreateRegion(
                display=protocol,
                opcode=protocol.get_extension_major("XFIXES"),
                region=self.damage_region,
                rectangles=[],
            )
            self.root = self.events.screen().root
            self.damage_id = self.root.damage_create(
                damage.DamageReportNonEmpty)
            self._cursor_supported = bool(
                draw_cursor and xfixes is not None and
                self.events.has_extension("XFIXES"))
            self._cursor_signature = None
            self._cursor_rect = None
            self.events.sync()

            self.x11 = self._library("X11")
            self.xext = self._library("Xext")
            self.libc = self._library("c")
            self._declare_functions()
            self.display = self.x11.XOpenDisplay(display_name.encode())
            if not self.display:
                raise CaptureUnavailable(f"cannot open display {display_name}")
            if not self.xext.XShmQueryExtension(self.display):
                raise CaptureUnavailable("MIT-SHM is unavailable")
            screen = self.x11.XDefaultScreen(self.display)
            self.visual = self.x11.XDefaultVisual(self.display, screen)
            self.depth = self.x11.XDefaultDepth(self.display, screen)
            self.drawable = self.x11.XRootWindow(self.display, screen)
            self.frame = bytearray(width * height * 3)
            self.capture_rect((0, 0, width, height))
            # Atomically move damage accumulated before/during the initial
            # snapshot into an XFixes region, then recapture it. Clearing a
            # Damage object from only its earlier event rectangles can erase
            # a repaint that arrived between event delivery and DamageSubtract.
            initial_damage = self._take_damage()
            self._capture_rects(initial_damage)
        except Exception:
            self.close()
            raise

    @staticmethod
    def _library(name):
        path = ctypes.util.find_library(name)
        if not path:
            raise CaptureUnavailable(f"lib{name} is unavailable")
        return ctypes.CDLL(path, use_errno=True)

    def _declare_functions(self):
        image_pointer = ctypes.POINTER(_XImage)
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
        self.x11.XDefaultScreen.restype = ctypes.c_int
        self.x11.XDefaultVisual.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x11.XDefaultVisual.restype = ctypes.c_void_p
        self.x11.XDefaultDepth.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x11.XDefaultDepth.restype = ctypes.c_int
        self.x11.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x11.XRootWindow.restype = ctypes.c_ulong
        self.x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x11.XFree.argtypes = [ctypes.c_void_p]
        self.xext.XShmQueryExtension.argtypes = [ctypes.c_void_p]
        self.xext.XShmQueryExtension.restype = ctypes.c_int
        self.xext.XShmCreateImage.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
            ctypes.c_void_p, ctypes.POINTER(_XShmSegmentInfo),
            ctypes.c_uint, ctypes.c_uint]
        self.xext.XShmCreateImage.restype = image_pointer
        self.xext.XShmAttach.argtypes = [ctypes.c_void_p,
                                         ctypes.POINTER(_XShmSegmentInfo)]
        self.xext.XShmAttach.restype = ctypes.c_int
        self.xext.XShmDetach.argtypes = [ctypes.c_void_p,
                                         ctypes.POINTER(_XShmSegmentInfo)]
        self.xext.XShmGetImage.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, image_pointer,
            ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
        self.xext.XShmGetImage.restype = ctypes.c_int
        self.libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
        self.libc.shmget.restype = ctypes.c_int
        self.libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        self.libc.shmat.restype = ctypes.c_void_p
        self.libc.shmdt.argtypes = [ctypes.c_void_p]
        self.libc.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]

    def fileno(self):
        return self.events.fileno()

    def has_pending_damage(self):
        """Return whether python-xlib already queued an XDamage event.

        A synchronous request on this connection can read a later
        DamageNotify while waiting for its own reply. python-xlib keeps that
        event in memory, leaving the X socket empty. Callers must check this
        before sleeping in select(), because descriptor readiness alone
        cannot represent the in-process queue.
        """
        return bool(self.events.pending_events())

    def _bucket(self, rect):
        x, y, width, height = rect
        x, y = max(0, x), max(0, y)
        width = max(1, min(width, self.width - x))
        height = max(1, min(height, self.height - y))
        bucket_w = min(self.width, (width + 63) // 64 * 64)
        bucket_h = min(self.height, (height + 31) // 32 * 32)
        x = min(x, self.width - bucket_w)
        y = min(y, self.height - bucket_h)
        return x, y, bucket_w, bucket_h

    def _buffer(self, width, height):
        key = (width, height)
        found = self._buffers.pop(key, None)
        if found is None:
            found = _ShmImage(self, width, height)
        self._buffers[key] = found
        while len(self._buffers) > self.max_buffers:
            _, old = self._buffers.popitem(last=False)
            old.close()
        return found

    def capture_rect(self, rect):
        x, y, width, height = self._bucket(rect)
        pixels = self._buffer(width, height).capture(self.drawable, x, y)
        stride = self.width * 3
        row_bytes = width * 3
        for row in range(height):
            source = row * row_bytes
            target = (y + row) * stride + x * 3
            self.frame[target:target + row_bytes] = pixels[source:source + row_bytes]
            cursor_frame = getattr(self, "_cursor_frame", None)
            if cursor_frame is not None:
                cursor_frame[target:target + row_bytes] = pixels[source:source + row_bytes]
        return x, y, width, height

    @staticmethod
    def _union_rect(union, rect):
        if rect is None:
            return union
        if rect[2] <= 0 or rect[3] <= 0:
            return union
        if union is None:
            return rect
        x0 = min(union[0], rect[0])
        y0 = min(union[1], rect[1])
        x1 = max(union[0] + union[2], rect[0] + rect[2])
        y1 = max(union[1] + union[3], rect[1] + rect[3])
        return x0, y0, x1 - x0, y1 - y0

    def _clip_rect(self, rect):
        if rect is None or rect[2] <= 0 or rect[3] <= 0:
            return None
        x0 = max(0, int(rect[0]))
        y0 = max(0, int(rect[1]))
        x1 = min(self.width, int(rect[0]) + int(rect[2]))
        y1 = min(self.height, int(rect[1]) + int(rect[3]))
        if x1 <= x0 or y1 <= y0:
            return None
        return x0, y0, x1 - x0, y1 - y0

    @staticmethod
    def _rects_touch(first, second):
        return not (
            first[0] + first[2] < second[0]
            or second[0] + second[2] < first[0]
            or first[1] + first[3] < second[1]
            or second[1] + second[3] < first[1]
        )

    def _coalesce_rects(self, rects, limit=MAX_DAMAGE_RECTS):
        """Bound region count while retaining distant damage separately."""
        merged = []
        for value in rects:
            candidate = self._clip_rect(value)
            if candidate is None:
                continue
            index = 0
            while index < len(merged):
                if self._rects_touch(candidate, merged[index]):
                    candidate = self._union_rect(
                        candidate, merged.pop(index))
                    index = 0
                else:
                    index += 1
            merged.append(candidate)

        while len(merged) > limit:
            best = None
            for first_index in range(len(merged) - 1):
                first = merged[first_index]
                first_area = first[2] * first[3]
                for second_index in range(first_index + 1, len(merged)):
                    second = merged[second_index]
                    union = self._union_rect(first, second)
                    extra = (union[2] * union[3] - first_area
                             - second[2] * second[3])
                    choice = (extra, first_index, second_index, union)
                    if best is None or choice[:3] < best[:3]:
                        best = choice
            _, first_index, second_index, union = best
            merged[first_index] = union
            merged.pop(second_index)
        return tuple(sorted(merged, key=lambda rect: (rect[1], rect[0])))

    def _capture_rects(self, rects):
        bucketed = []
        for rect in rects:
            clipped = self._clip_rect(rect)
            if clipped is not None:
                bucketed.append(self._bucket(clipped))
        captured = self._coalesce_rects(bucketed)
        for rect in captured:
            self.capture_rect(rect)
        return captured

    def _extract_damage(self):
        """Move one snapshot of accumulated damage into the server region."""
        self.events.damage_subtract(
            self.damage_id, parts=self.damage_region)
        protocol = self.events.display
        fetched = _XFixesFetchRegion(
            display=protocol,
            opcode=protocol.get_extension_major("XFIXES"),
            region=self.damage_region,
        )
        rects = []
        for area in fetched.rectangles:
            rect = self._clip_rect(
                (int(area.x), int(area.y),
                 int(area.width), int(area.height)))
            if rect is not None:
                rects.append(rect)
        return tuple(rects)

    def _take_damage(self):
        """Atomically extract damage, including events queued by the reply.

        FetchRegion is a synchronous request.  While python-xlib waits for its
        reply it can receive a later DamageNotify and place that event in its
        in-process queue.  The X socket is then empty, so a caller waiting only
        with select() would never wake for that queued event.  Drain and
        extract again until the reply round trip leaves no queued notification;
        an event arriving after the final check remains on the socket and
        wakes the caller normally.
        """
        rects = []
        while True:
            while self.events.pending_events():
                self.events.next_event()
            rects.extend(self._extract_damage())
            if not self.events.pending_events():
                return self._coalesce_rects(rects)

    def _drain_damage(self):
        return self._take_damage()

    def _with_cursor_damage(self):
        if not self._cursor_supported:
            return bytes(self.frame), ()
        result = getattr(self, "_cursor_frame", None)
        if result is None or len(result) != len(self.frame):
            result = bytearray(self.frame)
            self._cursor_frame = result
        elif self._cursor_rect is not None:
            x, y, width, height = self._cursor_rect
            row_bytes = width * 3
            for row in range(y, y + height):
                start = (row * self.width + x) * 3
                result[start:start + row_bytes] = self.frame[start:start + row_bytes]
        try:
            cursor = self.events.xfixes_get_cursor_image(self.root)
        except Exception:
            self._cursor_signature = None
            self._cursor_rect = None
            # The base frame is still valid. Unknown cursor state merely makes
            # the presenter use its full-frame CPU comparison for this offer.
            return bytes(result), None
        left = int(cursor.x) - int(cursor.xhot)
        top = int(cursor.y) - int(cursor.yhot)
        cursor_width = int(cursor.width)
        cursor_height = int(cursor.height)
        signature = (
            int(cursor.cursor_serial), left, top, cursor_width, cursor_height)
        cursor_rect = self._clip_rect(
            (left, top, cursor_width, cursor_height))
        cursor_damage = ()
        if signature != self._cursor_signature:
            cursor_damage = self._coalesce_rects(
                (self._cursor_rect, cursor_rect))
        for cy in range(cursor_height):
            y = top + cy
            if not 0 <= y < self.height:
                continue
            for cx in range(cursor_width):
                x = left + cx
                if not 0 <= x < self.width:
                    continue
                value = int(cursor.cursor_image[cy * cursor_width + cx])
                alpha = value >> 24
                if not alpha:
                    continue
                # XFixes returns premultiplied ARGB.
                at = (y * self.width + x) * 3
                inverse = 255 - alpha
                result[at] = ((value >> 16) & 0xFF) + result[at] * inverse // 255
                result[at + 1] = ((value >> 8) & 0xFF) + result[at + 1] * inverse // 255
                result[at + 2] = (value & 0xFF) + result[at + 2] * inverse // 255
        self._cursor_signature = signature
        self._cursor_rect = cursor_rect
        return bytes(result), cursor_damage

    def _with_cursor(self):
        return self._with_cursor_damage()[0]

    def snapshot_with_damage(self, capture_full=False):
        captured = ()
        if capture_full:
            captured = (self.capture_rect(
                (0, 0, self.width, self.height)),)
        frame, cursor_damage = self._with_cursor_damage()
        if cursor_damage is None:
            return frame, None
        return frame, self._coalesce_rects((*captured, *cursor_damage))

    def snapshot(self, capture_full=False):
        return self.snapshot_with_damage(capture_full)[0]

    def pump(self):
        rects = self._drain_damage()
        if not rects:
            return None
        captured = self._capture_rects(rects)
        frame, cursor_damage = self._with_cursor_damage()
        if cursor_damage is None:
            return frame, None
        return frame, self._coalesce_rects((*captured, *cursor_damage))

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        buffers = getattr(self, "_buffers", {})
        for value in buffers.values():
            value.close()
        buffers.clear()
        display = getattr(self, "display", None)
        if display:
            self.x11.XCloseDisplay(display)
            self.display = None
        events = getattr(self, "events", None)
        if events:
            try:
                if self.damage_id is not None:
                    events.damage_destroy(self.damage_id)
                if self.damage_region is not None:
                    protocol = events.display
                    _XFixesDestroyRegion(
                        display=protocol,
                        opcode=protocol.get_extension_major("XFIXES"),
                        region=self.damage_region,
                    )
                events.sync()
            except Exception:
                pass
            events.close()
            self.events = None
            self.damage_id = None
            self.damage_region = None
