"""kilix desktop — XPane: an X11 application inside a kilix 95 window.

The apprun recipe, embedded: the app runs on a private Xvfb sized to the
window's client area, event-driven XDamage/MIT-SHM capture feeds frames into
the window surface through the Desk's fd hooks, and mouse/keys are injected
with XTest into the private display only. Processes are owned by a
StreamSupervisor, so closing the window (or the desktop) tears everything
down. Also here: InstallerWindow, a small log-tailing window that runs
`games.py <target> --setup-only` and fires a callback on success.
"""
import os
import signal
import subprocess
import tempfile
import time

from PIL import Image, ImageChops

import theme as T
import widgets as W
import wm
import storage

import clipboard                  # one shared clipboard across panes/windows
import stream                     # from config/ (main.py puts it on the path)
import xcapture
import xinject
from kilix_sdk import xapp as xapp_sdk
from Xlib import display as xdisplay, X, Xatom

_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The private X server's root is painted this chroma and keyed out when the
# frame is composited, so only the app's own (rectangular, opaque) windows —
# the skin — show on the desktop. Classic skins never use pure magenta.
CHROMA = (255, 0, 255)


class _XSurface(W.Widget):
    """The client-area widget: shows the captured frames, forwards input."""
    focusable = True

    def __init__(self, pane, w, h):
        super().__init__(0, 0, w, h)
        self.pane = pane

    def draw(self, d, img):
        p = self.pane
        tw, th = p.w, p.h                    # scale the native frame to the window
        fr = p.frame_img
        if fr is not None:
            if fr.size != (tw, th):
                fr = fr.resize((tw, th))
            img.paste(fr, (self.x, self.y))
        else:
            img.paste(CHROMA, (self.x, self.y, self.x + tw, self.y + th))

    def on_mouse(self, ev):
        self.pane.inject_mouse(ev)
        return True

    def on_key(self, ev):
        self.pane.inject_key(ev)
        return True


