"""kilix 95 — UI sound engine + scheme model.

Original synthesized Win95-style cues; NO external assets. Short mono .wav
files generated in pure Python (wave + math + struct), cached under
the Kilix private data directory and regenerated when missing. A *scheme* maps each
system event to a sound file (any format) or silence, defaulting to the
built-in cue; the active scheme persists to sounds/scheme.json, named schemes
to sounds/schemes/<name>.json. Playback is fire-and-forget through a detached
CLI player (WAV via paplay/aplay, other formats via ffplay/mpv/cvlc), so it
never blocks the event loop and never raises when no player exists.
"""
import json
import math
import os
import random
import re
import shutil
import struct
import subprocess
import threading
import wave

import storage

RATE = 44100
SYNTH_VERSION = 2          # bump when a synth changes so cached wavs regenerate
_PLAYERS = ("paplay", "aplay", "ffplay", "play")     # WAV players
_MEDIA_PLAYERS = ("ffplay", "mpv", "cvlc")           # non-WAV (kilix-amp formats)
_AUDIO_EXT = (".wav", ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a",
              ".aac", ".aiff", ".aif", ".aifc", ".wma")

# equal-temperament reference pitches (Hz)
C5, D5, E5, F5, G5, A5, B5 = 523.25, 587.33, 659.25, 698.46, 784.0, 880.0, 987.77
C6, E6, G6 = 1046.5, 1318.5, 1568.0
C4, E4, G4 = 261.63, 329.63, 392.0


def _data_dir():
    return storage.data_dir("desktop-sounds")


# ── synthesis primitives ────────────────────────────────────────────────────
def _osc(freq, i, kind):
    ph = 2 * math.pi * freq * (i / RATE)
    if kind == "square":
        return 1.0 if math.sin(ph) >= 0 else -1.0
    if kind == "tri":
        return (2.0 / math.pi) * math.asin(math.sin(ph))
    return math.sin(ph)


def _env(i, n, a, r):
    at = max(1, int(a * RATE))
    rt = max(1, int(r * RATE))
    if i < at:
        return i / at
    if i > n - rt:
        return max(0.0, (n - i) / rt)
    return 1.0


def _note(buf, freq, start, dur, amp=0.3, kind="sine", a=0.008, r=0.12):
    n = int(dur * RATE)
    s = int(start * RATE)
    for i in range(n):
        j = s + i
        if 0 <= j < len(buf):
            buf[j] += amp * _env(i, n, a, r) * _osc(freq, i, kind)


def _glide(buf, f0, f1, start, dur, amp=0.25):
    n = int(dur * RATE)
    s = int(start * RATE)
    ph = 0.0
    for i in range(n):
        f = f0 + (f1 - f0) * (i / n)
        ph += 2 * math.pi * f / RATE
        j = s + i
        if 0 <= j < len(buf):
            buf[j] += amp * _env(i, n, 0.004, 0.03) * math.sin(ph)


def _blank(dur):
    return [0.0] * int(dur * RATE)


def _click(cut, dur=0.035, amp=0.6, decay=0.006, seed=95):
    """A short filtered-noise transient — a soft mechanical tick, not a tone.
    Higher cut = brighter click; faster decay = crisper."""
    n = int(dur * RATE)
    b = [0.0] * n
    rnd = random.Random(seed)
    prev = 0.0
    for i in range(n):
        prev += cut * (rnd.uniform(-1.0, 1.0) - prev)        # one-pole low-pass
        b[i] = amp * math.exp(-i / (decay * RATE)) * prev    # fast percussive tail
    return b


# a few overtones give a warm, slightly inharmonic bell rather than a flat tone
_BELL_PARTIALS = ((1.0, 1.0), (2.0, 0.45), (2.77, 0.22), (3.9, 0.10))


def _bell(buf, freq, start, dur, amp=0.18):
    n = int(dur * RATE)
    s = int(start * RATE)
    at = max(1, int(0.006 * RATE))
    for i in range(n):
        j = s + i
        if not (0 <= j < len(buf)):
            continue
        env = min(1.0, i / at) * math.exp(-i / (dur * 0.5 * RATE))
        t = i / RATE
        v = sum(a * math.sin(2 * math.pi * freq * m * t) for m, a in _BELL_PARTIALS)
        buf[j] += amp * env * v


def _pad(buf, freq, start, dur, amp=0.1):
    """A soft-attack sustained tone that swells in and fades slowly."""
    _note(buf, freq, start, dur, amp=amp, kind="tri", a=0.16, r=0.5)


