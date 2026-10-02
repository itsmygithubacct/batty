"""Shared GPU Terminal settings used by Kilix and its companion projects.

The file is deliberately a small ``KEY=value`` document rather than a shell
fragment.  Kilix, Kilix 95, Pleb, and Plebian-OS can therefore share settings
without executing user-controlled configuration as code.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import os
import re
import stat
import tempfile
from typing import Mapping


SETTINGS_BASENAME = "settings.conf"
SETTINGS_HEADER = "# GPU Terminal shared settings (KEY=value; not shell code)."
SETTINGS_MAX_BYTES = 1024 * 1024
SETTINGS_MARKER = "# -- Kilix clickable chrome --"
SESSION_LOG_MARKER = "# -- Kilix session logging --"
VOICE_MARKER = "# -- Kilix voice --"
GAMES_MARKER = "# -- Kilix game availability --"
CODING_MARKER = "# -- Kilix coding agents --"


@dataclass(frozen=True)
class ToggleSpec:
    key: str
    label: str
    section: str
    default: bool = True


TOP_BAR_TOGGLES = (
    ToggleSpec("KILIX_CHROME_TEMPERATURE", "Thermal status", "Top bar", default=False),
    ToggleSpec("KILIX_CHROME_VOLUME", "Volume", "Top bar"),
    ToggleSpec("KILIX_CHROME_NETWORK", "Network / Wi-Fi", "Top bar"),
    ToggleSpec("KILIX_CHROME_CALENDAR", "Calendar", "Top bar"),
    ToggleSpec("KILIX_CHROME_CLOCK", "Date and time", "Top bar"),
    ToggleSpec("KILIX_CHROME_BATTERY", "Battery", "Top bar"),
    # Only takes effect in a Pleb session, where the tab bar is the desktop's
    # only taskbar. An ordinary desktop already has a panel of its own.
    ToggleSpec("KILIX_CHROME_WINDOWS", "Native window taskbar (Pleb)", "Top bar"),
    ToggleSpec("KILIX_CHROME_SPEAK", "Read pane aloud", "Top bar"),
    ToggleSpec("KILIX_CHROME_DICTATE", "Dictate to pane", "Top bar"),
    ToggleSpec("KILIX_CHROME_START_MENU", "Start menu", "Top bar", default=False),
)

PANE_BUTTON_TOGGLES = (
    ToggleSpec("KILIX_CHROME_BUTTON_SYNCHRONIZE_INPUT", "Synchronize keyboard input", "Pane buttons"),
    ToggleSpec("KILIX_CHROME_BUTTON_FONT_INCREASE", "Increase text size", "Pane buttons"),
    ToggleSpec("KILIX_CHROME_BUTTON_FONT_DECREASE", "Decrease text size", "Pane buttons"),
    ToggleSpec("KILIX_CHROME_BUTTON_SPLIT_LEFT", "Split pane left", "Pane buttons"),
    ToggleSpec("KILIX_CHROME_BUTTON_SPLIT_UP", "Split pane up", "Pane buttons"),
    ToggleSpec("KILIX_CHROME_BUTTON_SPLIT_DOWN", "Split pane down", "Pane buttons"),
    ToggleSpec("KILIX_CHROME_BUTTON_SPLIT_RIGHT", "Split pane right", "Pane buttons"),
    ToggleSpec("KILIX_CHROME_BUTTON_MAXIMIZE", "Maximize / restore pane", "Pane buttons"),
    ToggleSpec("KILIX_CHROME_BUTTON_CLOSE", "Close pane", "Pane buttons"),
)

# Session logging is on by default: the value of a transcript is that it was
# already running when something went wrong.  Logs are private to the user,
# bounded, and removable from every settings interface.
SESSION_LOG_TOGGLES = (
    ToggleSpec("KILIX_TRANSCRIPT", "Record pane session logs", "Session logging"),
)

# The two chrome toggles above make the widgets visible; these settings govern
# what they do once clicked.  Both widgets ship on, which means the buttons are
# present, not that anything is listening: capture opens on a click and on
# nothing else.
VOICE_PUNCTUATION_KEY = "KILIX_VOICE_STT_PUNCTUATION"
VOICE_TOGGLES = (
    ToggleSpec(VOICE_PUNCTUATION_KEY, "Spoken punctuation", "Voice"),
)

# Stable IDs cover the two built-in Kilix 95 games and every game-like entry
# in the pinned host content catalog.  Keeping these in the host SDK gives the
# CLI, TUI, built-in desktop, and external Kilix 95 provider one vocabulary.
GAME_TOGGLE_IDS = (
    ("minesweeper", "Minesweeper"),
    ("solitaire", "Solitaire"),
    ("doom", "Doom"),
    ("dosbox", "DOSBox"),
    ("bashed-earth", "Bashed Earth"),
    ("kilix-jpak", "Kilix JPAK"),
    ("kilix-rancher", "Kilix Rancher"),
    ("kilix-pong", "Kilix Pong"),
    ("kilix-lights", "Kilix Lights"),
    ("super-kilix", "Super Kilix"),
    ("joustix", "Joustix"),
    ("chess-bash", "Chess Bash"),
    ("kilix-fishtank", "Kilix Fishtank"),
    ("terminal-lander", "Terminal Lander"),
    ("kitty-brokeout", "Kitty Brokeout"),
    ("kilix-land", "Kilix Land"),
)


def _game_setting_key(game_id: str) -> str:
    return "KILIX_GAME_" + game_id.upper().replace("-", "_")


GAME_TOGGLES = tuple(
    ToggleSpec(_game_setting_key(game_id), label, "Games")
    for game_id, label in GAME_TOGGLE_IDS
)
GAME_KEY_BY_ID = {
    game_id: spec.key
    for (game_id, _label), spec in zip(GAME_TOGGLE_IDS, GAME_TOGGLES)
}
GAME_ID_BY_KEY = {key: game_id for game_id, key in GAME_KEY_BY_ID.items()}

# Concatenation order is the section order every interface presents, and
# ``kilix-settings`` derives its numeric ``--section`` values from it.  Voice
# goes after session logging so the earlier numbers keep meaning what they did.
TOGGLE_SPECS = (
    TOP_BAR_TOGGLES + PANE_BUTTON_TOGGLES + SESSION_LOG_TOGGLES
    + VOICE_TOGGLES + GAME_TOGGLES
)
TOGGLE_BY_KEY = {spec.key: spec for spec in TOGGLE_SPECS}
CLOCK_FORMAT_KEY = "KILIX_CHROME_CLOCK_FORMAT"
CLOCK_FORMAT_DEFAULT = "%Y-%m-%d %H:%M"
PANE_CPU_MODE_KEY = "KILIX_CHROME_PANE_CPU_MODE"
PANE_CPU_MODE_DEFAULT = "auto"
PANE_CPU_MODE_CHOICES = ("auto", "always", "off")
PANE_MEMORY_MODE_KEY = "KILIX_CHROME_PANE_MEMORY_MODE"
PANE_MEMORY_MODE_DEFAULT = "auto"
PANE_MEMORY_MODE_CHOICES = ("auto", "always", "off")
TAB_BAR_EDGE_KEY = "KILIX_CHROME_TAB_BAR_EDGE"
TAB_BAR_EDGE_DEFAULT = "top"
TAB_BAR_EDGE_CHOICES = ("top", "bottom")

# Kitty ships images as APC sequences whose payload is base64 pixel data, so a
# pane running the desktop, a browser, or icat can emit megabytes per second.
# ``elide`` records a byte-count marker instead, keeping the transcript a
# readable record of text; ``keep`` captures the stream verbatim.
TRANSCRIPT_GRAPHICS_KEY = "KILIX_TRANSCRIPT_GRAPHICS"
TRANSCRIPT_GRAPHICS_DEFAULT = "elide"
TRANSCRIPT_GRAPHICS_CHOICES = ("elide", "keep")

# Presets rather than a free-form number: every settings interface shares one
# vocabulary, and an unrecognised value would silently read back as the default.
# Stored as human tokens so a dropdown, a TUI cycle, and the file itself all
# read the same way; only the broker needs the byte count.
TRANSCRIPT_LIMIT_KEY = "KILIX_TRANSCRIPT_MAX_SIZE"
TRANSCRIPT_LIMIT_DEFAULT = "8M"
TRANSCRIPT_LIMIT_CHOICES = ("2M", "8M", "32M", "128M")

# The per-pane cap above bounds one file; these bound the directory. Without
# them a long-running kiosk grows without limit, because panes come and go and
# nothing reclaims a dead pane's log.
#
# Every stored transcript is compressed. A pane's log is written plain while the
# pane lives — the broker appends to it and drops the oldest bytes on overflow,
# neither of which works on a compressed stream — and is compressed with
# zstd -3 after the pane dies. Terminal output stores at roughly a
# sixtieth of its size at that level, and decompression is about half a second
# per 400 MB, so reading a recent transcript is effectively free. The 5 GiB
# default plus the 1 GiB archive below stays proportionate to Plebian-OS's
# supported 20 GiB minimum disk instead of letting logs claim more than it.
TRANSCRIPT_TOTAL_KEY = "KILIX_TRANSCRIPT_MAX_TOTAL"
TRANSCRIPT_TOTAL_DEFAULT = "5G"
TRANSCRIPT_TOTAL_CHOICES = ("1G", "5G", "10G", "20G", "50G", "100G")

# Past that budget the oldest are recompressed at zstd -9, which reaches about
# 116x for roughly three seconds of CPU per 400 MB. Level costs compression
# time, not recall: -9 and -19 both decompress in well under a second, the same
# as -3, so a denser older tier is not a slower one. ``off`` deletes instead of
# recompressing, for operators who want a hard ceiling and no history.
TRANSCRIPT_ARCHIVE_KEY = "KILIX_TRANSCRIPT_ARCHIVE_MAX_TOTAL"
TRANSCRIPT_ARCHIVE_DEFAULT = "1G"
TRANSCRIPT_ARCHIVE_CHOICES = ("off", "1G", "5G", "10G", "20G", "50G", "100G")

# Read-aloud and dictation, presets for the same reason the transcript limit is
# one: the shared file, the CLI, the settings TUI, and Kilix 95 all offer the
# same vocabulary, and a value none of them recognises would read back as the
# default without saying so.
VOICE_TTS_ENGINE_KEY = "KILIX_VOICE_TTS_ENGINE"
VOICE_TTS_ENGINE_DEFAULT = "espeak"
VOICE_TTS_ENGINE_CHOICES = ("espeak", "mbrola", "off")
VOICE_TTS_VOICE_KEY = "KILIX_VOICE_TTS_VOICE"
VOICE_TTS_VOICE_DEFAULT = "en-us"
VOICE_TTS_RATE_KEY = "KILIX_VOICE_TTS_RATE"
VOICE_TTS_RATE_DEFAULT = "170"
VOICE_TTS_RATE_CHOICES = ("120", "150", "170", "200", "240")
VOICE_TTS_EXTENT_KEY = "KILIX_VOICE_TTS_EXTENT"
VOICE_TTS_EXTENT_DEFAULT = "screen"
VOICE_TTS_EXTENT_CHOICES = ("screen", "scrollback", "selection")
VOICE_TTS_MAX_CHARS_KEY = "KILIX_VOICE_TTS_MAX_CHARS"
VOICE_TTS_MAX_CHARS_DEFAULT = "4000"
VOICE_TTS_MAX_CHARS_CHOICES = ("1000", "4000", "16000", "unlimited")
VOICE_STT_ENGINE_KEY = "KILIX_VOICE_STT_ENGINE"
VOICE_STT_ENGINE_DEFAULT = "vosk"
VOICE_STT_ENGINE_CHOICES = ("vosk", "vibevoice", "off")
VOICE_STT_MODEL_KEY = "KILIX_VOICE_STT_MODEL"
VOICE_STT_MODEL_DEFAULT = "small-en-us"
VOICE_STT_MODEL_CHOICES = (
    "small-en-us", "lgraph-en-us", "vibevoice-asr-bitnet")
VOICE_STT_MODEL_ENGINES = {
    "small-en-us": "vosk",
    "lgraph-en-us": "vosk",
    "vibevoice-asr-bitnet": "vibevoice",
}

# There is no ``always``, and adding one is not a small change.  Dictation that
# presses Enter on its own behalf turns a misrecognition into an arbitrary
# command, so the choice is between never submitting and asking first.
VOICE_STT_SUBMIT_KEY = "KILIX_VOICE_STT_SUBMIT"
VOICE_STT_SUBMIT_DEFAULT = "never"
VOICE_STT_SUBMIT_CHOICES = ("never", "confirm")

VOICE_STT_MAX_SECONDS_KEY = "KILIX_VOICE_STT_MAX_SECONDS"
VOICE_STT_MAX_SECONDS_DEFAULT = "30"
VOICE_STT_MAX_SECONDS_CHOICES = ("15", "30", "60", "120")
VOICE_STT_SILENCE_MS_KEY = "KILIX_VOICE_STT_SILENCE_MS"
VOICE_STT_SILENCE_MS_DEFAULT = "900"
VOICE_STT_SILENCE_MS_CHOICES = ("500", "900", "1500")
VOICE_DEVICE_IN_KEY = "KILIX_VOICE_DEVICE_IN"
VOICE_DEVICE_IN_DEFAULT = "default"
VOICE_DEVICE_OUT_KEY = "KILIX_VOICE_DEVICE_OUT"
VOICE_DEVICE_OUT_DEFAULT = "default"

# Off by default, unlike session logging: a record of what the user said is a
# different privacy class from a record of what the terminal printed, and the
# microphone is the one input the user cannot see accumulating.
VOICE_HISTORY_KEY = "KILIX_VOICE_HISTORY"
VOICE_HISTORY_DEFAULT = "off"
VOICE_HISTORY_CHOICES = ("off", "on")

VOICE_CHOICE_SPECS = {
    VOICE_TTS_ENGINE_KEY: (VOICE_TTS_ENGINE_DEFAULT, VOICE_TTS_ENGINE_CHOICES),
    VOICE_TTS_RATE_KEY: (VOICE_TTS_RATE_DEFAULT, VOICE_TTS_RATE_CHOICES),
    VOICE_TTS_EXTENT_KEY: (VOICE_TTS_EXTENT_DEFAULT, VOICE_TTS_EXTENT_CHOICES),
    VOICE_TTS_MAX_CHARS_KEY: (
        VOICE_TTS_MAX_CHARS_DEFAULT, VOICE_TTS_MAX_CHARS_CHOICES),
    VOICE_STT_ENGINE_KEY: (VOICE_STT_ENGINE_DEFAULT, VOICE_STT_ENGINE_CHOICES),
    VOICE_STT_MODEL_KEY: (VOICE_STT_MODEL_DEFAULT, VOICE_STT_MODEL_CHOICES),
    VOICE_STT_SUBMIT_KEY: (VOICE_STT_SUBMIT_DEFAULT, VOICE_STT_SUBMIT_CHOICES),
    VOICE_STT_MAX_SECONDS_KEY: (
        VOICE_STT_MAX_SECONDS_DEFAULT, VOICE_STT_MAX_SECONDS_CHOICES),
    VOICE_STT_SILENCE_MS_KEY: (
        VOICE_STT_SILENCE_MS_DEFAULT, VOICE_STT_SILENCE_MS_CHOICES),
    VOICE_HISTORY_KEY: (VOICE_HISTORY_DEFAULT, VOICE_HISTORY_CHOICES),
}

# The two settings a preset list cannot cover: the valid voices are whatever the
# installed synthesiser was built with, and the valid devices are whatever the
# audio server reports right now.  Validated by shape instead, so a bad value
# still falls back rather than reaching a command line.  Voice names are short
# tokens (``en-us``, ``mb-us1``); PulseAudio device names are much longer and
# carry dots and colons (``alsa_input.pci-0000_00_1f.3.analog-stereo``).
VOICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_+-]{1,32}$")
VOICE_DEVICE_PATTERN = re.compile(r"^[A-Za-z0-9_.:+-]{1,128}$")
VOICE_TOKEN_SPECS = {
    VOICE_TTS_VOICE_KEY: (VOICE_TTS_VOICE_DEFAULT, VOICE_NAME_PATTERN),
    VOICE_DEVICE_IN_KEY: (VOICE_DEVICE_IN_DEFAULT, VOICE_DEVICE_PATTERN),
    VOICE_DEVICE_OUT_KEY: (VOICE_DEVICE_OUT_DEFAULT, VOICE_DEVICE_PATTERN),
}

# Written and presented in this order: what to read, how it sounds, what to
# hear, then where the audio goes.  The toggle sits with the dictation settings
# it qualifies rather than being hoisted to the top of the section.
VOICE_KEYS = (
    VOICE_TTS_ENGINE_KEY,
    VOICE_TTS_VOICE_KEY,
    VOICE_TTS_RATE_KEY,
    VOICE_TTS_EXTENT_KEY,
    VOICE_TTS_MAX_CHARS_KEY,
    VOICE_STT_ENGINE_KEY,
    VOICE_STT_MODEL_KEY,
    VOICE_STT_SUBMIT_KEY,
    VOICE_STT_MAX_SECONDS_KEY,
    VOICE_STT_SILENCE_MS_KEY,
    VOICE_PUNCTUATION_KEY,
    VOICE_DEVICE_IN_KEY,
    VOICE_DEVICE_OUT_KEY,
    VOICE_HISTORY_KEY,
)
VOICE_VALUE_KEYS = tuple(key for key in VOICE_KEYS if key not in TOGGLE_BY_KEY)

# Whether a resumed coding agent starts with its own approval prompts turned
# off for coding tools. Off by default and deliberately a stack-wide setting rather
# than a per-tool one: it governs whether an agent asks before it acts, so it
# belongs where the user can find and audit it alongside everything else, not
# buried in whichever launcher happened to start the session.
CODING_YOLO_KEY = "KILIX_CODING_YOLO"
CODING_YOLO_DEFAULT = "off"
CODING_YOLO_CHOICES = ("off", "on")
CODING_CHOICE_SPECS = {
    CODING_YOLO_KEY: (CODING_YOLO_DEFAULT, CODING_YOLO_CHOICES),
}
CODING_KEYS = tuple(CODING_CHOICE_SPECS)

MANAGED_KEYS = tuple(spec.key for spec in TOGGLE_SPECS) + (
    CLOCK_FORMAT_KEY,
    PANE_CPU_MODE_KEY,
    PANE_MEMORY_MODE_KEY,
    TAB_BAR_EDGE_KEY,
    TRANSCRIPT_GRAPHICS_KEY,
    TRANSCRIPT_LIMIT_KEY,
    TRANSCRIPT_TOTAL_KEY,
    TRANSCRIPT_ARCHIVE_KEY,
) + VOICE_VALUE_KEYS + CODING_KEYS

_ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _target_path(path: str | None) -> str:
    value = settings_path() if not path else os.fspath(path)
    return os.path.abspath(os.path.expanduser(value))


def _open_regular(path: str, flags: int, mode: int = 0o600) -> int:
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, mode)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "shared settings path is not a regular file", path)
    except BaseException:
        os.close(fd)
        raise
    return fd


def settings_path() -> str:
    """Return the one shared settings file used by every stack component."""
    override = os.environ.get("GPU_TERMINAL_SETTINGS_FILE")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    root = os.environ.get("GPU_TERMINAL_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "gpu_terminal")
    return os.path.join(os.path.abspath(os.path.expanduser(root)), SETTINGS_BASENAME)


def parse_text(text: str) -> dict[str, str]:
    """Parse a settings document, with the last assignment winning."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def read_text(path: str | None = None) -> tuple[str, bool]:
    """Read one bounded regular settings file; only absence is a fallback."""
    target = _target_path(path)
    try:
        fd = _open_regular(target, os.O_RDONLY)
    except FileNotFoundError:
        return "", False
    with os.fdopen(fd, "rb") as stream:
        data = stream.read(SETTINGS_MAX_BYTES + 1)
    if len(data) > SETTINGS_MAX_BYTES:
        raise OSError(
            errno.EFBIG,
            f"shared settings file exceeds {SETTINGS_MAX_BYTES} bytes",
            target,
        )
    return data.decode("utf-8", errors="replace"), True