class XPane(wm.Window):
    """An X11 app shown directly on the desktop with no kilix window chrome:
    the app runs on a private Xvfb whose root is chroma-keyed away, so its own
    skin (title bar, buttons, dragging) is all the UI — Winamp-on-Win95 style.
    Clicks on the keyed-out (transparent) gaps fall through to the desktop."""
    _seq2 = 0
    _GRIP = 8                              # resize-grip band (px) at the edges

    def __init__(self, desk, cmd, title, icon="exe", app_size=None,
                 fps=15, env=None, cwd=None, fill=False, cleanup=None):
        self._external_cleanup = cleanup
        self._external_cleanup_done = False
        sw, sh = desk.size()
        aw, ah = app_size or (sw, sh - T.TASKBAR_H)
        super().__init__(desk, title, aw, ah, x=0, y=0, icon=icon,
                         chromeless=True)
        # resizable by scaling: the window resizes, but the app keeps rendering
        # at its native Xvfb size (app_w/app_h) — the frame is scaled to fit and
        # input coords are mapped back, so the Xvfb/ffmpeg never have to resize.
        self.resizable = True
        self.min_w, self.min_h = 240, 160
        XPane._seq2 += 1
        self.app_w, self.app_h = aw, ah
        self.frame_img = None
        self._last_frame = None
        # fully transparent until the first real frame, so the magenta chroma
        # fill never flashes onto the desktop during startup
        self.compose_mask = Image.new("L", (aw, ah), 0)
        self.fsize = aw * ah * 3
        self.buf = bytearray()
        self.capture = None
        self.ff = None
        initial_frame = None
        self._dead = False
        self.xapp = xapp_sdk.XAppSession(
            f"desk-xpane-{os.getpid()}-{XPane._seq2}", aw, ah, fps)
        self.sup = self.xapp.supervisor
        # any failure past here leaks Xvfb/app/display-lock unless we clean up
        try:
            # the desktop paints its own pointer, so hide Xvfb's software cursor
            n = self.xapp.start_xvfb(nocursor=True)
            self.xd = self.xapp.connect()
            self._paint_root_chroma()
            self.app = self.xapp.launch_app(cmd, env=env, cwd=cwd)
            self.inj = self.xapp.make_injector()
            started = self.xapp.start_capture(draw_cursor=False)
            self.capture = self.xapp.capture
            self.ff = self.xapp.capture_process
            initial_frame = started.initial_frame
        except Exception:
            self.xapp.close()
            self._run_external_cleanup()
            raise
        self.add(_XSurface(self, aw, ah))
        self.set_focus(self.widgets[-1])
        # bridge this pane's private CLIPBOARD to the shared hub. Best-effort:
        # a copy/paste failure must never stop the app window from opening.
        self.clip = None
        try:
            self.clip = clipboard.SelectionBridge(
                desk, self.xapp.display, self.xapp.xauthority)
        except Exception:
            self.clip = None
        # micro-WM on the private Xvfb: advertise just enough EWMH that the
        # app's own title-bar buttons emit minimize/maximize requests, then
        # translate those into kilix 95 window ops. Best-effort; the buttons
        # simply stay inert (as before) on failure. KILIX_XPANE_WM=0 opts out.
        self._wm_on = False
        if os.environ.get("KILIX_XPANE_WM", "1") != "0":
            try:
                self._wm_setup()
            except Exception:
                self._wm_on = False
        self._born = time.time()
        # fill: a general app opened "in a window" should maximize; with no WM
        # on the Xvfb we resize its main window ourselves until it settles
        self.fill = fill
        self._fill_deadline = self._born + 15 if fill else 0.0
        self._fill_at = 0.0
        if self.capture is not None:
            desk.add_fd(self.capture.fileno(), self._pump_damage)
            self._accept_frame(initial_frame)
        else:
            desk.add_fd(self.ff.stdout.fileno(), self._pump)
        desk.tick_hooks.append(self._tick)

    def hit_test(self, gx, gy):
        # only the opaque skin pixels belong to us; clicks on the keyed-out
        # gaps fall through to the desktop (icons) or windows behind — EXCEPT
        # the resize-grip band at the edges, which we always claim so the window
        # is grabbable even over the app's own transparent (rounded) corners.
        if not self.hit(gx, gy):
            return False
        if self.resizable and not self.maximized:
            lx, ly, g = gx - self.x, gy - self.y, self._GRIP
            if lx < g or ly < g or lx >= self.w - g or ly >= self.h - g:
                return True
        if self.compose_mask is None:
            return False
        try:
            return self.compose_mask.getpixel((gx - self.x, gy - self.y)) != 0
        except Exception:
            return False

    def _edge_at(self, lx, ly):
        # a wider resize grip than the default chrome frame — the app fills the
        # window edge-to-edge with no visible border to aim at.
        if not self.resizable or self.maximized:
            return ""
        g, e = self._GRIP, ""
        if ly < g:
            e += "n"
        elif ly >= self.h - g:
            e += "s"
        if lx < g:
            e += "w"
        elif lx >= self.w - g:
            e += "e"
        return e

    def on_resize(self):
        # window resized: grow the surface widget to fill it (so clicks in the
        # enlarged area still land) — the frame is scaled at draw time — and keep
        # the chroma mask matched to the new surface size for the compositor.
        for w in self.widgets:
            if isinstance(w, _XSurface):
                w.w, w.h = self.w, self.h
        if self.compose_mask is not None and \
                self.compose_mask.size != (self.w, self.h):
            self.compose_mask = self.compose_mask.resize(
                (self.w, self.h), Image.NEAREST)
        self.invalidate()

    def _paint_root_chroma(self):
        try:
            scr = self.xd.screen()
            pix = scr.default_colormap.alloc_color(
                CHROMA[0] * 257, CHROMA[1] * 257, CHROMA[2] * 257).pixel
            scr.root.change_attributes(background_pixel=pix)
            scr.root.clear_area(x=0, y=0, width=self.app_w, height=self.app_h)
            self.xd.sync()
        except Exception:
            pass

    # ── micro-WM: the app's own min/max buttons drive the kilix window ───────
    def _wm_setup(self):
        """Advertise a minimal EWMH window manager on the private Xvfb. GTK/Qt
        only send _NET_WM_STATE / WM_CHANGE_STATE requests (what the maximize
        and minimize buttons do) when they believe a compliant WM is present —
        so we plant the _NET_SUPPORTING_WM_CHECK beacon and list the states we
        honour in _NET_SUPPORTED, then listen for those requests on the root.
        We do NOT take SubstructureRedirect: the app maps its own windows as
        before, we only observe."""
        d = self.xd
        root = d.screen().root
        A = d.intern_atom
        self._A_STATE = A("_NET_WM_STATE")
        self._A_MAX_V = A("_NET_WM_STATE_MAXIMIZED_VERT")
        self._A_MAX_H = A("_NET_WM_STATE_MAXIMIZED_HORZ")
        self._A_FULLSCR = A("_NET_WM_STATE_FULLSCREEN")
        self._A_CHANGE_STATE = A("WM_CHANGE_STATE")
        a_check = A("_NET_SUPPORTING_WM_CHECK")
        a_supported = A("_NET_SUPPORTED")
        a_wm_name = A("_NET_WM_NAME")
        a_utf8 = A("UTF8_STRING")
        # the beacon window: root and it both point at it, and it carries a
        # _NET_WM_NAME — the exact structure GTK validates a live WM through.
        check = root.create_window(
            -100, -100, 1, 1, 0, X.CopyFromParent,
            window_class=X.InputOnly, visual=X.CopyFromParent)
        check.change_property(a_check, Xatom.WINDOW, 32, [check.id])
        check.change_property(a_wm_name, a_utf8, 8, b"kilix")
        root.change_property(a_check, Xatom.WINDOW, 32, [check.id])
        root.change_property(a_supported, Xatom.ATOM, 32,
                             [self._A_STATE, self._A_MAX_V, self._A_MAX_H,
                              self._A_FULLSCR])
        # observe requests routed to the root (sent with SubstructureNotify),
        # without redirecting the app's own map/configure traffic
        root.change_attributes(event_mask=X.SubstructureNotifyMask)
        d.flush()
        self._wm_check = check
        self._wm_on = True
        self.desk.add_fd(d.fileno(), self._pump_wm)   # wake instantly on a click

    def _pump_wm(self):
        if not getattr(self, "_wm_on", False):
            return
        try:
            n = self.xd.pending_events()
        except Exception:
            return
        for _ in range(n):
            try:
                ev = self.xd.next_event()
            except Exception:
                return
            try:
                self._handle_wm(ev)
            except Exception:
                pass

    def _handle_wm(self, ev):
        if ev.type != X.ClientMessage:
            return
        try:
            fmt, vals = ev.data
        except Exception:
            return
        if fmt != 32:
            return
        ct = ev.client_type
        if ct == self._A_STATE:
            # [action, atom1, atom2, source, 0]; action is
            # 0=remove, 1=add, 2=toggle. Keep repeated add/remove messages
            # idempotent so an app reasserting state cannot flip the pane back.
            if any(a in (self._A_MAX_V, self._A_MAX_H, self._A_FULLSCR)
                   for a in vals[1:3]):
                action = vals[0] if vals else 2
                maximized = getattr(self, "maximized", False)
                if action == 1 and not maximized:
                    self.desk.wm.toggle_maximize(self)
                elif action == 0 and maximized:
                    self.desk.wm.toggle_maximize(self)
                elif action == 2:
                    self.desk.wm.toggle_maximize(self)
        elif ct == self._A_CHANGE_STATE:
            if vals and vals[0] == 3:              # IconicState → minimize
                self.desk.wm.minimize(self)

    # ── frames in ───────────────────────────────────────────────────────────
    def _accept_frame(self, frame):
        if frame is None or frame == self._last_frame:
            return
        self._last_frame = frame
        self.frame_img = Image.frombytes(
            "RGB", (self.app_w, self.app_h), frame)
        # color-key: opaque everywhere the pixel differs from the chroma.
        diff = ImageChops.difference(
            self.frame_img, Image.new("RGB", self.frame_img.size, CHROMA))
        mask = diff.convert("L").point(lambda value: 0 if value == 0 else 255)
        if mask.size != (self.w, self.h):
            mask = mask.resize((self.w, self.h), Image.NEAREST)
        self.compose_mask = mask
        self.invalidate()

    def _pump_damage(self):
        try:
            update = self.capture.pump()
        except Exception:
            self.close()
            return
        if update is not None:
            self._accept_frame(update[0])

    def _pump(self):
        try:
            while True:
                chunk = os.read(self.ff.stdout.fileno(), 1 << 20)
                if not chunk:                     # ffmpeg gone: a pipe at EOF
                    self.desk.remove_fd(          # is permanently readable, so
                        self.ff.stdout.fileno())  # leaving the hook spins select
                    return
                self.buf += chunk
        except BlockingIOError:
            pass
        frame = None
        while len(self.buf) >= self.fsize:        # newest frame wins
            frame = bytes(self.buf[:self.fsize])
            del self.buf[:self.fsize]
        self._accept_frame(frame)

    def _tick(self, now):
        if self._dead:
            return
        if self.app.poll() is not None or \
                (self.ff is not None and self.ff.poll() is not None):
            self.close()                          # app or capture gone: close
            return
        self._pump_wm()                           # drain any min/max requests
        if self.fill and now < self._fill_deadline and now >= self._fill_at:
            self._fill_at = now + 0.5             # re-fill twice a second while
            self._fill_app_window()               # the app maps its main window
        self._keep_on_screen()

    def _fill_app_window(self):
        """No WM on the Xvfb: size the app's largest window to fill the pane, so
        a general app opened 'in a window' maximizes instead of floating small."""
        try:
            best = None
            for c in self.xd.screen().root.query_tree().children:
                if c.get_attributes().map_state != X.IsViewable:
                    continue
                g = c.get_geometry()
                if g.width <= 8 or g.height <= 8:  # skip tiny helper windows
                    continue
                area = g.width * g.height
                if best is None or area > best[1]:
                    best = (c, area, g)
            if best:
                c, _, g = best
                if (g.x, g.y, g.width, g.height) != (0, 0, self.app_w,
                                                     self.app_h):
                    c.configure(x=0, y=0, width=self.app_w, height=self.app_h)
                    self.xd.set_input_focus(c, X.RevertToPointerRoot,
                                            X.CurrentTime)
                    self.xd.sync()
        except Exception:
            pass

    def _keep_on_screen(self):
        """Keep every one of the app's windows fully within the visible
        region. kilix-amp is a multi-window docking app built for a real
        screen — its playlist/EQ windows (mapped only when toggled on, after
        startup) can dock or restore to positions off our region. Pull any
        out-of-bounds window just inside; windows already in view are left
        alone, so this never fights a drag happening on-screen."""
        # the visible region can shrink under us on a terminal resize; clamp
        # against the on-screen intersection, not the (stale) capture size, or
        # windows parked past the new edge become mouse-unreachable
        vw = min(self.app_w, self.desk.w)
        vh = min(self.app_h, self.desk.h - T.TASKBAR_H)
        try:
            for c in self.xd.screen().root.query_tree().children:
                if c.get_attributes().map_state != X.IsViewable:
                    continue
                g = c.get_geometry()
                if g.width <= 8 or g.height <= 8:
                    continue
                nx = min(max(0, g.x), max(0, vw - g.width))
                ny = min(max(0, g.y), max(0, vh - g.height))
                if (nx, ny) != (g.x, g.y):
                    c.configure(x=nx, y=ny)
            self.xd.sync()
        except Exception:
            pass

    # ── input out ───────────────────────────────────────────────────────────
    def inject_mouse(self, ev):
        # the window may be scaled; map surface coords back to the native size
        sx = self.app_w / self.w if self.w else 1.0
        sy = self.app_h / self.h if self.h else 1.0
        x, y = ev.x * sx, ev.y * sy
        try:
            if ev.wheel:
                self.inj.move_click(x, y, button=4 if ev.wheel < 0 else 5)
            elif ev.move:
                self.inj.move_click(x, y)
            else:
                self.inj.move_click(x, y, button=ev.btn, press=ev.press)
        except Exception:
            pass

    def inject_key(self, ev):
        # desk key events are press-only; tap the key, but hold any modifiers
        # around it so shortcuts reach the app (kilix-amp toggles EQ/playlist/
        # editor with Alt+G/E/D; games use Ctrl/Shift combos)
        try:
            mods = []
            if getattr(ev, "ctrl", False):
                mods.append("Control_L")
            if getattr(ev, "alt", False):
                mods.append("Alt_L")
            if getattr(ev, "shift", False):
                mods.append("Shift_L")
            for m in mods:
                self.inj.key_named(m, 1)
            self.inj.key(ev.key, 1)
            self.inj.key(ev.key, 3)
            for m in reversed(mods):
                self.inj.key_named(m, 3)
        except Exception:
            pass

    # ── teardown ────────────────────────────────────────────────────────────
    def _run_external_cleanup(self):
        if self._external_cleanup_done:
            return
        self._external_cleanup_done = True
        if self._external_cleanup is not None:
            try:
                self._external_cleanup()
            except Exception:
                pass

    def _teardown(self):
        if self._dead:
            return
        self._dead = True
        if self.clip is not None:
            self.clip.close()
        if getattr(self, "_wm_on", False):
            self.desk.remove_fd(self.xd.fileno())
        if getattr(self, "capture", None) is not None:
            self.desk.remove_fd(self.capture.fileno())
        elif self.ff is not None:
            self.desk.remove_fd(self.ff.stdout.fileno())
        if self._tick in self.desk.tick_hooks:
            self.desk.tick_hooks.remove(self._tick)
        self.xapp.close()
        self._run_external_cleanup()
        self.capture = self.ff = None

    def close(self):
        self._teardown()
        super().close()

    def request_close(self):
        self.close()


