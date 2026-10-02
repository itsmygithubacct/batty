#!/usr/bin/env python3
"""kilix run — prototype: an X11 app living inside a kilix/kitty pane.

The i3 idea turned inside-out: instead of a WM arranging app windows,
each app gets a private off-screen X server and its pixels are streamed
into a pane, so GUI apps tile exactly like terminal programs.

  - display : a per-instance Xvfb (found on PATH, or the user-space copy
    under ~/.local/gpu_terminal/kilix/data/xvfb/usr/bin/Xvfb)
  - pixels  : XDamage + MIT-SHM rectangles -> kitty graphics protocol,
    letterboxed into the pane by the GPU via c=/r= cell-scaling (ffmpeg
    x11grab remains the compatibility fallback)
  - input   : kitty keyboard protocol (with press/release reporting, so
    games can hold keys) + SGR-pixel mouse, injected with XTest — into
    the private display only, never the real one

Usage: kilix run [--size WxH] [--fps N] command [args…]
Keys : everything is forwarded to the app; Ctrl+Q quits the kitten.

Broadcast tiers (combinable): --hls (fMP4 HLS, scales out, ~1.5-2.5 s),
--mse (MPEG-TS over WebSocket -> mpegts.js, ~0.3-1 s), --webrtc (MediaMTX
WHEP, sub-500 ms), --audio (AAC from a PipeWire null sink), --no-pane
(headless: network tiers only). With a local pane, all encoders are fed
from its single capture (E4 fan-out). XDamage capture sleeps at zero work on
an idle screen; fallback polling downshifts until input wakes it.

Tab-fill + scalable: with no --size, the private X screen tracks the
pane — it starts at the pane's pixel size and a pane resize RESIZES the
display (RandR RRSetScreenSize on the Xvfb, debounced), refits the app
window, and restarts the capture, so the app always fills the tab 1:1.
--size pins the app resolution (games) and pane resizes then rescale the
picture on the GPU as before. Efficient/tiled: consecutive frames are
diffed into an exact changed rectangle and only that rectangle is retransmitted,
composed onto the displayed image via the kitty animation protocol
(a=f frame edits) — locally through a bounded POSIX shared-memory ring and
inline (t=d) when streamed. Scrolls can shift the existing root texture before
uploading exposed damage. Full placements establish the base at startup/resize
and periodically refresh streamed sessions for late attachers.

Known limits (prototype): apps that grab the pointer (e.g. DOSBox
autolock) see relative motion, so the app cursor and the pane cursor can
drift; with --size or broadcast tiers (--serve/--hls/--mse/--webrtc) the
X screen size stays fixed at startup — those pane resizes rescale the
picture instead of resizing the app.
"""
import json
import os
import select
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browse  # reuse Term (raw mode, kitty kbd/mouse parsing), not Chrome
import gfx      # direct (t=d) graphics transmission for streamed sessions
import stream   # Xvnc/Xvfb + VNC/HLS/bridge supervisor for serve modes
from app_profiles import (APP_PROFILE_STALE_SECONDS, cleanup_app_profile,
                          cleanup_stale_app_profiles, prepare_app_command)
from kilix_sdk import xapp as xapp_sdk

try:
    from Xlib import X
    from Xlib.ext import randr, xtest
except ImportError:
    sys.exit("kilix run: python3-xlib is required (apt install python3-xlib)")

LOG_PATH = os.environ.get("KILIX_RUN_LOG")

# The private display's framebuffer allocation; RRSetScreenSize can move the
# visible screen anywhere up to this. 4K default ≈ 24 MB of Xvfb framebuffer.
DEFAULT_MAX_SCREEN = "3840x2160"
# A changed capture proves that an app painted after the startup snapshot.
# Some fast, static apps finish before capture starts, however, so accept a
# stable initial frame after this grace period instead of leaving launchers
# waiting until their much longer failure timeout.
CONTENT_READY_GRACE = 3.0
CONTENT_READY_CHANGED = "changed"
CONTENT_READY_INITIAL_GRACE = "initial-grace"


def randr_prepare(xd):
    """Disable the CRTC on the private display so RRSetScreenSize can move the
    screen size freely (Xvfb rejects SetScreenSize while a CRTC is active with
    BadMatch). Must run before every resize now that randr_set_monitor_mode
    re-enables the CRTC. Returns True when the display is resizable this way."""
    try:
        root = xd.screen().root
        res = randr.get_screen_resources(root)
        if not res.crtcs:
            return False
        randr.set_crtc_config(xd, res.crtcs[0], res.config_timestamp,
                              0, 0, 0, randr.Rotate_0, [])
        xd.sync()
        return True
    except Exception as e:
        log("randr_prepare failed:", type(e).__name__, e)
        return False