def truthy(value: object) -> bool:
    return str(value).strip().lower() not in (
        "", "0", "no", "false", "off", "disabled")


def _normalize_change(key: str, raw_value: object) -> str:
    if key in TOGGLE_BY_KEY:
        return "1" if truthy(raw_value) else "0"
    if key in (PANE_CPU_MODE_KEY, PANE_MEMORY_MODE_KEY):
        value = str(raw_value).strip().lower()
        choices_for_key = (
            PANE_CPU_MODE_CHOICES
            if key == PANE_CPU_MODE_KEY
            else PANE_MEMORY_MODE_CHOICES
        )
        if value not in choices_for_key:
            choices = ", ".join(choices_for_key)
            raise ValueError(f"{key} must be one of: {choices}")
        return value
    if key == TAB_BAR_EDGE_KEY:
        value = str(raw_value).strip().lower()
        if value not in TAB_BAR_EDGE_CHOICES:
            choices = ", ".join(TAB_BAR_EDGE_CHOICES)
            raise ValueError(f"{TAB_BAR_EDGE_KEY} must be one of: {choices}")
        return value
    if key == TRANSCRIPT_GRAPHICS_KEY:
        value = str(raw_value).strip().lower()
        if value not in TRANSCRIPT_GRAPHICS_CHOICES:
            choices = ", ".join(TRANSCRIPT_GRAPHICS_CHOICES)
            raise ValueError(f"{TRANSCRIPT_GRAPHICS_KEY} must be one of: {choices}")
        return value
    if key == TRANSCRIPT_LIMIT_KEY:
        value = str(raw_value).strip().upper()
        if value not in TRANSCRIPT_LIMIT_CHOICES:
            choices = ", ".join(TRANSCRIPT_LIMIT_CHOICES)
            raise ValueError(f"{TRANSCRIPT_LIMIT_KEY} must be one of: {choices}")
        return value
    if key == TRANSCRIPT_TOTAL_KEY:
        value = str(raw_value).strip().upper()
        if value not in TRANSCRIPT_TOTAL_CHOICES:
            choices = ", ".join(TRANSCRIPT_TOTAL_CHOICES)
            raise ValueError(f"{TRANSCRIPT_TOTAL_KEY} must be one of: {choices}")
        return value
    if key == TRANSCRIPT_ARCHIVE_KEY:
        value = str(raw_value).strip()
        value = "off" if value.lower() == "off" else value.upper()
        if value not in TRANSCRIPT_ARCHIVE_CHOICES:
            choices = ", ".join(TRANSCRIPT_ARCHIVE_CHOICES)
            raise ValueError(f"{TRANSCRIPT_ARCHIVE_KEY} must be one of: {choices}")
        return value
    if key in VOICE_CHOICE_SPECS:
        _default, valid = VOICE_CHOICE_SPECS[key]
        value = str(raw_value).strip().lower()
        if value not in valid:
            raise ValueError(f"{key} must be one of: {', '.join(valid)}")
        return value
    if key in CODING_CHOICE_SPECS:
        _default, valid = CODING_CHOICE_SPECS[key]
        value = str(raw_value).strip().lower()
        if value not in valid:
            raise ValueError(f"{key} must be one of: {', '.join(valid)}")
        return value
    if key in VOICE_TOKEN_SPECS:
        _default, pattern = VOICE_TOKEN_SPECS[key]
        value = str(raw_value).strip()
        if not pattern.fullmatch(value):
            raise ValueError(f"{key} must be a plain engine or device name")
        return value
    value = (str(raw_value) or CLOCK_FORMAT_DEFAULT).replace(
        "\r", " ").replace("\n", " ").strip()
    if not value:
        value = CLOCK_FORMAT_DEFAULT
    if len(value) > 256 or any(ord(char) < 32 and char != "\t" for char in value):
        raise ValueError(f"{CLOCK_FORMAT_KEY} must be a short, single-line format")
    return value


