"""kilix — streaming supervisor: shared plumbing for the pixel-plane serve modes.

Used by `kilix run --serve` (Phase 2) and `kilix share` (Phase 3). Provides:
  - a private per-session runtime dir (0700) for sockets, pidfiles, secrets, logs
  - X display-number allocation held with an flock (TightVNC Xvnc has no
    -displayfd, so the server can't pick its own number)
  - Xvnc / Xvfb launch + readiness wait
  - a two-entry VncAuth password file (full control + view-only)
  - a bearer token and a first-use self-signed TLS cert (for the --lan bridge)
  - an HLS (H.264) broadcaster off an X display
  - connect-instruction printing (SSH-tunnel first)
  - teardown of every child on exit (atexit + signals)

Everything binds loopback by default; nothing here ever passes a non-loopback
interface to Xvnc, ffmpeg, or a socket. LAN exposure happens only through the
token+TLS wsbridge (config/wsbridge.py), never these primitives.
"""
import atexit
import fcntl
import glob
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request


def _runtime_root():
    base = os.environ.get("KILIX_SESSION_HOME") or os.path.join(
        os.environ.get("KILIX_STORAGE_HOME", os.path.expanduser(
            "~/.local/gpu_terminal/kilix")), "session")
    return os.path.abspath(os.path.join(base, "stream"))


def _safe_session_name(session):
    if (not isinstance(session, str) or not session
            or session in (".", "..", "locks")
            or "\0" in session or os.path.isabs(session)
            or os.path.basename(session) != session
            or (os.path.altsep and os.path.altsep in session)):
        raise ValueError("kilix: invalid stream session name")
    return session


def _safe_runtime_dir(root, path):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        return False
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    return path_real.startswith(root_real + os.sep)


def _proc_stat(pid):
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            data = f.read()
        tail = data.rsplit(") ", 1)[1].split()
        return {"state": tail[0], "start": tail[19] if len(tail) > 19 else None}
    except (OSError, ValueError, IndexError):
        return {}


def _proc_start_time(pid):
    return _proc_stat(pid).get("start")


def _read_pidfile(path):
    try:
        with open(path) as f:
            data = f.read().strip()
        meta = json.loads(data)
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    pid = meta.get("pid")
    start = meta.get("start")
    if isinstance(pid, int) and pid > 0 and isinstance(start, str) and start:
        return pid, start
    return None


def _pid_alive(pid):
    st = _proc_stat(pid)
    if st:
        return st.get("state") != "Z"
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _close_handles(handles):
    for h in handles:
        try:
            h.close()
        except Exception:
            pass


def _private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _private_open(path, mode):
    flags = os.O_CREAT | os.O_WRONLY
    flags |= os.O_APPEND if mode.startswith("a") else os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, mode)


def kill_all():
    """Reap every kilix stream process (Xvnc/Xvfb/ffmpeg/bridge) recorded in the
    runtime dirs' pidfiles, and remove the session dirs. Backstop for a serve
    that was SIGKILLed (bypassing its own atexit/SIGTERM cleanup)."""
    root = _runtime_root()
    if not os.path.isdir(root):
        return 0
    killed = 0
    for ent in os.scandir(root):
        if ent.name == "locks" or not ent.is_dir(follow_symlinks=False):
            continue
        d = ent.path
        if not _safe_runtime_dir(root, d):
            continue
        removable = True
        for pf in glob.glob(os.path.join(d, "*.pid")):
            meta = _read_pidfile(pf)
            if meta is None:
                removable = False
                continue
            pid, start = meta
            if _proc_start_time(pid) != start:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                killed += 1
            except OSError:
                pass
            deadline = time.time() + 3
            while _pid_alive(pid) and time.time() < deadline:
                time.sleep(0.05)
            if _pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            if _pid_alive(pid):
                removable = False
        if removable and _safe_runtime_dir(root, d):
            shutil.rmtree(d, ignore_errors=True)
    return killed


def _data_home():
    return os.environ.get("KILIX_DATA_HOME") or os.path.join(
        os.environ.get("KILIX_STORAGE_HOME", os.path.expanduser(
            "~/.local/gpu_terminal/kilix")), "data")


def _whoami():
    import getpass
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "user")


def _hostname():
    try:
        return socket.gethostname() or "HOST"
    except Exception:
        return "HOST"


def find_xvfb():
    p = shutil.which("Xvfb")
    if p:
        return p
    p = os.path.join(_data_home(), "deps", "usr", "bin", "Xvfb")
    return p if os.access(p, os.X_OK) else None