# ── the sounds ───────────────────────────────────────────────────────────────
def _startup():
    # a warm, welcoming boot chime: a soft major pad swells in under bells that
    # rise and resolve up to the octave
    b = _blank(1.9)
    for f in (C4, E4, G4, C5):                            # pad chord swells in
        _pad(b, f, 0.0, 1.75, amp=0.085)
    for f, t in ((G4, 0.10), (C5, 0.34), (E5, 0.56),     # bells ascend, resolving
                 (G5, 0.78), (C6, 1.00)):
        _bell(b, f, t, 0.8, amp=0.16)
    return b


def _shutdown():
    # the companion: the same warmth settling down and resolving low
    b = _blank(1.7)
    for f in (C5, G4, E4, C4):                            # pad chord settles in
        _pad(b, f, 0.05, 1.5, amp=0.085)
    for f, t in ((G5, 0.10), (E5, 0.32), (C5, 0.54),     # bells descend, resolving
                 (G4, 0.76), (C4, 0.98)):
        _bell(b, f, t, 0.8, amp=0.15)
    return b


def _error():
    # soft, serious minor-third fall — rounded sine, not a harsh buzz
    b = _blank(0.64)
    _note(b, 196.0, 0.0, 0.26, amp=0.26, kind="sine", a=0.012, r=0.16)
    _note(b, 155.56, 0.24, 0.36, amp=0.26, kind="sine", a=0.012, r=0.22)
    _note(b, 392.0, 0.0, 0.2, amp=0.04, kind="sine", a=0.012, r=0.16)   # faint body
    return b


def _exclamation():
    b = _blank(0.46)                                     # gentle two-tone alert
    _note(b, E5, 0.0, 0.16, amp=0.2, kind="sine", a=0.008, r=0.1)
    _note(b, A5, 0.15, 0.26, amp=0.2, kind="sine", a=0.008, r=0.16)
    return b


def _asterisk():
    b = _blank(0.5)                                      # soft ding + octave
    _note(b, A5, 0.0, 0.46, amp=0.2, kind="sine", a=0.008, r=0.4)
    _note(b, A5 * 2, 0.0, 0.3, amp=0.055, kind="sine", a=0.008, r=0.3)
    return b


def _question():
    b = _blank(0.46)                                     # gentle rising two-tone
    _note(b, D5, 0.0, 0.15, amp=0.2, kind="sine", a=0.008, r=0.1)
    _note(b, G5, 0.15, 0.26, amp=0.2, kind="sine", a=0.008, r=0.16)
    return b


def _minimize():
    b = _blank(0.14)
    _glide(b, A5, D5, 0.0, 0.12, amp=0.22)               # quick descending blip
    return b


def _maximize():
    b = _blank(0.14)
    _glide(b, D5, A5, 0.0, 0.12, amp=0.22)               # quick ascending blip
    return b


def _restore():
    b = _blank(0.12)
    _note(b, E5, 0.0, 0.09, amp=0.2, kind="tri", r=0.05)
    return b


def _open():
    return _click(0.55, dur=0.03, amp=0.6, decay=0.005, seed=11)    # bright tick


def _close():
    return _click(0.34, dur=0.045, amp=0.6, decay=0.008, seed=23)   # softer tock


def _recycle_empty():
    dur = 0.5                                             # filtered-noise whoosh
    n = int(dur * RATE)
    b = [0.0] * n
    rnd = random.Random(95)
    prev = 0.0
    for i in range(n):
        p = i / n
        cut = 0.02 + 0.25 * math.sin(math.pi * p)        # sweeping one-pole LP
        prev += cut * (rnd.uniform(-1.0, 1.0) - prev)
        b[i] = 0.55 * math.sin(math.pi * p) * prev        # bell-shaped envelope
    return b


_GEN = {
    "startup": _startup, "shutdown": _shutdown, "error": _error,
    "exclamation": _exclamation, "asterisk": _asterisk, "question": _question,
    "minimize": _minimize, "maximize": _maximize, "restore": _restore,
    "open": _open, "close": _close, "recycle_empty": _recycle_empty,
}


def names():
    return list(_GEN)


# ── cache (generate .wav files, regenerate if missing) ───────────────────────
def path_for(name):
    return os.path.join(_data_dir(), name + ".wav")


def _valid(path):
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() > 0
    except Exception:
        return False


def _write(path, samples):
    peak = max((abs(s) for s in samples), default=0.0)
    scale = (0.89 / peak) if peak > 0 else 1.0           # normalize loudness
    frames = bytearray()
    for s in samples:
        v = int(max(-1.0, min(1.0, s * scale)) * 32767)
        frames += struct.pack("<h", v)
    tmp = path + ".tmp"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))
    os.replace(tmp, path)                                # atomic swap


_reconciled = False