def defaults(*, migrate_environment: bool = False) -> dict[str, str]:
    values = {
        spec.key: "1" if spec.default else "0"
        for spec in TOGGLE_SPECS
    }
    values[CLOCK_FORMAT_KEY] = CLOCK_FORMAT_DEFAULT
    values[PANE_CPU_MODE_KEY] = PANE_CPU_MODE_DEFAULT
    values[PANE_MEMORY_MODE_KEY] = PANE_MEMORY_MODE_DEFAULT
    values[TAB_BAR_EDGE_KEY] = TAB_BAR_EDGE_DEFAULT
    # Match the engine's contextual default without persisting it on first use.
    values["KILIX_CHROME_START_MENU"] = "1" if (
        os.environ.get("XDG_SESSION_DESKTOP", "").lower() == "pleb"
        or os.environ.get("XDG_CURRENT_DESKTOP", "") == "Pleb"
    ) else "0"
    values[TRANSCRIPT_GRAPHICS_KEY] = TRANSCRIPT_GRAPHICS_DEFAULT
    values[TRANSCRIPT_LIMIT_KEY] = TRANSCRIPT_LIMIT_DEFAULT
    values[TRANSCRIPT_TOTAL_KEY] = TRANSCRIPT_TOTAL_DEFAULT
    values[TRANSCRIPT_ARCHIVE_KEY] = TRANSCRIPT_ARCHIVE_DEFAULT
    for key, (default, _valid) in (*VOICE_CHOICE_SPECS.items(),
                                   *VOICE_TOKEN_SPECS.items(),
                                   *CODING_CHOICE_SPECS.items()):
        values[key] = default
    if migrate_environment:
        # Clock and battery were historically stored in kilix.env.  On the
        # first shared-file creation, preserve those effective preferences.
        for key in MANAGED_KEYS:
            if key in os.environ:
                try:
                    values[key] = _normalize_change(key, os.environ[key])
                except ValueError:
                    pass
        if "KILIX_CHROME_CALENDAR" not in os.environ \
                and "KILIX_CHROME_CLOCK" in os.environ:
            values["KILIX_CHROME_CALENDAR"] = _normalize_change(
                "KILIX_CHROME_CALENDAR", os.environ["KILIX_CHROME_CLOCK"])
    return values