class InstallerWindow(wm.Window):
    """Runs `games.py <target> --setup-only` and tails its log; fires
    on_ok() when the install succeeds."""

    def __init__(self, desk, target, label, on_ok=None):
        super().__init__(desk, f"Installing {label}…", 480, 300,
                         icon="exe", resizable=False)
        self.on_ok = on_ok
        cw, ch = self.client_size()
        self.ta = self.add(W.TextArea(6, 6, cw - 12, ch - 12, ""))
        log_dir = storage.session_dir("desktop-installer-logs")
        os.makedirs(log_dir, mode=0o700, exist_ok=True)
        self.log = tempfile.NamedTemporaryFile(
            mode="w+", prefix=f"kilix-install-{target}-", suffix=".log",
            dir=log_dir)
        self.proc = subprocess.Popen(
            ["python3", os.path.join(_here, "games.py"), target,
             "--setup-only"],
            stdout=self.log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
        self._done = False
        self._log_len = 0
        desk.tick_hooks.append(self._tick)

    def _tick(self, now):
        if self._done:
            return
        try:
            with open(self.log.name) as f:
                text = f.read()
        except OSError:
            text = ""
        if len(text) != self._log_len:
            self._log_len = len(text)
            lines = text.splitlines()[-14:]
            self.ta.set_text("\n".join(lines))
            self.invalidate()
        rc = self.proc.poll()
        if rc is None:
            return
        self._done = True
        if self._tick in self.desk.tick_hooks:
            self.desk.tick_hooks.remove(self._tick)
        if rc == 0:
            self.close()
            if self.on_ok:
                # on_ok spawns the app (e.g. XPane → Xvfb/ffmpeg); a failure
                # here runs inside a tick hook, which Desk.run does not guard,
                # so an uncaught raise would take the whole desktop down
                try:
                    self.on_ok()
                except Exception as ex:
                    wm.msgbox(self.desk, "kilix",
                              f"Could not start:\n{ex}", icon="error")
        else:
            self.title = "Install failed"
            self.invalidate()

    def request_close(self):
        if self._tick in self.desk.tick_hooks:
            self.desk.tick_hooks.remove(self._tick)
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=1)
                except Exception:
                    pass
        self.close()