def _reconcile_version():
    """Once per process: if the cached wavs were made by an older synth, drop
    them so ensure() rebuilds them with the current code."""
    global _reconciled
    if _reconciled:
        return
    _reconciled = True
    stamp = os.path.join(_data_dir(), ".synth-version")
    try:
        with open(stamp) as f:
            if f.read().strip() == str(SYNTH_VERSION):
                return
    except OSError:
        pass
    for n in _GEN:                                       # stale/absent stamp: wipe
        try:
            os.unlink(path_for(n))
        except OSError:
            pass
    try:
        os.makedirs(_data_dir(), exist_ok=True)
        with open(stamp, "w") as f:
            f.write(str(SYNTH_VERSION))
    except OSError:
        pass


def ensure(name):
    """Return the cached wav path for name, generating it if missing/invalid;
    None if name is unknown or generation fails."""
    if name not in _GEN:
        return None
    _reconcile_version()
    p = path_for(name)
    if _valid(p):
        return p
    try:
        os.makedirs(_data_dir(), exist_ok=True)
        _write(p, _GEN[name]())
    except OSError:
        return None
    return p


def ensure_all():
    """Generate every sound; return {name: path or None}."""
    return {n: ensure(n) for n in _GEN}


_warmed = False


def warm():
    """Pre-synthesize every wav off-thread so no first-play() ever blocks the
    loop (one-time; the files persist). No-op after the first call."""
    global _warmed
    if _warmed:
        return
    _warmed = True
    threading.Thread(target=ensure_all, daemon=True).start()


# ── event registry & scheme model ────────────────────────────────────────────
_EVENTS = [
    ("startup", "Startup"), ("shutdown", "Shutdown"),
    ("error", "Critical Stop"), ("exclamation", "Exclamation"),
    ("asterisk", "Default Beep"), ("question", "Question"),
    ("minimize", "Minimize"), ("maximize", "Maximize"),
    ("restore", "Restore"), ("open", "Open program"),
    ("close", "Close program"), ("recycle_empty", "Empty Recycle Bin"),
]
_EVENT_IDS = {eid for eid, _ in _EVENTS}

DEFAULT_SCHEME = "kilix 95"                              # built-in cue per event
NO_SOUNDS = "No Sounds"                                  # all silent

# Events with NO cue by default — silent unless the user assigns one in
# Settings ▸ Sounds. (Minimize firing on every window minimize got noisy.)
DEFAULT_SILENT = {"minimize"}

_active = None                                           # dict: event_id -> path|None (overrides only)
_active_name = DEFAULT_SCHEME


def events(generate=True):
    """[(event_id, human label, default built-in wav path)] in display order.
    generate=False lists cue paths without synthesizing (for UI enumeration)."""
    return [(eid, label, ensure(eid) if generate else path_for(eid))
            for eid, label in _EVENTS]


def _scheme_path():
    return os.path.join(_data_dir(), "scheme.json")


def _schemes_dir():
    return os.path.join(_data_dir(), "schemes")


def _named_path(name):
    safe = re.sub(r"[^\w .-]", "_", name).strip() or "scheme"
    return os.path.join(_schemes_dir(), safe + ".json")


def _sanitize(mapping):
    """Keep only known event ids mapped to a str path or None."""
    out = {}
    if isinstance(mapping, dict):
        for eid in _EVENT_IDS:
            if eid in mapping:
                v = mapping[eid]
                out[eid] = v if (v is None or isinstance(v, str)) else None
    return out


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)                                # atomic swap


def _ensure_loaded():
    """Load the active scheme once; tolerate a missing/corrupt file."""
    global _active, _active_name
    if _active is not None:
        return
    try:
        with open(_scheme_path()) as f:
            data = json.load(f)
        _active = _sanitize(data.get("sounds", {}))
        _active_name = data.get("name") or DEFAULT_SCHEME
    except Exception:
        _active, _active_name = {}, DEFAULT_SCHEME       # regenerate default


def _save_active():
    try:
        _write_json(_scheme_path(), {"name": _active_name, "sounds": _active})
    except OSError:
        pass


def current_scheme():
    """The active scheme's overrides {event_id: path|None}; empty == default."""
    _ensure_loaded()
    return dict(_active)


def set_sound(event_id, path_or_None):
    """Bind one event to a file path (any format) or None for silence."""
    _ensure_loaded()
    if event_id in _EVENT_IDS:
        _active[event_id] = path_or_None
        _save_active()


def scheme_names():
    """Built-in schemes plus every saved named scheme."""
    out = [DEFAULT_SCHEME, NO_SOUNDS]
    try:
        for fn in sorted(os.listdir(_schemes_dir())):
            if fn.endswith(".json"):
                nm = fn[:-5]
                if nm not in out:
                    out.append(nm)
    except OSError:
        pass
    return out