def load(path: str | None = None) -> dict[str, str]:
    """Load effective values, falling back to defaults for absent keys."""
    text, exists = read_text(path)
    values = defaults(migrate_environment=not exists)
    if exists:
        values.update(parse_text(text))
    return values


def enabled(key: str, path: str | None = None) -> bool:
    spec = TOGGLE_BY_KEY.get(key)
    if spec is None:
        raise KeyError(f"unknown shared Kilix toggle: {key}")
    return truthy(load(path).get(key, "1" if spec.default else "0"))


def coding_yolo(path: str | None = None) -> bool:
    """Return whether resumed coding agents skip their own approval prompts."""
    value = load(path).get(CODING_YOLO_KEY, CODING_YOLO_DEFAULT)
    return str(value).strip().casefold() in ("on", "1", "true", "yes")


def game_enabled(game_id: str, path: str | None = None) -> bool:
    """Return whether a Kilix game is exposed by desktop providers."""
    try:
        key = GAME_KEY_BY_ID[game_id]
    except KeyError as error:
        raise KeyError(f"unknown Kilix game: {game_id}") from error
    return enabled(key, path)


def game_availability(path: str | None = None) -> dict[str, bool]:
    """Return one consistent snapshot of all Kilix game selections."""
    values = load(path)
    return {
        game_id: truthy(values[key])
        for game_id, key in GAME_KEY_BY_ID.items()
    }