def randr_set_screen_size(xd, w, h):
    """Resize the private display's screen (and root window) to w×h pixels.
    Physical size is derived at ~96 dpi (only toolkit font scaling reads it).
    Returns True on success."""
    try:
        root = xd.screen().root
        randr.set_screen_size(root, w, h,
                              max(1, w * 254 // 960), max(1, h * 254 // 960))
        xd.sync()
        g = root.get_geometry()
        if (g.width, g.height) != (w, h):
            log("randr_set_screen_size: no-op", g.width, g.height)
            return False
        return True
    except Exception as e:
        log("randr_set_screen_size failed:", type(e).__name__, e)
        return False


def randr_set_monitor_mode(xd, w, h, old_mode=None):
    """Re-enable the CRTC with a real w×h mode after an RRSetScreenSize, so
    the OUTPUT (monitor) geometry matches the screen. Toolkits place and size
    windows against the monitor, and with the CRTC merely disabled they fall
    back to the output's preferred mode — the framebuffer allocation — so a
    self-centering app (GIMP, most GTK/Qt) maps its window outside the
    captured pane box, hiding its menu bar behind nothing reachable. Xvfb
    does implement RRCreateMode & friends (verified on 21.x; older notes
    claiming otherwise were wrong). On failure the CRTC simply stays off,
    which is the previous behavior: only whole-monitor size queries see the
    stale size. Returns the active kilix mode id, or None."""
    try:
        root = xd.screen().root
        res = randr.get_screen_resources(root)
        if not res.crtcs or not res.outputs:
            return None
        crtc, output = res.crtcs[0], res.outputs[0]
        name = f"kilix-{w}x{h}"
        # Degenerate timings: only width/height matter to a virtual display;
        # totals stay non-zero so refresh math never divides by zero.
        info = (0, w, h, w * h * 60, w, w, w, 0, h, h, h, len(name), 0)
        mid = randr.create_mode(root, info, name).mode
        xd.sync()
        randr.add_output_mode(xd, output, mid)
        res = randr.get_screen_resources(root)   # config_timestamp moved
        randr.set_crtc_config(xd, crtc, res.config_timestamp,
                              0, 0, mid, randr.Rotate_0, [output])
        xd.sync()
        if old_mode:
            try:
                randr.delete_output_mode(xd, output, old_mode)
                randr.destroy_mode(xd, old_mode)
                xd.sync()
            except Exception:
                pass                             # stale mode: cosmetic only
        return mid
    except Exception as e:
        log("randr_set_monitor_mode failed (CRTC left off):",
            type(e).__name__, e)
        return None


def log(*a):
    if LOG_PATH:
        with stream._private_open(LOG_PATH, "a") as f:
            f.write(f"[{time.time():.3f}] " + " ".join(str(x) for x in a) + "\n")


def log_marker(marker):
    """Append one exact, untimestamped machine-readable protocol line."""
    marker = str(marker)
    if "\n" in marker or "\r" in marker:
        raise ValueError("log marker must be one line")
    if LOG_PATH:
        with stream._private_open(LOG_PATH, "a") as f:
            f.write(marker + "\n")


def _close_proc_streams(p):
    for stream_obj in (getattr(p, "stdin", None), getattr(p, "stdout", None),
                       getattr(p, "stderr", None)):
        if stream_obj is None:
            continue
        try:
            stream_obj.close()
        except Exception:
            pass


def _stop_proc(p, timeout=3):
    if p is None:
        return
    try:
        if p.poll() is None:
            p.terminate()
        p.wait(timeout=timeout)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
        try:
            p.wait(timeout=1)
        except Exception:
            pass
    _close_proc_streams(p)


def find_xvfb():
    p = shutil.which("Xvfb")
    if p:
        return p
    data = os.environ.get("KILIX_DATA_HOME") or os.path.join(
        os.environ.get("KILIX_STORAGE_HOME", os.path.expanduser(
            "~/.local/gpu_terminal/kilix")), "data")
    p = os.path.join(data, "xvfb", "usr", "bin", "Xvfb")
    if os.access(p, os.X_OK):
        return p
    sys.exit("kilix run: Xvfb not found — install xvfb, or unpack the .deb "
             "into ~/.local/gpu_terminal/kilix/data/xvfb (apt-get download "
             "xvfb && dpkg -x xvfb_*.deb "
             "~/.local/gpu_terminal/kilix/data/xvfb)")


# legacy CSI ~ numbers for F1-F12 (browse's tables stop at PageDown)
FKEY_TILDE = {11: "F1", 12: "F2", 13: "F3", 14: "F4", 15: "F5", 17: "F6",
              18: "F7", 19: "F8", 20: "F9", 21: "F10", 23: "F11", 24: "F12"}
FKEY_CSI = {"P": "F1", "Q": "F2", "S": "F4"}


class RunTerm(browse.Term):
    """browse.Term + key press/release reporting (kbd protocol flag 2)."""

    def enter(self):
        import tty
        tty.setraw(self.fd)
        # same as browse, but >15u: disambiguate + report event types +
        # alternates + all-keys-as-escapes — releases matter for games.
        # ?1003h = ANY-motion tracking (not just drag): the app needs free
        # pointer motion for hover/mouse-look, e.g. xeyes or an FPS.
        self.write("\x1b[?1049h\x1b[2J\x1b[?25l\x1b[?7l\x1b[>15u"
                   "\x1b[?1003h\x1b[?1006h\x1b[?1016h\x1b[?2004h")

    def restore(self):
        # browse.Term.restore disables ?1002l but not ?1003l (all-motion),
        # which we enable above — turn it off or the shell gets flooded.
        self.write("\x1b[?1003l")
        super().restore()

    def _parse_csi(self, params, final):
        ev = super()._parse_csi(params, final)
        parts = params.split(";") if params else []
        if ev is None and final == "~" and parts:
            num = int(parts[0].split(":")[0] or 0)
            if num in FKEY_TILDE:
                mods = int(parts[1].split(":")[0]) if len(parts) > 1 else 1
                ev = {"kind": "key", "key": FKEY_TILDE[num], "code": "",
                      "vk": 0, "mods": mods, "text": ""}
        if ev is None and final in FKEY_CSI:
            mods = int(parts[1].split(":")[0]) if len(parts) > 1 else 1
            ev = {"kind": "key", "key": FKEY_CSI[final], "code": "",
                  "vk": 0, "mods": mods, "text": ""}
        if ev and ev.get("kind") == "key":
            etype = 1  # 1 press, 2 repeat, 3 release
            if len(parts) > 1 and ":" in parts[1]:
                try:
                    etype = int(parts[1].split(":")[1] or 1)
                except ValueError:
                    pass
            ev["event"] = etype
        return ev


IDLE_AFTER = 5.0                     # QW5: downshift capture after this long
IDLE_FPS = 2                         # …to this rate (terminal screens idle ~95%)
KEEPALIVE = 1.0                      # E4: re-feed the last frame to idle sinks


class EncoderFeed:
    """E4 single-capture fan-out: the pane's rawvideo frames, written to the
    broadcast encoders' stdins, so one x11grab serves the pane AND every
    encoder. Non-blocking progressive writes of whole frames; if an encoder
    can't keep up the newest frame replaces the queued one (latest wins); a
    1 s keepalive re-sends the last frame so the VFR sinks (HLS segmenter,
    TS/RTSP mux) keep flowing while the screen is idle."""

    def __init__(self):
        self.sinks = []              # {fd, cur, off, next}
        self.last_fed = 0.0

    def add(self, proc):
        import fcntl
        fd = proc.stdin.fileno()
        os.set_blocking(fd, False)
        try:                         # F_SETPIPE_SZ: frames are ~1 MB, default
            fcntl.fcntl(fd, 1031, 1 << 20)   # 64 KB pipes mean 16 partials
        except OSError:
            pass
        self.sinks.append({"fd": fd, "cur": None, "off": 0, "next": None})

    def offer(self, frame, now):
        self.last_fed = now
        for s in self.sinks:
            if s["cur"] is None:
                s["cur"], s["off"] = memoryview(frame), 0
            else:
                s["next"] = frame    # replace any queued frame: latest wins

    def keepalive(self, frame, now):
        if frame is not None and self.sinks and now - self.last_fed >= KEEPALIVE:
            self.offer(frame, now)

    def pump(self):
        for s in self.sinks:
            while s["cur"] is not None:
                try:
                    n = os.write(s["fd"], s["cur"][s["off"]:])
                except BlockingIOError:
                    break
                except OSError:      # encoder died; supervisor reports it
                    s["cur"] = s["next"] = None
                    break
                s["off"] += n
                if s["off"] >= len(s["cur"]):
                    s["cur"], s["off"] = None, 0
                    if s["next"] is not None:
                        s["cur"], s["next"] = memoryview(s["next"]), None

    def pending_fds(self):
        return [s["fd"] for s in self.sinks if s["cur"] is not None]


class AppPane:
    def __init__(self, cmd, app_w, app_h, fps, serve=False, lan=False, hls=False,
                 audio=False, mse=False, webrtc=False, no_pane=False,
                 fill=False, auto_fit=False, manage_windows=True):
        self.cmd = cmd
        self.app_w, self.app_h = app_w, app_h   # None → sized from the pane
        self.fill = fill
        self.fps = fps
        # --serve: also expose the app to remote devices via Xvnc (view+control).
        # --hls/--mse/--webrtc/--lan add browser tiers (see stream.py).
        self.serve, self.lan, self.hls = serve, lan, hls
        self.mse, self.webrtc = mse, webrtc
        self.audio = audio               # capture app audio into the broadcasts
        self.no_pane = no_pane           # QW3: headless — no local pane at all
        # Ordinary X apps have no WM on their private display, so the runner
        # sizes, focuses and clamps their top-level windows. A desktop session
        # supplies its own WM and must retain sole ownership of those windows.
        self.manage_windows = bool(manage_windows)
        self.pulse_sink = None
        self.feed = EncoderFeed()
        self.session = os.environ.get("KILIX_SESSION") or f"run-{os.getpid()}"
        self.xapp = self.sup = None
        self.rfb_port = None
        self.disp_n = None
        self.full_pw = self.view_pw = None
        self.http_port = self.token = self.tls_fp = None
        self.rtsp_port = self.webrtc_port = None
        self.term = None if no_pane else RunTerm()
        # Tab-fill: with no --size and no broadcast tier, the private display
        # tracks the pane — it can then be resized live (RandR) when the pane
        # resizes. --size pins the app resolution; the network tiers bake WxH
        # into every encoder argv, so their display stays fixed too.
        self.resizable = (app_w is None and not no_pane
                          and not (serve or lan or hls or mse or webrtc))
        try:
            mw, mh = (int(v) for v in os.environ.get(
                "KILIX_RUN_MAX", DEFAULT_MAX_SCREEN).lower().split("x"))
        except ValueError:
            mw, mh = (int(v) for v in DEFAULT_MAX_SCREEN.split("x"))
        self.max_w, self.max_h = max(640, mw) & ~1, max(480, mh) & ~1
        if self.app_w is None and no_pane:
            self.app_w, self.app_h = 1280, 720
        if self.app_w is None:
            # No --size given: match the app's screen to the pane's usable
            # pixel area (full width × rows minus the status row), as the
            # terminal reports it (TIOCGWINSZ — so HiDPI scaling is already
            # baked in). The picture then renders 1:1 crisp instead of an
            # upscaled 640×400. Rounded down to even: the --serve/--hls
            # H.264 encoders (yuv420p) reject odd dimensions.
            t = self.term
            self.app_w = max(320, int(t.cols * t.cell_w)) & ~1
            self.app_h = max(200, int((t.rows - 1) * t.cell_h)) & ~1
        if self.resizable:
            # only pane-tracked displays live inside the max framebuffer
            # allocation; an explicit --size (or tier/headless size) is pinned
            # exactly as given, like before.
            self.app_w, self.app_h = (min(self.app_w, self.max_w),
                                      min(self.app_h, self.max_h))
        self.xapp = xapp_sdk.XAppSession(
            self.session, self.app_w, self.app_h, fps)
        self.sup = self.xapp.supervisor
        self.wid = os.environ.get("KITTY_WINDOW_ID", str(os.getpid()))
        # In a streamed/served session (KILIX_STREAM=1) pixels are inlined;
        # otherwise FramePresenter uses its bounded POSIX shared-memory ring.
        # img_id is stable per producer so multiple apps cannot collide.
        self.stream = os.environ.get("KILIX_STREAM") == "1"
        self.img_id = 1 + ((int(self.wid) if self.wid.isdigit()
                            else os.getpid()) % 4000)
        self.presenter = (gfx.FramePresenter(
            self.term, self.img_id, stream=self.stream,
            in_tmux=bool(os.environ.get("TMUX")), max_fps=fps)
            if self.term else None)
        self.frames = 0
        # Launchers wait for the first post-startup change, or for a stable
        # initial frame to survive CONTENT_READY_GRACE. The latter covers fast,
        # static apps that finish painting before capture can observe a change.
        self.content_frames = 0
        self._content_ready = False
        self._content_ready_reason = None
        self._initial_frame_ready_at = None
        # --debug / KILIX_DEBUG: capture-vs-blit fps + wire kbps, to a metrics
        # file and the status bar, for measuring streaming efficiency.
        self.debug = os.environ.get("KILIX_DEBUG") == "1"
        self._dbg = {"t0": time.time(), "cap": 0, "blit": 0, "bytes": 0,
                     "cfps": 0.0, "fps": 0.0, "kbps": 0.0}
        self.resized = False
        self._resize_deadline = None
        self.status = "starting…"
        self.prev_status = None
        self.last_frame = None
        self.ffbuf = bytearray()
        self.xvfb = self.app = self.ff = self.capture = None
        self.capture_backend = "pending"
        self._damage_unavailable = False
        self._cursor_capture_requested = False

        self.xd = None
        self._cap_fps = fps              # current pane-capture rate (QW5)
        self._last_change = time.time()
        self._fit_window_id = None
        self._last_window_fit = 0.0
        self._randr_mode = None          # active kilix-created RandR mode id
        fit_env = os.environ.get("KILIX_RUN_AUTO_FIT")
        self._auto_fit = (fit_env.lower() not in ("0", "false", "no", "off")
                          if fit_env is not None else auto_fit)
        self._fit_suspended = False
        if self.term:
            self.compute_layout()

    # ---- geometry ----------------------------------------------------------
    def compute_layout(self):
        t = self.term
        view_rows = t.rows - 1                     # last row = status
        if self.fill:
            # --fill: stretch the placement over the whole pane (games etc.
            # that should own the pane edge-to-edge; aspect is the app's
            # problem — pick an app --size with the ratio you want)
            self.img_cols, self.img_rows = t.cols, view_rows
            self.off_col = self.off_row = 0
        else:
            pane_w, pane_h = t.cols * t.cell_w, view_rows * t.cell_h
            scale = min(pane_w / self.app_w, pane_h / self.app_h)
            self.img_cols = max(1, round(self.app_w * scale / t.cell_w))
            self.img_rows = max(1, min(view_rows,
                                       round(self.app_h * scale / t.cell_h)))
            self.off_col = (t.cols - self.img_cols) // 2
            self.off_row = (view_rows - self.img_rows) // 2
        # the pixel box the image actually occupies, for mouse mapping
        self.box = (self.off_col * t.cell_w, self.off_row * t.cell_h,
                    self.img_cols * t.cell_w, self.img_rows * t.cell_h)

    # ---- processes ---------------------------------------------------------
    def start(self):
        self.start_display()
        self.start_app_and_capture()

    def start_display(self):
        if self.serve:
            self._start_xvnc()
        else:
            self._start_xvfb()
        # browser tiers work with either X server — the old code only offered
        # them under --serve, which needlessly dragged in the Xvnc dependency
        # for a broadcast-only use
        if self.lan or self.hls or self.mse or self.webrtc:
            self._start_web_tier(self.disp_n)

    def _start_xvfb(self):
        # Xvfb (with -auth, via the supervisor) so the private display is not
        # reachable by other local users on its /tmp/.X11-unix socket.
        # When the display tracks the pane, allocate the framebuffer at the
        # maximum (RRSetScreenSize can only move within the allocation) and
        # shrink the visible screen to the pane before the app starts.
        if self.resizable:
            n = self.xapp.start_xvfb(width=self.max_w, height=self.max_h)
        else:
            n = self.xapp.start_xvfb()
        self.disp = self.xapp.display
        self.disp_n = n
        self.xvfb = self.xapp.server
        log("Xvfb on", self.disp)

    def _start_xvnc(self):
        # Serve mode: run the app on Xvnc (a virtual X server that is ALSO a VNC
        # server), so the same display the local pane captures is exposed for
        # remote view+control. Xvnc lacks -displayfd, so reserve the number.
        n = self.sup.pick_display()
        # rfb port tied to the flock-reserved display (5900+n) so two kilix
        # serves never race for it; ephemeral fallback only if it's taken.
        self.rfb_port = 5900 + n if stream.port_free(5900 + n) else stream.free_port()
        # VncAuth caps passwords at 8 chars; two roles -> full + view-only.
        self.full_pw = self.sup.mint_token()[:8]
        self.view_pw = self.sup.mint_token()[:8]
        pwfile = self.sup.make_vncpw(self.full_pw, self.view_pw)
        self.xapp.start_xvnc(
            self.rfb_port, pwfile, number=n,
            desktop=f"kilix-run {os.path.basename(self.cmd[0])}")
        self.disp = self.xapp.display
        self.disp_n = n
        self.xvfb = self.xapp.server
        log("Xvnc on", self.disp, "rfb", self.rfb_port)

    def _start_web_tier(self, n):
        """Browser tiers: WS<->RFB bridge (noVNC), fMP4-HLS broadcast, the
        TS-over-WS low-latency feed (--mse) and/or WebRTC (--webrtc). Loopback
        by default, LAN over TLS+token with --lan. With a local pane the
        encoders are fed from its capture (E4 single-capture fan-out, piped);
        headless (--no-pane) they x11grab the display directly — one capture
        total either way."""
        self.token = self.sup.mint_token()
        monitor = None
        if self.audio:
            self.pulse_sink, monitor = self.sup.make_null_sink(self.session)
        piped = not self.no_pane
        w, h, fps = self.app_w, self.app_h, self.fps
        what = f"run {os.path.basename(self.cmd[0])}"
        need_bridge = self.lan or self.hls or self.mse
        hlsdir = ts_port = None
        tls = self.sup.tls_cert() if self.lan else None
        self.tls_fp = tls[2] if tls else None
        if need_bridge:
            self.http_port = stream.free_port()
            if self.hls:
                hlsdir = os.path.join(self.sup.runtime_dir, "hls")
            if self.mse:
                ts_port = stream.free_port()
            self.sup.start_bridge(rfb_port=self.rfb_port,
                                  http_port=self.http_port, token=self.token,
                                  hlsdir=hlsdir, ts_port=ts_port, tls=tls,
                                  lan=self.lan, what=what)
            log("bridge on http", self.http_port,
                "lan" if self.lan else "loopback")
        if self.hls:
            p = self.sup.start_hls(n, w, h, hlsdir, fps=fps, debug=self.debug,
                                   audio=monitor, piped=piped)
            if piped:
                self.feed.add(p)
        if self.mse:
            # the encoder connects OUT to the bridge's TS listener
            if not stream.wait_port(ts_port):
                raise RuntimeError("kilix: bridge TS port did not come up")
            p = self.sup.start_ts(n, w, h, ts_port, fps=fps, debug=self.debug,
                                  audio=monitor, piped=piped)
            if piped:
                self.feed.add(p)
        if self.webrtc:
            self.rtsp_port = stream.free_port()
            self.webrtc_port = (8889 if stream.port_free(8889)
                                else stream.free_port())
            self.sup.start_mediamtx(rtsp_port=self.rtsp_port,
                                    webrtc_port=self.webrtc_port,
                                    token=self.token, lan=self.lan, tls=tls)
            p = self.sup.start_rtsp_pub(n, w, h, self.rtsp_port, fps=fps,
                                        debug=self.debug, audio=monitor,
                                        piped=piped)
            if piped:
                self.feed.add(p)
            log("webrtc on", self.webrtc_port, "rtsp", self.rtsp_port)

    def start_app_and_capture(self):
        # Connect to the private display FIRST and shrink its screen to the
        # pane before the app starts, so the app's very first screen-size
        # query already sees the pane-tracked geometry.
        self.xd = self.xapp.connect()
        if self.resizable:
            if randr_prepare(self.xd) and randr_set_screen_size(
                    self.xd, self.app_w, self.app_h):
                self._randr_mode = randr_set_monitor_mode(
                    self.xd, self.app_w, self.app_h)
                log(f"display sized to pane: {self.app_w}x{self.app_h}")
            else:
                # RandR unavailable: the screen stays at the framebuffer max,
                # but the app window is still fitted (and captured) at pane
                # size, so only whole-screen size queries see the difference.
                self.resizable = False
                log("display resize unavailable; window-fit fallback")
        env = {}
        if self.pulse_sink:
            env["PULSE_SINK"] = self.pulse_sink   # route app audio to our sink
        self.app = self.xapp.launch_app(self.cmd, env=env)
        if self.term:
            self.inj = self.xapp.make_injector()
        self.focus_app_window()
        if self.term:                    # QW3: headless mode has no pane feed
            self._spawn_capture(self.fps)
        self.status = f"{os.path.basename(self.cmd[0])} on {self.disp}"

    def _spawn_capture(self, fps):
        """Start event-driven capture, with fixed-rate ffmpeg as fallback."""
        self._stop_capture()
        self.xapp.set_geometry(self.app_w, self.app_h)
        del self.ffbuf[:]                # drop any partial frame
        started = self.xapp.start_capture(
            fps=fps, draw_cursor=True,
            prefer_damage=not getattr(self, "_damage_unavailable", False))
        self.capture = self.xapp.capture
        self.ff = self.xapp.capture_process
        self._cap_fps = fps
        self.capture_backend = started.backend
        if started.damage_error is not None:
            self._damage_unavailable = True
            error = started.damage_error
            log("event-driven capture unavailable; ffmpeg fallback:",
                type(error).__name__, error)
        if self.capture is not None:
            log("capture event-driven (XDamage + MIT-SHM)")
            self._accept_frame(started.initial_frame, startup=True)
        else:
            log(f"capture at {fps}fps")

    def _stop_capture(self):
        if getattr(self, "xapp", None) is not None:
            self.xapp.stop_capture()
        self.capture = self.ff = None
        if getattr(self, "ffbuf", None):
            del self.ffbuf[:]

    def announce(self):
        """Print connect instructions to the LOCAL terminal (before the app
        takes the alt screen) and to a 0600 file. Passwords appear here only —
        never in the pane/status, and the VNC stream is the app's display, not
        this pane, so viewers never see them."""
        lan_host = stream.lan_ip() if self.lan else None
        self.sup.print_connect(what=f"app '{os.path.basename(self.cmd[0])}'",
                               rfb_port=self.rfb_port,
                               full_pw=self.full_pw, view_pw=self.view_pw,
                               http_port=self.http_port, token=self.token,
                               lan_host=lan_host, tls_fp=self.tls_fp,
                               have_hls=self.hls, have_ts=self.mse,
                               webrtc_port=self.webrtc_port)
        try:
            _cf = os.open(os.path.join(self.sup.runtime_dir, "connect.txt"),
                          os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(_cf, "w") as f:
                f.write(f"kilix run {' '.join(self.cmd)}\n")
                if self.rfb_port:
                    f.write(f"VNC        127.0.0.1:{self.rfb_port}\n"
                            f"control pw {self.full_pw}\n"
                            f"view-only  {self.view_pw}\n")
                if self.http_port:
                    scheme = "https" if self.lan else "http"
                    base = f"{scheme}://127.0.0.1:{self.http_port}"
                    f.write(f"browser    {base}/?t={self.token}\n")
                    if self.mse:
                        f.write(f"low-latency {base}/watch?t={self.token}\n")
                    if self.hls:
                        f.write(f"hls        {base}/hls/live.m3u8?t={self.token}\n")
                if self.webrtc_port:
                    scheme = "https" if self.lan else "http"
                    f.write(f"webrtc     {scheme}://127.0.0.1:{self.webrtc_port}"
                            f"/kilix  (user kilix, pass = token)\n"
                            f"token      {self.token}\n")
        except OSError:
            pass

    def _visible_app_windows(self):
        root = self.xd.screen().root
        out = []
        for c in root.query_tree().children:
            try:
                if c.get_attributes().map_state != X.IsViewable:
                    continue
                g = c.get_geometry()
                if g.width <= 8 or g.height <= 8:
                    continue
                out.append((c, g))
            except Exception:
                pass
        return out

    def fit_app_window(self, force=False):
        """Resize the topmost visible app window to the private display.

        VirtualBox maps the VM console as a later top-level window after the
        manager has already started. With no WM on Xvfb, that late window keeps
        its default small size unless kilix keeps fitting the active top-level.
        """
        windows = self._visible_app_windows()
        if not windows:
            return False
        c, g = windows[-1]               # XQueryTree children are bottom→top
        full = (0, 0, self.app_w, self.app_h)
        cid = getattr(c, "id", None)
        if force or cid != self._fit_window_id \
                or (g.x, g.y, g.width, g.height) != full:
            c.configure(x=0, y=0, width=self.app_w, height=self.app_h)
            self.xd.set_input_focus(c, X.RevertToPointerRoot, X.CurrentTime)
            xtest.fake_input(self.xd, X.MotionNotify,
                             x=self.app_w // 2, y=self.app_h // 2)
            self.xd.sync()
            self._fit_window_id = cid
            log("fit app window", hex(cid) if isinstance(cid, int) else cid)
        return True

    def focus_app_window(self):
        """No WM on the Xvfb: size the app's window to fill the virtual
        screen (small apps like xclock default to a tiny window in the
        top-left corner otherwise), focus it, and park the pointer inside
        (PointerRoot focus follows the pointer)."""
        if not self.manage_windows:
            return
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.fit_app_window(force=True):
                return
            if self.app.poll() is not None:
                raise RuntimeError(f"app exited (rc={self.app.returncode})")
            time.sleep(0.25)
        log("no app window appeared; capturing root anyway")

    def clamp_app_windows(self):
        """WM-less guard: pull any regular top-level that sticks out of the
        visible screen back inside it, shrinking it to the screen first when
        it is bigger. An app that restores saved geometry, or that mapped
        against stale monitor info before a resize, otherwise leaves its
        menu bar outside the captured pane box with no WM to drag it back.
        Windows already fully inside — dialogs, the fitted main window — are
        untouched, and override-redirect windows (menus, tooltips) are never
        moved."""
        dirty = False
        for c, g in self._visible_app_windows():
            try:
                if c.get_attributes().override_redirect:
                    continue
                kw = {}
                w, h = min(g.width, self.app_w), min(g.height, self.app_h)
                if (w, h) != (g.width, g.height):
                    kw["width"], kw["height"] = w, h
                x = min(max(g.x, 0), self.app_w - w)
                y = min(max(g.y, 0), self.app_h - h)
                if (x, y) != (g.x, g.y):
                    kw["x"], kw["y"] = x, y
                if kw:
                    c.configure(**kw)
                    dirty = True
                    log("clamped window", hex(getattr(c, "id", 0)),
                        f"{g.width}x{g.height}+{g.x}+{g.y} -> {w}x{h}+{x}+{y}")
            except Exception:
                pass
        if dirty:
            self.xd.sync()

    def maintain_app_window(self, now):
        """Keep late-mapped top-level windows filling the pane (--fit apps
        like VirtualBox), and every top-level inside the visible screen
        (all apps).

        This is intentionally periodic instead of event-driven: it avoids a
        second X event hook and still catches app-spawned windows within a
        fraction of a second.
        """
        if (not getattr(self, "manage_windows", True) or self.xd is None
                or getattr(self, "_fit_suspended", False)):
            return
        if now - getattr(self, "_last_window_fit", 0.0) < 0.5:
            return
        self._last_window_fit = now
        if getattr(self, "_auto_fit", True):
            self.fit_app_window()
        self.clamp_app_windows()

    # ---- pixel layer -------------------------------------------------------
    def _mark_content_ready(self, reason):
        if getattr(self, "_content_ready", False):
            return False
        if reason not in (CONTENT_READY_CHANGED,
                          CONTENT_READY_INITIAL_GRACE):
            raise ValueError(f"invalid content readiness reason: {reason}")
        self._content_ready = True
        self._content_ready_reason = reason
        self._initial_frame_ready_at = None
        log_marker(f"content-ready={reason}")
        return True

    def _settle_initial_frame(self, now):
        ready_at = getattr(self, "_initial_frame_ready_at", None)
        if (getattr(self, "_content_ready", False) or ready_at is None
                or now < ready_at or self.last_frame is None
                or self.frames < 1):
            return False
        return self._mark_content_ready(CONTENT_READY_INITIAL_GRACE)

    def _accept_frame(self, frame, *, content=False, startup=False,
                      damage=None):
        if frame is None:
            return False
        if self.last_frame is not None:
            if damage is None:
                if frame == self.last_frame:
                    return False
            else:
                damage = gfx.diff_damage_rects(
                    self.last_frame, frame, self.app_w, self.app_h, damage)
                if not damage:
                    return False
        self.last_frame = frame
        now = time.time()
        self._last_change = now
        self.feed.offer(frame, now)      # encoders always receive full frames
        if self.ff is not None and self._cap_fps != self.fps:
            self._spawn_capture(self.fps)
        self.blit(frame, damage=damage)
        if content:
            self.content_frames += 1
            if self.content_frames == 1:
                # Compatibility marker: this remains a literal count of
                # changed captures after the startup snapshot.
                log_marker("content-frames=1")
            self._mark_content_ready(CONTENT_READY_CHANGED)
        elif startup and not getattr(self, "_content_ready", False):
            self._initial_frame_ready_at = now + CONTENT_READY_GRACE
        return True

    def pump_damage(self):
        try:
            update = self.capture.pump()
        except Exception as error:
            log("event-driven capture failed; switching to ffmpeg:",
                type(error).__name__, error)
            self._damage_unavailable = True
            self._spawn_capture(self.fps)
            return
        if update is not None:
            self._dbg["cap"] += 1
            self._accept_frame(update[0], content=True, damage=update[1])

    def pump_frames(self):
        fd = self.ff.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 1 << 20)
            except BlockingIOError:
                break
            if not chunk:
                raise EOFError("ffmpeg capture stream closed")
            self.ffbuf += chunk
        fsize = self.app_w * self.app_h * 3
        frame = None
        while len(self.ffbuf) >= fsize:      # keep only the newest frame
            frame = bytes(self.ffbuf[:fsize])
            del self.ffbuf[:fsize]
            self._dbg["cap"] += 1            # frames captured (before change-detect)
        # The first fallback frame establishes the same startup baseline that
        # XDamage captures synchronously. A later changed frame is content.
        startup = self.last_frame is None
        self._accept_frame(frame, content=not startup, startup=startup)

    def tick_capture(self, now):
        """Idle housekeeping each loop pass: QW5 capture downshift and the E4
        keepalive that keeps VFR encoder sinks flowing on a static screen."""
        if self._cursor_capture_requested and self.capture is not None:
            self._cursor_capture_requested = False
            self._dbg["cap"] += 1
            try:
                frame, damage = self.capture.snapshot_with_damage()
            except Exception as error:
                log("cursor capture failed; switching to ffmpeg:",
                    type(error).__name__, error)
                self._damage_unavailable = True
                self._spawn_capture(self.fps)
            else:
                self._accept_frame(frame, damage=damage)
        if (self.ff is not None and self._cap_fps > IDLE_FPS
                and now - self._last_change > IDLE_AFTER):
            self._spawn_capture(IDLE_FPS)
        self.feed.keepalive(self.last_frame, now)
        self.feed.pump()
        if self.presenter is not None:
            self._record_present(self.presenter.flush())
        self._settle_initial_frame(now)

    def _record_present(self, result):
        if not result.emitted:
            return result
        self._blit_t = time.time()
        self.frames += 1
        self._dbg["blit"] += 1
        self._dbg["bytes"] += (result.wire_bytes if self.stream
                               else result.pixel_bytes)
        if self.frames == 1 or self.frames % 300 == 0:
            log(f"frames={self.frames}")
        return result

    def blit(self, rgb, force_full=False, scroll=None, damage=None):
        """Offer a complete RGB frame to the shared damage-aware presenter."""
        if getattr(self, "presenter", None) is None:
            self.presenter = gfx.FramePresenter(
                self.term, self.img_id, stream=self.stream,
                in_tmux=bool(os.environ.get("TMUX")),
                max_fps=getattr(self, "fps", 0))
        result = self.presenter.present(
            rgb, self.app_w, self.app_h, self.img_cols, self.img_rows,
            origin_row=self.off_row + 1, origin_column=self.off_col + 1,
            content_key="app", force_full=force_full, scroll=scroll,
            damage=damage)
        return self._record_present(result)

    def _dbg_tick(self):
        d = self._dbg
        dt = time.time() - d["t0"]
        if dt < 1.0:
            return
        d["cfps"], d["fps"] = d["cap"] / dt, d["blit"] / dt
        d["kbps"] = (d["bytes"] / dt) * 8 / 1000
        log(f"metrics cap={d['cfps']:.1f}/s blit={d['fps']:.1f}/s "
            f"wire={d['kbps']:.0f}kbps {self.app_w}x{self.app_h}")
        try:
            with stream._private_open(
                    os.path.join(self.sup.runtime_dir, "metrics.jsonl"), "a") as f:
                f.write(json.dumps({"t": round(time.time(), 1),
                                    "cap_fps": round(d["cfps"], 1),
                                    "blit_fps": round(d["fps"], 1),
                                    "pane_kbps": round(d["kbps"]),
                                    "capture": self.capture_backend,
                                    "w": self.app_w, "h": self.app_h}) + "\n")
        except Exception:
            pass
        d["t0"], d["cap"], d["blit"], d["bytes"] = time.time(), 0, 0, 0

    def render_status(self):
        dbg = ""
        if self.debug:
            self._dbg_tick()
            dbg = (f" · cap{self._dbg['cfps']:.0f} blit{self._dbg['fps']:.0f}/s "
                   f"{self._dbg['kbps']:.0f}kb/s {self.capture_backend}")
        serve = f" · VNC :{self.rfb_port}" if self.serve else ""
        fit = ""
        if (getattr(self, "manage_windows", True)
                and getattr(self, "_auto_fit", False)):
            fit = (" · F10 fit:off" if getattr(self, "_fit_suspended", False)
                   else " · F10 fit:on")
        size_tag = "≡pane" if self.resizable else "fixed"
        body = (f" kilix run — {' '.join(self.cmd)[:28]} · {self.disp} "
                f"{self.app_w}x{self.app_h} {size_tag} · {self.frames}f"
                f"{serve}{dbg}{fit} · Ctrl+Q")
        body = body[:self.term.cols].ljust(self.term.cols)
        s = f"\x1b[{self.term.rows};1H\x1b[0;7m{body}\x1b[0m"
        if s != self.prev_status:
            self.term.write(s)
            self.prev_status = s

    # ---- input (injected via xinject.Injector into the private display) ----
    def _wake_capture(self):
        """Restore fallback polling capture before injecting fresh input."""
        if getattr(self, "ff", None) is not None and \
                self._cap_fps != self.fps:
            self._spawn_capture(self.fps)

    def on_key(self, ev):
        self._wake_capture()
        mods = max(0, ev["mods"] - 1)
        etype = ev.get("event", 1)
        if (mods & 4) and ev["key"] == "q" and etype == 1:
            raise KeyboardInterrupt
        if (getattr(self, "manage_windows", True)
                and ev["key"] == "F10" and etype == 1
                and getattr(self, "_auto_fit", False)):
            self._fit_suspended = not getattr(self, "_fit_suspended", False)
            self.prev_status = None
            if not self._fit_suspended:
                self.fit_app_window(force=True)
        if etype == 2:
            return                       # Xvfb autorepeats held keys itself
        self.inj.key(ev["key"], etype)

    def on_paste(self, text):
        self._wake_capture()
        self.inj.paste(text)

    def on_mouse(self, ev):
        self._wake_capture()
        self.inj.mouse(ev, self.box)
        if self.capture is not None:
            # X cursors are server-side sprites and do not damage root pixels.
            # Query/composite the newest cursor once after the input batch.
            self._cursor_capture_requested = True

    # ---- lifecycle ---------------------------------------------------------
    def do_resize(self):
        """Apply a (debounced) pane resize.

        Tab-fill: when the display tracks the pane, resize the private X
        screen to the new pane pixel size (stop capture → RRSetScreenSize →
        refit app window → restart capture), so the app itself resizes with
        the tab instead of being rescaled. Fixed-size runs (--size, tiers)
        keep the old behavior: recompute the GPU-scaled placement only.
        """
        previous_frame = self.last_frame
        restart_capture = False
        self.term.refresh_size()
        if self.resizable:
            t = self.term
            w = max(320, int(t.cols * t.cell_w)) & ~1
            h = max(200, int((t.rows - 1) * t.cell_h)) & ~1
            w, h = min(w, self.max_w), min(h, self.max_h)
            if (w, h) != (self.app_w, self.app_h) and self.xd is not None:
                self._stop_capture()             # capture geometry is fixed
                if randr_prepare(self.xd) and \
                        randr_set_screen_size(self.xd, w, h):
                    self._randr_mode = randr_set_monitor_mode(
                        self.xd, w, h, old_mode=self._randr_mode)
                    self.app_w, self.app_h = w, h
                    if getattr(self, "inj", None) is not None:
                        self.inj.app_w, self.inj.app_h = w, h
                    if getattr(self, "manage_windows", True):
                        self.fit_app_window(force=True)
                    log(f"pane resize -> display {w}x{h}")
                else:
                    self.resizable = False       # degrade to GPU scaling
                restart_capture = True
        self.compute_layout()
        if self.presenter is not None:
            self.presenter.invalidate()
        self.prev_status = None
        # placement (i=…,p=1) is replaced on next blit; clear stale cells
        self.term.write("\x1b[2J")
        self.last_frame = None           # force a full re-blit at the new size
        if self.ffbuf:
            del self.ffbuf[:]
        # Initial capture can emit synchronously. Draw it only after clearing
        # stale placements, or an idle app can remain blank after a resize.
        if restart_capture:
            self._spawn_capture(self.fps)
        elif previous_frame is not None:
            self._accept_frame(previous_frame)

    def run(self):
        if self.term:
            signal.signal(signal.SIGWINCH,
                          lambda *a: setattr(self, "resized", True))
            os.set_blocking(self.term.fd, False)
        for _s in (signal.SIGTERM, signal.SIGHUP):   # -> finally + sup.cleanup()
            signal.signal(_s, lambda *a: sys.exit(0))
        err = None
        web = self.lan or self.hls or self.mse or self.webrtc
        if self.serve or web:
            # Bring the servers up first so connect details print to the LOCAL
            # terminal before the app takes the alt screen (and never into the
            # captured stream — secrets must not reach remote viewers).
            try:
                self.start_display()
                self.announce()
            except Exception as e:
                if self.xapp is not None:
                    self.xapp.close()
                print(f"kilix run: {e}", file=sys.stderr)
                sys.exit(1)
        if self.term:
            self.term.enter()
        try:
            if self.serve or web:
                self.start_app_and_capture()   # display already up
            else:
                self.start()
            self._loop_start = time.time()
            while True:
                # Cursor-image and other synchronous X replies can move a
                # DamageNotify into python-xlib's queue while leaving the
                # kernel socket empty. Consume it before select() so a
                # browser's first network paint cannot remain stranded.
                if (self.capture is not None and
                        self.capture.has_pending_damage()):
                    self.pump_damage()
                rlist = []
                if self.capture is not None:
                    rlist.append(self.capture)
                if self.ff is not None:
                    rlist.append(self.ff.stdout)
                if self.term:
                    rlist.append(self.term.fd)
                # write-select on encoder stdins with a queued frame, so a
                # briefly-full pipe drains as soon as the encoder catches up
                r, w, _ = select.select(rlist, self.feed.pending_fds(),
                                        [], 0.25)
                if self.capture is not None and self.capture in r:
                    self.pump_damage()
                if self.ff is not None and self.ff.stdout in r:
                    self.pump_frames()
                if self.term and self.term.fd in r:
                    for ev in browse.coalesce_mouse_motion(self.term.read_input()):
                        if ev["kind"] == "key":
                            self.on_key(ev)
                        elif ev["kind"] == "mouse":
                            self.on_mouse(ev)
                        elif ev["kind"] == "paste":
                            self.on_paste(ev["text"])
                now = time.time()
                if self.resized:
                    self.resized = False
                    # debounce: dragging a pane split fires SIGWINCH storms;
                    # apply once things settle (display+capture restarts are
                    # not free). Fixed-size runs relayout immediately.
                    if self.resizable:
                        self._resize_deadline = now + 0.30
                    else:
                        self.do_resize()
                if getattr(self, "_resize_deadline", None) and \
                        now >= self._resize_deadline:
                    self._resize_deadline = None
                    self.do_resize()
                self.tick_capture(now)
                self.maintain_app_window(now)
                # A first placement can be dropped right after startup (seen
                # as a pane that stays black while the app clearly has output;
                # q=2 means we never hear the error). Placement is otherwise
                # reliable, so self-heal by re-placing the current frame: fast
                # during a warmup window (recover in <1s), then a cheap idle
                # interval so an app sitting on a static screen isn't rewritten
                # every tick. Animation blits on its own and resets the timer.
                if self.term and self.last_frame is not None:
                    idle = now - getattr(self, "_blit_t", 0)
                    warming = now - self._loop_start < 4
                    if idle > (0.4 if warming else 3):
                        self.blit(self.last_frame, force_full=True)
                if self.app.poll() is not None:
                    err = (None if self.app.returncode == 0 else
                           f"app exited with rc={self.app.returncode}")
                    break
                if self.term:
                    self.render_status()
        except KeyboardInterrupt:
            pass
        except (EOFError, BrokenPipeError) as e:
            err = str(e)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        finally:
            if self.term:
                self.term.restore()
            if self.presenter is not None:
                self.presenter.close()
            self._stop_capture()
            if self.xapp is not None:
                self.xapp.close()
        if err:
            print(f"kilix run: {err}", file=sys.stderr)
            sys.exit(1)


def main():
    args = sys.argv[1:]
    app_w = app_h = None                 # default: sized from the pane
    fps = 20
    serve = lan = hls = audio = mse = webrtc = fill = False
    auto_fit = None
    manage_windows = True
    no_pane = os.environ.get("KILIX_NO_PANE") == "1"
    while args and args[0].startswith("--"):
        if args[0] == "--size" and len(args) > 1:
            app_w, app_h = (int(v) for v in args[1].lower().split("x"))
            args = args[2:]
        elif args[0].startswith("--size="):
            app_w, app_h = (int(v) for v in args[0][7:].lower().split("x"))
            args = args[1:]
        elif args[0] == "--fps" and len(args) > 1:
            fps = int(args[1])
            args = args[2:]
        elif args[0].startswith("--fps="):
            fps = int(args[0][6:])
            args = args[1:]
        elif args[0] == "--serve":
            serve = True
            args = args[1:]
        elif args[0] == "--lan":            # implies serve + LAN bridge (TLS+token)
            serve = lan = True
            args = args[1:]
        elif args[0] == "--hls":            # broadcast tier (many view-only viewers)
            hls = True
            args = args[1:]
        elif args[0] in ("--mse", "--ts"):  # TS-over-WS low-latency tier (E1)
            mse = True
            args = args[1:]
        elif args[0] == "--webrtc":         # sub-500ms WebRTC tier (E2, MediaMTX)
            webrtc = True
            args = args[1:]
        elif args[0] == "--no-pane":        # QW3: headless, network tiers only
            no_pane = True
            args = args[1:]
        elif args[0] == "--fill":           # stretch over the whole pane
            fill = True
            args = args[1:]
        elif args[0] == "--refit-windows":  # keep late top-levels full-screen
            auto_fit = True
            args = args[1:]
        elif args[0] == "--no-refit-windows":
            auto_fit = False
            args = args[1:]
        elif args[0] == "--desktop-session":
            # The child is a window manager/desktop, not a WM-less app. It
            # owns placement, focus and resize policy for every X window.
            manage_windows = False
            auto_fit = False
            args = args[1:]
        elif args[0] == "--debug":          # fps/bandwidth metrics -> status + file
            os.environ["KILIX_DEBUG"] = "1"
            args = args[1:]
        elif args[0] == "--audio":          # capture app audio into the broadcasts
            audio = True
            args = args[1:]
        else:
            sys.exit(f"kilix run: unknown option {args[0]}")
    if audio and not (hls or mse or webrtc):
        hls = True                          # audio needs a broadcast to ride in
    if not args:
        sys.exit("usage: kilix run [--size WxH] [--fps N] [--serve|--lan] "
                 "[--hls] [--mse] [--webrtc] [--audio] [--no-pane] [--debug] "
                 "[--desktop-session] "
                 "command [args…]\n"
                 "  --size defaults to the pane's pixel size")
    if auto_fit is None:
        auto_fit = os.path.basename(args[0]).lower() in (
            "virtualbox", "virtualboxvm", "vbox", "steam")
    args, temporary_profile = prepare_app_command(args)
    try:
        AppPane(args, app_w, app_h, fps, serve=serve, lan=lan, hls=hls,
                audio=audio, mse=mse, webrtc=webrtc, no_pane=no_pane,
                fill=fill, auto_fit=auto_fit,
                manage_windows=manage_windows).run()
    finally:
        cleanup_app_profile(temporary_profile)


if __name__ == "__main__":
    main()