def scheme_overrides(name):
    """The overrides {event_id: path|None} a scheme defines, WITHOUT making it
    active or persisting anything (for deferred/working-copy editing)."""
    if name == DEFAULT_SCHEME:
        return {}
    if name == NO_SOUNDS:
        return {eid: None for eid in _EVENT_IDS}
    try:
        with open(_named_path(name)) as f:
            return _sanitize(json.load(f).get("sounds", {}))
    except Exception:
        return {}


def load_scheme(name):
    """Make `name` the active scheme (built-in or saved); persist it. A
    missing/corrupt named scheme falls back to the default. Returns overrides."""
    global _active, _active_name
    _active = scheme_overrides(name)
    _active_name = name
    _save_active()
    return dict(_active)


def save_scheme_as(name):
    """Persist the active scheme under `name`; return its file path."""
    _ensure_loaded()
    global _active_name
    p = _named_path(name)
    try:
        _write_json(p, {"name": name, "sounds": _active})
        _active_name = name
        _save_active()
    except OSError:
        pass
    return p


def reset():
    """Restore the built-in 'kilix 95' default scheme."""
    return load_scheme(DEFAULT_SCHEME)


def library_sounds(generate=True):
    """Candidate sound files: the built-in cues plus any audio files found in
    the shared library dir and the user's ~/audio_clips. generate=False lists
    the cue paths without synthesizing them on the calling thread."""
    seen, out = set(), []
    cues = ensure_all() if generate else {n: path_for(n) for n in _GEN}
    for _, p in sorted(cues.items()):                    # synthesized cues first
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    for d in (_data_dir(), os.path.join(os.path.expanduser("~"), "audio_clips")):
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in entries:
            if fn.lower().endswith(_AUDIO_EXT):
                p = os.path.join(d, fn)
                if p not in seen and os.path.isfile(p):
                    seen.add(p)
                    out.append(p)
    return out


# ── playback (fire-and-forget, never blocks, never raises) ───────────────────
def _which(cands):
    for name in cands:
        exe = shutil.which(name)
        if exe:
            return exe
    return None


def player():
    """Absolute path of the first available WAV player command, or None."""
    return _which(_PLAYERS)


def _player_for(path):
    """WAV → paplay/aplay/…; any other format → ffplay/mpv/cvlc (amp formats)."""
    if os.path.splitext(path)[1].lower() == ".wav":
        return _which(_PLAYERS)
    return _which(_MEDIA_PLAYERS)


def _argv(exe, path, volume):
    base = os.path.basename(exe)
    vol = max(0, min(100, int(volume)))
    if base == "ffplay":
        return [exe, "-nodisp", "-autoexit", "-loglevel", "quiet",
                "-volume", str(vol), path]
    if base == "mpv":
        return [exe, "--no-video", "--really-quiet", "--volume=%d" % vol, path]
    if base in ("cvlc", "vlc"):
        return [exe, "--intf", "dummy", "--no-video", "--play-and-exit",
                "--quiet", "--gain", "%.3f" % (vol / 100.0), path]
    if base == "paplay":
        return [exe, "--volume=%d" % int(vol * 655.36), path]
    if base == "play":                                   # sox
        return [exe, "-q", path, "vol", "%.3f" % (vol / 100.0)]
    return [exe, path]                                   # aplay: no volume flag


def _play_file(path, volume, muted):
    """Spawn a detached player for an already-resolved file. Honors mute/level
    and KILIX_NO_SOUND=1; no-op for silence (None); never raises."""
    if path is None:
        return False
    if muted or int(volume) <= 0:
        return False
    if os.environ.get("KILIX_NO_SOUND") == "1":
        return False
    if not os.path.isfile(path):
        return False
    exe = _player_for(path)
    if exe is None:
        return False
    try:
        subprocess.Popen(_argv(exe, path, volume),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        return False
    return True


def _resolve(event_id):
    """The active scheme's file for an event, or the built-in cue if unset."""
    _ensure_loaded()
    if event_id in _active:
        return _active[event_id]                         # path or None (silent)
    if event_id in DEFAULT_SILENT:
        return None                                      # off by default (assignable)
    return ensure(event_id)                              # default built-in wav


def play(event_id, volume=100, muted=False):
    """Play the sound bound to a system event through the active scheme,
    detached. Returns True iff a player was spawned; never raises."""
    return _play_file(_resolve(event_id), volume, muted)


def preview(path, volume=100, muted=False):
    """Audition an arbitrary file the same way play() does."""
    return _play_file(path, volume, muted)