def find_xvnc():
    # TightVNC ships `Xvnc`; TigerVNC (Debian's tigervnc-standalone-server) ships
    # `Xtigervnc` (and only sets up the `Xvnc` alternative on a system install).
    return shutil.which("Xvnc") or shutil.which("Xtigervnc")


def _xvnc_is_tiger(xvnc):
    # Binary name is the reliable signal (Debian ships TigerVNC as `Xtigervnc`);
    # fall back to the -version banner for an `Xvnc`-named TigerVNC.
    if "tiger" in os.path.basename(xvnc or "").lower():
        return True
    try:
        r = subprocess.run([xvnc, "-version"], capture_output=True, text=True, timeout=5)
        return "tigervnc" in (r.stdout + r.stderr).lower()
    except Exception:
        return False


def _fontpath_args():
    # When fonts are unpacked into a no-sudo prefix (install-stream-deps.sh sets
    # KILIX_XFONTS) the X server's built-in default font path misses them, so it
    # can't open 'fixed' and refuses to start. Point it at the unpacked dirs.
    fp = os.environ.get("KILIX_XFONTS")
    return ["-fp", fp] if fp else []


_H264_CACHE = None
_VAAPI_DEVICE_CACHE = None


def _vaapi_device():
    """Find a render node that can actually encode H.264, not just open VA-API."""
    global _VAAPI_DEVICE_CACHE
    if _VAAPI_DEVICE_CACHE is not None:
        return _VAAPI_DEVICE_CACHE or None
    for device in sorted(glob.glob("/dev/dri/renderD[0-9]*")):
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-vaapi_device", device, "-f", "lavfi", "-i",
                 "color=size=64x64:rate=1", "-frames:v", "1",
                 "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi",
                 "-f", "null", "-"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            _VAAPI_DEVICE_CACHE = device
            return device
    _VAAPI_DEVICE_CACHE = ""
    return None


def _h264_encoder():
    """Pick the best available H.264 encoder in this ffmpeg. Returns a 'kind'
    string: 'x264' | 'openh264' | 'vaapi' | None. Fedora's ffmpeg-free ships no
    libx264, so we fall back to Cisco OpenH264 (software) or VAAPI (hardware).
    KILIX_HW=1 prefers a working VAAPI H.264 encoder (frees the CPU for
    capture/serve at some quality-per-bit cost). Cached."""
    global _H264_CACHE
    if _H264_CACHE is not None:
        return _H264_CACHE
    try:
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        enc = ""
    check_vaapi = "h264_vaapi" in enc
    prefer_hardware = os.environ.get("KILIX_HW") == "1"
    have_software = "libx264" in enc or "libopenh264" in enc
    have_vaapi = bool(_vaapi_device()) if check_vaapi and (prefer_hardware or not have_software) else False
    if have_vaapi and prefer_hardware:
        _H264_CACHE = "vaapi"
    elif "libx264" in enc:
        _H264_CACHE = "x264"
    elif "libopenh264" in enc:
        _H264_CACHE = "openh264"
    elif have_vaapi:
        _H264_CACHE = "vaapi"
    else:
        _H264_CACHE = None
    return _H264_CACHE


def _video_encode_args(fps, keyint_sec, vfr=False):
    """(global_pre_args, output_codec_args) for the chosen H.264 encoder, with a
    keyframe every keyint_sec seconds. For CFR x11grab input the GOP is counted
    in frames (fps*keyint_sec); a piped VFR feed (single-capture fan-out, frames
    arrive only on damage) instead forces keyframes on a WALLCLOCK schedule —
    frame-counted GOPs would stretch segments arbitrarily on an idle screen."""
    g = str(max(1, int(fps * keyint_sec)))
    kind = _h264_encoder()
    if kind == "x264":
        # veryfast + CRF28 + a VBV cap: ~55% less bitrate than ultrafast at equal
        # quality, still comfortably real-time; zerolatency governs latency.
        vargs = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                 "-crf", "28", "-maxrate", "2M", "-bufsize", "1M",
                 "-g", g, "-keyint_min", g, "-sc_threshold", "0",
                 "-pix_fmt", "yuv420p"]
        pre = []
    elif kind == "openh264":               # Fedora ffmpeg-free (no -crf/-preset)
        vargs = ["-c:v", "libopenh264", "-b:v", "1500k",
                 "-g", g, "-pix_fmt", "yuv420p"]
        pre = []
    elif kind == "vaapi":                  # hardware encode (frees the CPU)
        vargs = ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi",
                 "-qp", "26", "-g", g]
        pre = ["-vaapi_device", _VAAPI_DEVICE_CACHE]
    else:
        raise RuntimeError("kilix: ffmpeg has no usable H.264 encoder "
                           "(need libx264, libopenh264, or h264_vaapi)")
    if vfr:
        # generic ffmpeg option — works with every encoder above
        vargs += ["-force_key_frames", f"expr:gte(t,n_forced*{keyint_sec})"]
    return pre, vargs