def _initial_text(values: Mapping[str, str]) -> str:
    lines = [SETTINGS_HEADER, "", SETTINGS_MARKER]
    for spec in TOP_BAR_TOGGLES:
        if spec.key == "KILIX_CHROME_START_MENU" and spec.key not in os.environ:
            continue
        lines.append(f"{spec.key}={values[spec.key]}")
    lines.append(f"{CLOCK_FORMAT_KEY}={values[CLOCK_FORMAT_KEY]}")
    for spec in PANE_BUTTON_TOGGLES:
        lines.append(f"{spec.key}={values[spec.key]}")
    lines.append(f"{PANE_CPU_MODE_KEY}={values[PANE_CPU_MODE_KEY]}")
    lines.append(f"{PANE_MEMORY_MODE_KEY}={values[PANE_MEMORY_MODE_KEY]}")
    lines.append(f"{TAB_BAR_EDGE_KEY}={values[TAB_BAR_EDGE_KEY]}")
    lines.extend(("", SESSION_LOG_MARKER))
    for spec in SESSION_LOG_TOGGLES:
        lines.append(f"{spec.key}={values[spec.key]}")
    lines.append(f"{TRANSCRIPT_GRAPHICS_KEY}={values[TRANSCRIPT_GRAPHICS_KEY]}")
    lines.append(f"{TRANSCRIPT_LIMIT_KEY}={values[TRANSCRIPT_LIMIT_KEY]}")
    lines.append(f"{TRANSCRIPT_TOTAL_KEY}={values[TRANSCRIPT_TOTAL_KEY]}")
    lines.append(f"{TRANSCRIPT_ARCHIVE_KEY}={values[TRANSCRIPT_ARCHIVE_KEY]}")
    lines.extend(("", VOICE_MARKER))
    for key in VOICE_KEYS:
        lines.append(f"{key}={values[key]}")
    lines.extend(("", GAMES_MARKER))
    for spec in GAME_TOGGLES:
        lines.append(f"{spec.key}={values[spec.key]}")
    lines.extend(("", CODING_MARKER))
    for key in CODING_KEYS:
        lines.append(f"{key}={values[key]}")
    return "\n".join(lines) + "\n"