def _raw_input_args(w, h):
    """Input args for a piped rawvideo feed (the pane capture fanned out to the
    encoders — E4): frames arrive only when the screen changed, so timestamps
    come from the wallclock, not a nominal rate."""
    return ["-f", "rawvideo", "-pix_fmt", "rgb24", "-video_size", f"{w}x{h}",
            "-use_wallclock_as_timestamps", "1", "-i", "pipe:0"]


def wait_port(port, timeout=8.0, host="127.0.0.1"):
    """Poll until a TCP port accepts connections (bridge/mediamtx readiness)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        try:
            s.connect((host, port))
            return True
        except OSError:
            time.sleep(0.15)
        finally:
            s.close()
    return False


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def port_free(p):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


def lan_ip():
    """Best-effort primary non-loopback IPv4, for printing a --lan URL."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return _hostname()


class StreamSupervisor:
    def __init__(self, session):
        self.session = _safe_session_name(session)
        self.runtime_root = _private_dir(_runtime_root())
        self.runtime_dir = os.path.join(self.runtime_root, self.session)
        _private_dir(self.runtime_dir)
        # Display-number locks live in a SHARED dir (not the per-session runtime
        # dir), so the flock actually excludes OTHER concurrent serves from
        # picking the same X display before its socket appears.
        self.lockdir = os.path.join(self.runtime_root, "locks")
        _private_dir(self.lockdir)
        self.children = []     # list of (name, Popen, parent_handles, pidfile)
        self._locks = []       # held flock fds reserving display numbers
        self._pulse_modules = []   # pactl null-sink module ids to unload
        self._cleaned = False
        self.xauth = None      # per-session X authority file (MIT cookie)
        atexit.register(self.cleanup)

    # ---- process bookkeeping ------------------------------------------------
    def spawn(self, name, argv, **kw):
        handles = []
        seen = set()
        for key in ("stdin", "stdout", "stderr"):
            h = kw.get(key)
            if h is None or isinstance(h, int) or not hasattr(h, "close"):
                continue
            if id(h) in seen:
                continue
            seen.add(id(h))
            handles.append(h)
        try:
            p = subprocess.Popen(argv, **kw)
        except Exception:
            _close_handles(handles)
            raise
        pidfile = os.path.join(self.runtime_dir, f"{name}.pid")
        self.children.append((name, p, handles, pidfile))
        try:
            with _private_open(pidfile, "w") as f:
                json.dump({"pid": p.pid, "start": _proc_start_time(p.pid)}, f)
        except OSError:
            pass
        return p

    def cleanup(self, *_):
        if self._cleaned:
            return
        self._cleaned = True
        try:
            atexit.unregister(self.cleanup)
        except Exception:
            pass
        for _name, p, _handles, _pidfile in reversed(self.children):
            if p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        deadline = time.time() + 3
        for _name, p, handles, pidfile in reversed(self.children):
            try:
                p.wait(timeout=max(0.0, deadline - time.time()))
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
                try:
                    p.wait(timeout=1)
                except Exception:
                    pass
            for stream in (getattr(p, "stdin", None), getattr(p, "stdout", None),
                           getattr(p, "stderr", None)):
                if stream is None:
                    continue
                try:
                    stream.close()
                except Exception:
                    pass
            _close_handles(handles)
            try:
                os.unlink(pidfile)
            except OSError:
                pass
        self.children.clear()
        for fd in self._locks:
            try:
                os.close(fd)
            except OSError:
                pass
        self._locks.clear()
        for mod in self._pulse_modules:
            try:
                subprocess.run(["pactl", "unload-module", mod], capture_output=True)
            except Exception:
                pass
        self._pulse_modules.clear()
        if _safe_runtime_dir(self.runtime_root, self.runtime_dir):
            shutil.rmtree(self.runtime_dir, ignore_errors=True)

    # ---- display allocation -------------------------------------------------
    def pick_display(self, lo=60, hi=120):
        for n in range(lo, hi):
            if os.path.exists(f"/tmp/.X11-unix/X{n}"):
                continue
            fd = os.open(os.path.join(self.lockdir, f"X{n}.lock"),
                         os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                continue
            if os.path.exists(f"/tmp/.X11-unix/X{n}"):   # raced, appeared
                os.close(fd)
                continue
            self._locks.append(fd)
            return n
        raise RuntimeError("kilix: no free X display in range")

    def _wait_x(self, n, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(f"/tmp/.X11-unix/X{n}"):
                time.sleep(0.2)      # let it start accepting
                return True
            time.sleep(0.1)
        return False

    def make_xauth(self, n):
        """Per-session MIT-MAGIC-COOKIE-1 X-authority file (0600) for display :n.
        Without it, the display's world-accessible /tmp/.X11-unix/X<n> socket lets
        ANY local user connect (screenshot/keylog/inject). With -auth on the server
        and XAUTHORITY set on the clients we launch, only our processes get in."""
        path = os.path.join(self.runtime_dir, f"Xauth-{n}")
        _private_open(path, "a").close()
        subprocess.run(["xauth", "-f", path, "add", f":{n}",
                        "MIT-MAGIC-COOKIE-1", secrets.token_hex(16)],
                       check=True, capture_output=True)
        self.xauth = path
        return path

    # ---- X servers ----------------------------------------------------------
    def start_xvnc(self, n, w, h, port, pwfile, desktop="kilix"):
        xvnc = find_xvnc()
        if not xvnc:
            raise RuntimeError("kilix: Xvnc not found (needed for --serve)")
        auth = self.make_xauth(n)
        argv = [xvnc, f":{n}", "-geometry", f"{w}x{h}", "-depth", "24",
                "-rfbport", str(port), "-rfbauth", pwfile, "-auth", auth,
                "-localhost", "-desktop", desktop, "-nolisten", "tcp"] + _fontpath_args()
        if _xvnc_is_tiger(xvnc):
            # TigerVNC must be told to actually offer VncAuth (its default set
            # differs); TightVNC has no -SecurityTypes and would reject it.
            # -CompareFB 2: always diff the framebuffer before sending — cheap
            # CPU for a big bandwidth cut on terminal content, where most of
            # the screen is unchanged between updates (E6: VNC is the
            # text-efficiency tier; Tight beats H.264 on terminals).
            argv += ["-SecurityTypes", "VncAuth", "-CompareFB", "2"]
        logf = _private_open(os.path.join(self.runtime_dir, f"xvnc-{n}.log"), "wb")
        p = self.spawn(f"xvnc-{n}", argv, stdout=logf, stderr=logf)
        if not self._wait_x(n):
            raise RuntimeError("kilix: Xvnc did not come up (see runtime log)")
        return p

    def start_xvfb(self, n, w, h, nocursor=False):
        xvfb = find_xvfb()
        if not xvfb:
            raise RuntimeError("kilix: Xvfb not found")
        auth = self.make_xauth(n)
        argv = [xvfb, f":{n}", "-screen", "0", f"{w}x{h}x24",
                "-nolisten", "tcp", "-auth", auth] + _fontpath_args()
        if nocursor:
            # Xvfb draws a software cursor (the black root "X") into the
            # framebuffer, so ffmpeg -draw_mouse 0 can't remove it; disable it
            # entirely where the desktop paints its own pointer instead.
            argv.append("-nocursor")
        logf = _private_open(os.path.join(self.runtime_dir, f"xvfb-{n}.log"), "wb")
        p = self.spawn(f"xvfb-{n}", argv, stdout=logf, stderr=logf)
        if not self._wait_x(n):
            raise RuntimeError("kilix: Xvfb did not come up")
        return p

    def make_null_sink(self, name):
        """Create a PipeWire/PulseAudio null sink so a headless app's audio can
        be captured from its .monitor source. Returns (sink_name, monitor_source)
        or (None, None) if pactl is unavailable. The module is unloaded on
        cleanup. Set the app's PULSE_SINK to the returned sink name."""
        if not shutil.which("pactl"):
            return None, None
        sink = "kilix_" + "".join(c if c.isalnum() else "_" for c in name)
        r = subprocess.run(["pactl", "load-module", "module-null-sink",
                            f"sink_name={sink}",
                            f"sink_properties=device.description={sink}"],
                           capture_output=True, text=True)
        mod = r.stdout.strip()
        if not mod.isdigit():
            return None, None
        self._pulse_modules.append(mod)
        return sink, f"{sink}.monitor"

    # ---- broadcast encoders (H.264 + optional AAC audio) --------------------
    def _enc_argv(self, name, n, w, h, fps, keyint_sec, debug, audio, piped):
        """Shared front half of every broadcast encoder: metrics feed, video
        input (x11grab, or a piped rawvideo feed for single-capture fan-out),
        optional pulse-monitor audio, and the H.264 + AAC codec args."""
        pre, vargs = _video_encode_args(fps, keyint_sec, vfr=piped)
        argv = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if debug:
            # ffmpeg writes frame=/fps=/bitrate=/total_size=/drop_frames= here
            # every second, so the server-side encode rate can be read live.
            argv += ["-progress",
                     os.path.join(self.runtime_dir, f"{name}.progress"),
                     "-stats_period", "1"]
        argv += pre                        # global opts (e.g. -vaapi_device)
        if piped:
            argv += ["-thread_queue_size", "512"] + _raw_input_args(w, h)
        else:
            argv += ["-thread_queue_size", "512", "-f", "x11grab",
                     "-framerate", str(fps), "-video_size", f"{w}x{h}",
                     "-i", f":{n}"]
        if audio:                          # capture a pulse/pipewire monitor
            argv += ["-thread_queue_size", "512", "-f", "pulse", "-i", audio]
        argv += vargs                      # H.264 encoder (libx264/openh264/vaapi)
        if audio:
            argv += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                     "-af", "aresample=async=1"]
        return argv

    def _spawn_enc(self, name, argv, piped):
        logf = _private_open(os.path.join(self.runtime_dir, f"{name}.log"), "wb")
        env = dict(os.environ)
        if self.xauth:
            env["XAUTHORITY"] = self.xauth
        return self.spawn(name, argv, stdout=logf, stderr=logf,
                          stdin=subprocess.PIPE if piped else subprocess.DEVNULL,
                          env=env)

    def start_hls(self, n, w, h, outdir, fps=15, debug=False, audio=None,
                  hls_time=0.5, piped=False):
        """HLS broadcast. QW2: 0.5 s fMP4 segments, list of 3 — ~1.5-2.5 s glass
        latency, the segmented-HLS floor. The keyframe cadence must equal the
        segment length (HLS only cuts on an IDR frame). piped=True → feed frames
        on stdin (single-capture fan-out) instead of a second x11grab."""
        os.makedirs(outdir, exist_ok=True)
        m3u8 = os.path.join(outdir, "live.m3u8")
        argv = self._enc_argv(f"hls-{n}", n, w, h, fps, hls_time, debug,
                              audio, piped)
        argv += ["-f", "hls", "-hls_time", str(hls_time), "-hls_list_size", "3",
                 "-hls_segment_type", "fmp4",
                 "-hls_fmp4_init_filename", "init.mp4",
                 "-hls_flags",
                 "delete_segments+omit_endlist+independent_segments", m3u8]
        return self._spawn_enc(f"hls-{n}", argv, piped)

    def start_ts(self, n, w, h, ts_port, fps=15, debug=False, audio=None,
                 piped=False):
        """MPEG-TS to the wsbridge's loopback fan-out (E1: TS over WebSocket →
        mpegts.js/MSE, sub-second glass latency). Segment-free, so the GOP can
        be long (2 s) — a big bitrate saving on mostly-static screens. The
        bridge must already be listening on ts_port (see wait_port)."""
        argv = self._enc_argv(f"ts-{n}", n, w, h, fps, 2.0, debug, audio, piped)
        argv += ["-muxdelay", "0", "-muxpreload", "0", "-pat_period", "0.4",
                 "-f", "mpegts", f"tcp://127.0.0.1:{ts_port}"]
        return self._spawn_enc(f"ts-{n}", argv, piped)

    def start_rtsp_pub(self, n, w, h, rtsp_port, path="kilix", fps=15,
                       debug=False, audio=None, piped=False):
        """Publish into MediaMTX over loopback RTSP (E2: WebRTC tier). MediaMTX
        re-serves it as WHEP/WebRTC to browsers with sub-500 ms latency.
        WebRTC cannot carry the AAC used by HLS and MSE, so use Opus here.
        A one-second GOP bounds the wait for a new viewer's first keyframe."""
        argv = self._enc_argv(f"rtsp-{n}", n, w, h, fps, 1.0, debug, audio,
                              piped)
        if audio:
            codec = argv.index("-c:a")
            argv[codec + 1] = "libopus"
            bitrate = argv.index("-b:a")
            argv[bitrate + 1] = "96k"
        argv += ["-f", "rtsp", "-rtsp_transport", "tcp",
                 f"rtsp://127.0.0.1:{rtsp_port}/{path}"]
        return self._spawn_enc(f"rtsp-{n}", argv, piped)

    # ---- WebRTC (MediaMTX) ---------------------------------------------------
    MEDIAMTX_VERSION = "v1.9.3"
    # SHA-256 of the official release archive and its extracted executable.
    MEDIAMTX_RELEASES = {
        "x86_64": ("amd64",
                   "0b885dbfa4ef9c14cd00191c57d90d804255ff50403a28b85ceee7988c535b60",
                   "b59ecfdaa4ad9aad2281fda1ed46a73eaa0f08fb0d6271a287e87506fd9df2e7"),
        "aarch64": ("arm64v8",
                    "f2f02109dd3d88773d7de5ae84385d41041bc9d60d0459eb6c61462cb2da0d1e",
                    "367c142a06f1ade1014ec26957a73703a71e562302057632a13b6ea1ade02a72"),
        "armv7l": ("armv7",
                    "387a84bf47a0c3066d0159f84c8b9ea1da73a0233b4fc67f22731bc8001a6f74",
                    "045cbbed50dc60c8a3e4888c2ce4dd16687b1450f7921ddfbf6d8dbaf6b1b64d"),
    }

    def ensure_mediamtx(self):
        """Install and verify the pinned MediaMTX release in private storage."""
        release = self.MEDIAMTX_RELEASES.get(os.uname().machine)
        if release is None:
            raise RuntimeError(f"kilix: no MediaMTX build for {os.uname().machine}")
        arch, archive_hash, executable_hash = release
        d = os.path.join(_data_home(), "mediamtx")
        exe = os.path.join(d, "mediamtx")
        os.makedirs(d, mode=0o700, exist_ok=True)
        directory = os.lstat(d)
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid() or directory.st_mode & 0o077:
            raise RuntimeError("kilix: MediaMTX directory must be private and user-owned")
        try:
            installed = os.lstat(exe)
        except FileNotFoundError:
            installed = None
        if installed is not None:
            if (not stat.S_ISREG(installed.st_mode) or installed.st_uid != os.geteuid()
                    or installed.st_mode & 0o022 or not os.access(exe, os.X_OK)):
                raise RuntimeError("kilix: installed MediaMTX executable is unsafe")
            with open(exe, "rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != executable_hash:
                    raise RuntimeError("kilix: installed MediaMTX executable differs from pinned release")
            return exe
        url = ("https://github.com/bluenviron/mediamtx/releases/download/"
              f"{self.MEDIAMTX_VERSION}/mediamtx_{self.MEDIAMTX_VERSION}"
              f"_linux_{arch}.tar.gz")
        archive_fd, archive_path = tempfile.mkstemp(prefix=".mediamtx-archive-", dir=d)
        binary_path = None
        try:
            archive_digest = hashlib.sha256()
            size = 0
            with os.fdopen(archive_fd, "wb") as archive, urllib.request.urlopen(url, timeout=45) as response:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 32 * 1024 * 1024:
                        raise RuntimeError("kilix: MediaMTX archive exceeds 32 MiB")
                    archive_digest.update(chunk)
                    archive.write(chunk)
            if archive_digest.hexdigest() != archive_hash:
                raise RuntimeError("kilix: MediaMTX archive differs from pinned release")
            with tarfile.open(archive_path, "r:gz") as bundle:
                member = bundle.getmember("mediamtx")
                if not member.isfile() or member.size > 80 * 1024 * 1024:
                    raise RuntimeError("kilix: invalid MediaMTX executable in archive")
                binary_fd, binary_path = tempfile.mkstemp(prefix=".mediamtx-binary-", dir=d)
                binary_digest = hashlib.sha256()
                with os.fdopen(binary_fd, "wb") as output, bundle.extractfile(member) as source:
                    while chunk := source.read(1024 * 1024):
                        binary_digest.update(chunk)
                        output.write(chunk)
                    os.fchmod(output.fileno(), 0o700)
                if binary_digest.hexdigest() != executable_hash:
                    raise RuntimeError("kilix: MediaMTX executable differs from pinned release")
            os.replace(binary_path, exe)
            binary_path = None
        finally:
            os.unlink(archive_path)
            if binary_path is not None:
                os.unlink(binary_path)
        return exe

    def start_mediamtx(self, *, rtsp_port, webrtc_port, token, lan=False,
                       tls=None, path="kilix"):
        """MediaMTX with RTSP ingest on loopback and a WebRTC (WHEP + built-in
        web player) read side. Loopback publisher needs no credentials; readers
        authenticate as kilix/<token> (HTTP basic auth in the browser)."""
        if lan and not tls:
            raise RuntimeError("kilix: LAN WebRTC requires TLS")
        exe = self.ensure_mediamtx()
        addr = "" if lan else "127.0.0.1"
        conf = os.path.join(self.runtime_dir, "mediamtx.yml")
        udp_port = free_port()
        with _private_open(conf, "w") as f:
            f.write(f"""\
logLevel: warn
api: no
metrics: no
pprof: no
playback: no
rtsp: yes
rtspAddress: 127.0.0.1:{rtsp_port}
protocols: [tcp]
rtmp: no
hls: no
srt: no
webrtc: yes
webrtcAddress: {addr}:{webrtc_port}
webrtcEncryption: {'yes' if tls else 'no'}
webrtcServerKey: {json.dumps(tls[1]) if tls else "''"}
webrtcServerCert: {json.dumps(tls[0]) if tls else "''"}
webrtcLocalUDPAddress: :{udp_port}
authInternalUsers:
- user: any
  ips: ['127.0.0.1', '::1']
  permissions:
  - action: publish
- user: kilix
  pass: {token}
  permissions:
  - action: read
paths:
  {path}:
    source: publisher
""")
        logf = _private_open(os.path.join(self.runtime_dir, "mediamtx.log"), "wb")
        p = self.spawn("mediamtx", [exe, conf], stdout=logf, stderr=logf)
        if not wait_port(rtsp_port):
            raise RuntimeError("kilix: MediaMTX did not come up (see runtime log)")
        return p

    # ---- secrets ------------------------------------------------------------
    # Classic vncpasswd obfuscation key: the stored VNC password is the 8-byte
    # password DES-encrypted with this fixed key (d3des bit-reverses key bytes).
    _VNC_FIXED = bytes([23, 82, 107, 6, 35, 78, 88, 7])

    def _vnc_obfuscate(self, pw):
        p8 = pw.encode()[:8].ljust(8, b"\x00")
        key = bytes(int(f"{b:08b}"[::-1], 2) for b in self._VNC_FIXED).hex()
        for extra in (["-provider", "legacy", "-provider", "default"], []):
            r = subprocess.run(["openssl", "enc", "-des-ecb", "-e", "-K", key,
                                "-nopad"] + extra, input=p8, capture_output=True)
            if len(r.stdout) >= 8:
                return r.stdout[:8]
        raise RuntimeError("kilix: openssl des-ecb unavailable for VNC password")

    def make_vncpw(self, full, view):
        """Two-entry VNC passwd file (0600): entry 1 = full control, entry 2 =
        view-only. Generated with openssl — byte-identical to `vncpasswd` — so no
        vncpasswd binary is needed; works on TightVNC and TigerVNC hosts alike.
        A client authenticating with the `view` password gets a server-enforced
        view-only session."""
        data = self._vnc_obfuscate(full) + self._vnc_obfuscate(view)
        path = os.path.join(self.runtime_dir, "vncpw")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path

    def mint_token(self):
        return secrets.token_urlsafe(32)

    def tls_cert(self):
        """First-use self-signed cert for the --lan HTTPS bridge.
        Returns (cert_path, key_path, sha256_fingerprint)."""
        d = os.path.join(_data_home(), "tls")
        os.makedirs(d, mode=0o700, exist_ok=True)
        cert = os.path.join(d, "cert.pem")
        key = os.path.join(d, "key.pem")
        if not (os.path.exists(cert) and os.path.exists(key)):
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                            "-nodes", "-keyout", key, "-out", cert,
                            "-days", "3650", "-subj", "/CN=kilix"],
                           check=True, capture_output=True)
            os.chmod(key, 0o600)
        fp = subprocess.run(["openssl", "x509", "-in", cert, "-noout",
                             "-fingerprint", "-sha256"],
                            capture_output=True, text=True).stdout.strip()
        return cert, key, fp

    # ---- browser / HLS bridge ----------------------------------------------
    def ensure_novnc(self):
        d = os.path.join(_data_home(), "novnc")
        if not os.path.exists(os.path.join(d, "vnc.html")):
            os.makedirs(os.path.dirname(d), exist_ok=True)
            subprocess.run(["git", "clone", "--depth", "1", "-b", "v1.5.0",
                            "https://github.com/novnc/noVNC", d],
                           check=True, capture_output=True)
        return d

    def ensure_hlsjs(self):
        d = os.path.join(_data_home(), "hlsjs")
        f = os.path.join(d, "hls.min.js")
        if not os.path.exists(f):
            os.makedirs(d, exist_ok=True)
            subprocess.run(["curl", "-fsSL",
                            "https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js",
                            "-o", f], check=True, capture_output=True)
        return d

    def ensure_mpegtsjs(self):
        d = os.path.join(_data_home(), "mpegtsjs")
        f = os.path.join(d, "mpegts.js")
        if not os.path.exists(f):
            os.makedirs(d, exist_ok=True)
            subprocess.run(["curl", "-fsSL",
                            "https://cdn.jsdelivr.net/npm/mpegts.js@1.7.3/dist/mpegts.js",
                            "-o", f], check=True, capture_output=True)
        return d

    def start_bridge(self, *, rfb_port=None, http_port, token, hlsdir=None,
                     ts_port=None, tls=None, lan=False, what="session"):
        """Spawn config/wsbridge.py. Token goes via the env (KILIX_BRIDGE_TOKEN),
        never argv, so it is not visible in `ps`. rfb_port may be None (HLS/TS
        broadcast without a VNC control tier); ts_port makes the bridge listen
        there for the MPEG-TS encoder feed and serve it at /ts + /watch."""
        argv = ["python3",
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "wsbridge.py"),
                "--http-port", str(http_port),
                "--host", "0.0.0.0" if lan else "127.0.0.1",
                "--novnc", self.ensure_novnc(),
                "--hlsjs", self.ensure_hlsjs(), "--what", what]
        if rfb_port:
            argv += ["--rfb-port", str(rfb_port)]
        if hlsdir:
            argv += ["--hls", hlsdir]
        if ts_port:
            argv += ["--ts-port", str(ts_port),
                     "--mpegtsjs", self.ensure_mpegtsjs()]
        if tls:
            argv += ["--tls-cert", tls[0], "--tls-key", tls[1]]
        env = dict(os.environ, KILIX_BRIDGE_TOKEN=token)
        logf = _private_open(os.path.join(self.runtime_dir, "bridge.log"), "wb")
        return self.spawn("bridge", argv, stdout=logf, stderr=logf, env=env)

    # ---- connect instructions ----------------------------------------------
    def print_connect(self, *, what="app", rfb_port=None, http_port=None,
                       full_pw=None, view_pw=None, token=None,
                       lan_host=None, tls_fp=None, hls_path="hls/live.m3u8",
                       have_hls=True, have_ts=False, webrtc_port=None):
        user, host = _whoami(), _hostname()
        # flush every line: a serve process then blocks forever, so buffered
        # connect instructions (e.g. when stdout is a pipe) would never appear.
        w = lambda *a: print(*a, flush=True)
        w(f"\n\x1b[1mkilix serve — {what} is streaming\x1b[0m  (Ctrl+C to stop)")
        w("  Loopback-only by default; reach it from another device over SSH:")
        if rfb_port:
            w(f"   native VNC : ssh -N -L {rfb_port}:127.0.0.1:{rfb_port} {user}@{host}")
            w(f"                then open a VNC viewer on  localhost:{rfb_port}")
            if full_pw:
                w(f"                control password  : {full_pw}")
            if view_pw:
                w(f"                view-only password : {view_pw}")
        if http_port:
            q = f"?t={token}" if token else ""
            scheme = "https" if tls_fp else "http"
            w(f"   browser    : ssh -N -L {http_port}:127.0.0.1:{http_port} {user}@{host}")
            w(f"                then open  {scheme}://localhost:{http_port}/{q}")
            if have_ts:
                w(f"   low-latency: {scheme}://localhost:{http_port}/watch{q}   (~0.3-1 s)")
            if have_hls:
                w(f"   view (mpv) : mpv {scheme}://localhost:{http_port}/{hls_path}{q}")
        if webrtc_port:
            scheme = "https" if tls_fp else "http"
            w(f"   WebRTC     : ssh -N -L {webrtc_port}:127.0.0.1:{webrtc_port} {user}@{host}")
            w(f"                then open  {scheme}://localhost:{webrtc_port}/kilix"
              f"   (user kilix, password = token below)")
            if token:
                w(f"                token: {token}")
        if lan_host and http_port:
            q = f"?t={token}" if token else ""
            w(f"   LAN (TLS)  : https://{lan_host}:{http_port}/{q}")
            if tls_fp:
                w(f"                accept cert  {tls_fp}")
        if lan_host and webrtc_port:
            w(f"   LAN WebRTC : https://{lan_host}:{webrtc_port}/kilix")
            if tls_fp:
                w(f"                accept cert  {tls_fp}")
        w("")