@contextmanager
def _settings_lock(target: str):
    directory = os.path.dirname(target)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = _open_regular(target + ".lock", os.O_RDWR | os.O_CREAT)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _fsync_directory(directory: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(target: str, text: str) -> None:
    data = text.encode("utf-8")
    if len(data) > SETTINGS_MAX_BYTES:
        raise OSError(
            errno.EFBIG,
            f"updated shared settings exceed {SETTINGS_MAX_BYTES} bytes",
            target,
        )
    directory = os.path.dirname(target)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(target)}.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = ""
        _fsync_directory(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _apply_changes(text: str, changes: Mapping[str, str]) -> str:
    for key, value in changes.items():
        text = _set_value(text, key, value)
    return text


def ensure_file(path: str | None = None) -> str:
    """Create the shared file without migrating an existing operator file.

    Readers already supply defaults for absent managed keys. Keeping an
    existing file byte-exact means that launching a newer Kilix cannot turn a
    release update into an implicit settings migration; a value is persisted
    only when the operator changes it explicitly.
    """
    target = _target_path(path)
    with _settings_lock(target):
        _text, exists = read_text(target)
        if not exists:
            _atomic_write(target, _initial_text(defaults(migrate_environment=True)))
            return target
        fd = _open_regular(target, os.O_RDONLY)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    return target


def _set_value(text: str, key: str, value: str) -> str:
    value = value.replace("\r", " ").replace("\n", " ").strip()
    line = f"{key}={value}"
    pattern = re.compile(rf"^\s*{re.escape(key)}=.*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if matches:
        last = matches[-1]
        return text[:last.start()] + line + text[last.end():]
    if key in GAME_ID_BY_KEY:
        marker = GAMES_MARKER
    elif key in VOICE_KEYS:
        marker = VOICE_MARKER
    elif key in CODING_KEYS:
        marker = CODING_MARKER
    else:
        marker = SETTINGS_MARKER
    if marker not in text:
        text = text.rstrip("\n") + f"\n\n{marker}\n"
    return text.rstrip("\n") + "\n" + line + "\n"


def update(changes: Mapping[str, object], path: str | None = None) -> str:
    """Atomically update managed keys while preserving comments/unknown keys."""
    unknown = set(changes) - set(MANAGED_KEYS)
    if unknown:
        raise KeyError(f"unknown shared setting(s): {', '.join(sorted(unknown))}")
    normalized = {
        key: _normalize_change(key, raw_value)
        for key, raw_value in changes.items()
    }
    target = _target_path(path)
    with _settings_lock(target):
        text, exists = read_text(target)
        if not exists:
            text = _initial_text(defaults(migrate_environment=True))
        _atomic_write(target, _apply_changes(text, normalized))
    return target


def pane_memory_mode(path: str | None = None) -> str:
    """Return the normalized per-pane memory-chip visibility policy."""
    value = load(path).get(
        PANE_MEMORY_MODE_KEY, PANE_MEMORY_MODE_DEFAULT).strip().lower()
    return (
        value if value in PANE_MEMORY_MODE_CHOICES
        else PANE_MEMORY_MODE_DEFAULT
    )


def pane_cpu_mode(path: str | None = None) -> str:
    """Return the normalized pane process-tree CPU visibility policy."""
    value = load(path).get(
        PANE_CPU_MODE_KEY, PANE_CPU_MODE_DEFAULT).strip().lower()
    return (
        value if value in PANE_CPU_MODE_CHOICES
        else PANE_CPU_MODE_DEFAULT
    )


def transcript_enabled(path: str | None = None) -> bool:
    """Return whether panes record a durable session log."""
    return enabled("KILIX_TRANSCRIPT", path)


def transcript_graphics(path: str | None = None) -> str:
    """Return the normalized graphics-payload policy for transcripts."""
    value = load(path).get(
        TRANSCRIPT_GRAPHICS_KEY, TRANSCRIPT_GRAPHICS_DEFAULT).strip().lower()
    return (
        value if value in TRANSCRIPT_GRAPHICS_CHOICES
        else TRANSCRIPT_GRAPHICS_DEFAULT
    )


def transcript_limit(path: str | None = None) -> int:
    """Return the per-pane transcript size budget in bytes."""
    value = load(path).get(
        TRANSCRIPT_LIMIT_KEY, TRANSCRIPT_LIMIT_DEFAULT).strip().upper()
    if value not in TRANSCRIPT_LIMIT_CHOICES:
        value = TRANSCRIPT_LIMIT_DEFAULT
    return _size_bytes(value)


def _size_bytes(token: str) -> int:
    """Convert a settings size token such as ``8M`` or ``20G`` to bytes."""
    token = token.strip().upper()
    for suffix, scale in (("G", 1024 ** 3), ("M", 1024 ** 2), ("K", 1024)):
        if token.endswith(suffix):
            return int(token.removesuffix(suffix)) * scale
    return int(token)


def transcript_total(path: str | None = None) -> int:
    """Return the recent zstd -3 transcript-tier budget in bytes."""
    value = load(path).get(
        TRANSCRIPT_TOTAL_KEY, TRANSCRIPT_TOTAL_DEFAULT).strip().upper()
    if value not in TRANSCRIPT_TOTAL_CHOICES:
        value = TRANSCRIPT_TOTAL_DEFAULT
    return _size_bytes(value)


def transcript_archive_total(path: str | None = None) -> int:
    """Return the older zstd -9 tier budget; ``0`` disables that tier.

    ``off`` means a transcript leaving the recent tier is deleted instead of
    recompressed, giving the transcript tree a hard recent-tier ceiling.
    """
    value = load(path).get(
        TRANSCRIPT_ARCHIVE_KEY, TRANSCRIPT_ARCHIVE_DEFAULT).strip()
    value = "off" if value.lower() == "off" else value.upper()
    if value not in TRANSCRIPT_ARCHIVE_CHOICES:
        value = TRANSCRIPT_ARCHIVE_DEFAULT
    return 0 if value == "off" else _size_bytes(value)


def _voice_choice(key: str, path: str | None) -> str:
    default, valid = VOICE_CHOICE_SPECS[key]
    value = load(path).get(key, default).strip().lower()
    return value if value in valid else default


def _voice_token(key: str, path: str | None) -> str:
    default, pattern = VOICE_TOKEN_SPECS[key]
    value = load(path).get(key, default).strip()
    return value if pattern.match(value) else default


def tts_engine(path: str | None = None) -> str:
    """Return the speech synthesiser read-aloud should use, or ``off``."""
    return _voice_choice(VOICE_TTS_ENGINE_KEY, path)


def tts_voice(path: str | None = None) -> str:
    """Return the synthesiser voice name, unvalidated against the engine.

    Whether the local ``espeak-ng`` actually has this voice is the engine's
    question to answer at synthesis time; this only guarantees the value is a
    plain token, so an unusable voice degrades a read rather than shaping a
    command line.
    """
    return _voice_token(VOICE_TTS_VOICE_KEY, path)


def tts_rate(path: str | None = None) -> int:
    """Return the read-aloud speaking rate in words per minute."""
    return int(_voice_choice(VOICE_TTS_RATE_KEY, path))


def tts_extent(path: str | None = None) -> str:
    """Return how much of the pane read-aloud covers when nothing is selected."""
    return _voice_choice(VOICE_TTS_EXTENT_KEY, path)


def tts_max_chars(path: str | None = None) -> int | None:
    """Return the read-aloud length cap in characters, or ``None`` if uncapped."""
    value = _voice_choice(VOICE_TTS_MAX_CHARS_KEY, path)
    return None if value == "unlimited" else int(value)


def stt_engine(path: str | None = None) -> str:
    """Return the recogniser dictation should use, or ``off``."""
    return _voice_choice(VOICE_STT_ENGINE_KEY, path)


def stt_model(path: str | None = None) -> str:
    """Return the catalog id of the acoustic model dictation should load."""
    return _voice_choice(VOICE_STT_MODEL_KEY, path)


def stt_engine_for_model(model: str) -> str:
    """Return the engine paired with one catalog model, or reject the id."""
    try:
        return VOICE_STT_MODEL_ENGINES[model]
    except KeyError as error:
        choices = ", ".join(VOICE_STT_MODEL_CHOICES)
        raise ValueError(
            f"unknown speech model {model!r}; choose one of: {choices}") from error


def set_stt_default(model: str, path: str | None = None) -> str:
    """Save a speech model and its matching recognizer as one default."""
    return update({
        VOICE_STT_MODEL_KEY: model,
        VOICE_STT_ENGINE_KEY: stt_engine_for_model(model),
    }, path)


def stt_submit(path: str | None = None) -> str:
    """Return the dictation submit policy: ``never`` or ``confirm``.

    Callers may treat any value other than ``confirm`` as ``never``; there is
    deliberately no third choice that submits without asking.
    """
    return _voice_choice(VOICE_STT_SUBMIT_KEY, path)


def stt_max_seconds(path: str | None = None) -> int:
    """Return the hard ceiling on one dictation, in seconds."""
    return int(_voice_choice(VOICE_STT_MAX_SECONDS_KEY, path))


def stt_silence_ms(path: str | None = None) -> int:
    """Return the trailing silence that ends an utterance, in milliseconds."""
    return int(_voice_choice(VOICE_STT_SILENCE_MS_KEY, path))


def voice_device_in(path: str | None = None) -> str:
    """Return the capture device name, or ``default`` for the server's choice."""
    return _voice_token(VOICE_DEVICE_IN_KEY, path)


def voice_device_out(path: str | None = None) -> str:
    """Return the playback device name, or ``default`` for the server's choice."""
    return _voice_token(VOICE_DEVICE_OUT_KEY, path)


def voice_history(path: str | None = None) -> bool:
    """Return whether dictation may be written to a local history file.

    Boolean rather than the stored ``off``/``on`` token because the token is
    the wrong shape for the question every caller asks, and ``"off"`` is
    truthy.
    """
    return _voice_choice(VOICE_HISTORY_KEY, path) == "on"


__all__ = [
    "CLOCK_FORMAT_DEFAULT",
    "CLOCK_FORMAT_KEY",
    "CODING_CHOICE_SPECS",
    "CODING_KEYS",
    "CODING_MARKER",
    "CODING_YOLO_CHOICES",
    "CODING_YOLO_DEFAULT",
    "CODING_YOLO_KEY",
    "coding_yolo",
    "GAMES_MARKER",
    "GAME_ID_BY_KEY",
    "GAME_KEY_BY_ID",
    "GAME_TOGGLE_IDS",
    "GAME_TOGGLES",
    "MANAGED_KEYS",
    "PANE_BUTTON_TOGGLES",
    "PANE_CPU_MODE_CHOICES",
    "PANE_CPU_MODE_DEFAULT",
    "PANE_CPU_MODE_KEY",
    "PANE_MEMORY_MODE_CHOICES",
    "PANE_MEMORY_MODE_DEFAULT",
    "PANE_MEMORY_MODE_KEY",
    "SETTINGS_BASENAME",
    "SETTINGS_MAX_BYTES",
    "SESSION_LOG_MARKER",
    "SESSION_LOG_TOGGLES",
    "TAB_BAR_EDGE_CHOICES",
    "TAB_BAR_EDGE_DEFAULT",
    "TAB_BAR_EDGE_KEY",
    "TOGGLE_BY_KEY",
    "TOGGLE_SPECS",
    "TOP_BAR_TOGGLES",
    "TRANSCRIPT_ARCHIVE_CHOICES",
    "TRANSCRIPT_ARCHIVE_DEFAULT",
    "TRANSCRIPT_ARCHIVE_KEY",
    "TRANSCRIPT_GRAPHICS_CHOICES",
    "TRANSCRIPT_GRAPHICS_DEFAULT",
    "TRANSCRIPT_GRAPHICS_KEY",
    "TRANSCRIPT_LIMIT_CHOICES",
    "TRANSCRIPT_LIMIT_DEFAULT",
    "TRANSCRIPT_LIMIT_KEY",
    "TRANSCRIPT_TOTAL_CHOICES",
    "TRANSCRIPT_TOTAL_DEFAULT",
    "TRANSCRIPT_TOTAL_KEY",
    "VOICE_STT_MODEL_ENGINES",
    "ToggleSpec",
    "defaults",
    "enabled",
    "ensure_file",
    "game_availability",
    "game_enabled",
    "load",
    "pane_cpu_mode",
    "pane_memory_mode",
    "parse_text",
    "read_text",
    "settings_path",
    "set_stt_default",
    "stt_engine_for_model",
    "transcript_archive_total",
    "transcript_enabled",
    "transcript_graphics",
    "transcript_limit",
    "transcript_total",
    "truthy",
    "update",
]
