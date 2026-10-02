"""kilix desktop — kilix Settings (the control panel).

Edits the writable per-user Kilix configuration (normally under
``~/.local/gpu_terminal/kilix``; ``$KITTY_CONFIG_DIRECTORY`` overrides it). The
tracked ``config/`` tree contains defaults and is never rewritten. Clickable
chrome settings live in the stack-wide ``~/.local/gpu_terminal/settings.conf``.
In Batty mode the panel shows supported shared choices and applies them with
``kilix reload-settings``. In Kitty mode the raw kitty.conf tab remains
available and Apply uses the original Kitty control path. Managed lines are rewritten
(last occurrence wins, matching kitty's own semantics); everything else in
the file — comments included — is preserved byte for byte.
"""
import os
import re
import shutil
import signal
import subprocess
import tempfile
from typing import NamedTuple

import shell as _shell
import theme as T
import widgets as W
import wm
import storage
from kilix_sdk import settings as shared_settings

MARKER = "# ── kilix desktop settings ──"
ENV_MARKER = "# -- kilix settings env --"
BATTY_SHARED_KEYS = frozenset({
    shared_settings.TAB_BAR_EDGE_KEY,
    "KILIX_CHROME_START_MENU",
    "KILIX_TRANSCRIPT",
    shared_settings.TRANSCRIPT_GRAPHICS_KEY,
    shared_settings.TRANSCRIPT_LIMIT_KEY,
    shared_settings.TRANSCRIPT_TOTAL_KEY,
    shared_settings.TRANSCRIPT_ARCHIVE_KEY,
    *("KILIX_CHROME_BUTTON_" + name for name in (
        "SYNCHRONIZE_INPUT", "FONT_INCREASE", "FONT_DECREASE", "SPLIT_LEFT",
        "SPLIT_UP", "SPLIT_DOWN", "SPLIT_RIGHT", "MAXIMIZE", "CLOSE")),
})


class SettingSpec(NamedTuple):
    source: str
    key: str
    label: str
    kind: str
    default: str
    extra: object = None


def K(key, label, kind="text", default="", extra=None):
    return SettingSpec("kitty", key, label, kind, default, extra)


def E(key, label, kind="text", default="", extra=None):
    return SettingSpec("env", key, label, kind, default, extra)


def S(key, label, kind="bool", default="1", extra=("1", "0")):
    return SettingSpec("shared", key, label, kind, default, extra)


def L(key, label, default, line):
    return SettingSpec("line", key, label, "bool", default, line)


# kind: text | color | choice | bool. Bool extra is (true_value, false_value).
SETTING_PAGES = [
    ("Look", [
        K("font_family", "Font family"),
        K("font_size", "Font size", default="11.0"),
        K("foreground", "Text color", "color"),
        K("background", "Background color", "color"),
        K("background_opacity", "Background opacity"),
        K("cursor_shape", "Cursor shape", "choice", "block",
          ["block", "beam", "underline"]),
        K("tab_bar_style", "Page strip style", "choice", "powerline",
          ["fade", "separator", "powerline", "slant", "hidden", "custom"]),
        K("tab_powerline_style", "Powerline shape", "choice", "slanted",
          ["slanted", "round", "angled"]),
        K("draw_minimal_borders", "Minimal borders", "bool", "yes"),
        K("inactive_text_alpha", "Inactive text alpha", default="0.7"),
    ]),
    ("Terminal", [
        K("scrollback_lines", "Scrollback lines", default="10000"),
        E("KILIX_SHELL_INTEGRATION", "Kilix bash rc", "bool", "1", ("1", "0")),
        K("enable_audio_bell", "Audio bell", "bool", "no"),
        K("window_alert_on_bell", "Urgency on bell", "bool", "no"),
        K("copy_on_select", "Copy on select", "bool", "clipboard",
          ("clipboard", "no")),
        K("confirm_os_window_close", "Confirm close panes", default="-1"),
        K("mouse_hide_wait", "Hide mouse after (s)"),
        K("cursor_blink_interval", "Cursor blink interval"),
        K("software_mouse_cursor", "Software pointer", "choice", "block",
          ["block", "pointer", "none"]),
        L("right_click_context_menu", "Right-click menu", "yes",
          "mouse_map right press ungrabbed show_context_menu"),
        L("middle_paste_selection", "Middle paste selection", "yes",
          "mouse_map middle release ungrabbed paste_from_selection"),
        K("allow_remote_control", "Remote control", "choice", "socket",
          ["socket", "password", "no", "yes"]),
        K("listen_on", "Remote socket", default="unix:@kilix-{kitty_pid}"),
    ]),
    ("Chrome", [
        S(shared_settings.TAB_BAR_EDGE_KEY, "Page strip edge", "choice",
          shared_settings.TAB_BAR_EDGE_DEFAULT,
          list(shared_settings.TAB_BAR_EDGE_CHOICES)),
        S("KILIX_CHROME_TEMPERATURE", "Thermal status", default="0"),
        S("KILIX_CHROME_START_MENU", "Start menu", default="0"),
        S("KILIX_CHROME_VOLUME", "Volume"),
        S("KILIX_CHROME_NETWORK", "Network / Wi-Fi"),
        S("KILIX_CHROME_CALENDAR", "Calendar"),
        S("KILIX_CHROME_CLOCK", "Date and time"),
        S("KILIX_CHROME_CLOCK_FORMAT", "Clock format", "text",
          "%Y-%m-%d %H:%M", None),
        S("KILIX_CHROME_BATTERY", "Battery"),
        S("KILIX_CHROME_WINDOWS", "Native window taskbar (Pleb)"),
        E("KILIX_BATTERY_SUPPLY_DIR", "Battery supply dir"),
        K("tab_bar_min_tabs", "Show page strip at", default="1"),
        K("tab_bar_show_new_tab_button", "New-page button", "bool", "yes"),
    ]),
    ("Pane", [
        S(
            shared_settings.PANE_CPU_MODE_KEY,
            "Pane CPU load",
            "choice",
            shared_settings.PANE_CPU_MODE_DEFAULT,
            list(shared_settings.PANE_CPU_MODE_CHOICES),
        ),
        S(
            shared_settings.PANE_MEMORY_MODE_KEY,
            "Pane memory chip",
            "choice",
            shared_settings.PANE_MEMORY_MODE_DEFAULT,
            list(shared_settings.PANE_MEMORY_MODE_CHOICES),
        ),
        S("KILIX_CHROME_BUTTON_SYNCHRONIZE_INPUT", "Synchronize keyboard input"),
        S("KILIX_CHROME_BUTTON_FONT_INCREASE", "Increase text size"),
        S("KILIX_CHROME_BUTTON_FONT_DECREASE", "Decrease text size"),
        S("KILIX_CHROME_BUTTON_SPLIT_LEFT", "Split pane left"),
        S("KILIX_CHROME_BUTTON_SPLIT_UP", "Split pane up"),
        S("KILIX_CHROME_BUTTON_SPLIT_DOWN", "Split pane down"),
        S("KILIX_CHROME_BUTTON_SPLIT_RIGHT", "Split pane right"),
        S("KILIX_CHROME_BUTTON_MAXIMIZE", "Maximize / restore"),
        S("KILIX_CHROME_BUTTON_CLOSE", "Close pane"),
        K("window_title_bar", "Pane title bars", "choice", "top",
          ["top", "bottom", "none"]),
        K("window_title_bar_min_windows", "Title bars at panes", default="1"),
        K("window_title_bar_align", "Title alignment", "choice", "left",
          ["left", "center", "right"]),
        K("enabled_layouts", "Enabled layouts", default="splits,stack,tall,grid"),
    ]),
    ("Session logs", [
        S("KILIX_TRANSCRIPT", "Record pane session logs"),
        S(
            shared_settings.TRANSCRIPT_GRAPHICS_KEY,
            "Graphics in logs",
            "choice",
            shared_settings.TRANSCRIPT_GRAPHICS_DEFAULT,
            list(shared_settings.TRANSCRIPT_GRAPHICS_CHOICES),
        ),
        S(
            shared_settings.TRANSCRIPT_LIMIT_KEY,
            "Log size per pane",
            "choice",
            shared_settings.TRANSCRIPT_LIMIT_DEFAULT,
            list(shared_settings.TRANSCRIPT_LIMIT_CHOICES),
        ),
        S(
            shared_settings.TRANSCRIPT_TOTAL_KEY,
            "Recent logs (zstd -3)",
            "choice",
            shared_settings.TRANSCRIPT_TOTAL_DEFAULT,
            list(shared_settings.TRANSCRIPT_TOTAL_CHOICES),
        ),
        S(
            shared_settings.TRANSCRIPT_ARCHIVE_KEY,
            "Older logs (zstd -9)",
            "choice",
            shared_settings.TRANSCRIPT_ARCHIVE_DEFAULT,
            list(shared_settings.TRANSCRIPT_ARCHIVE_CHOICES),
        ),
    ]),
    # Kilix 95 carries the same page. The built-in provider is a compatibility
    # fallback, but it declares the same SDK level, so a setting it cannot reach
    # here would be a setting the user is told exists and cannot change.
    ("Voice", [
        S("KILIX_CHROME_SPEAK", "Read pane aloud"),
        S("KILIX_CHROME_DICTATE", "Dictate to pane"),
        S(shared_settings.VOICE_TTS_ENGINE_KEY, "Speech engine", "choice",
          shared_settings.VOICE_TTS_ENGINE_DEFAULT,
          list(shared_settings.VOICE_TTS_ENGINE_CHOICES)),
        S(shared_settings.VOICE_TTS_VOICE_KEY, "Voice", "text",
          shared_settings.VOICE_TTS_VOICE_DEFAULT, None),
        S(shared_settings.VOICE_TTS_RATE_KEY, "Speaking rate (wpm)", "choice",
          shared_settings.VOICE_TTS_RATE_DEFAULT,
          list(shared_settings.VOICE_TTS_RATE_CHOICES)),
        S(shared_settings.VOICE_TTS_EXTENT_KEY, "Read extent", "choice",
          shared_settings.VOICE_TTS_EXTENT_DEFAULT,
          list(shared_settings.VOICE_TTS_EXTENT_CHOICES)),
        S(shared_settings.VOICE_TTS_MAX_CHARS_KEY, "Maximum characters",
          "choice", shared_settings.VOICE_TTS_MAX_CHARS_DEFAULT,
          list(shared_settings.VOICE_TTS_MAX_CHARS_CHOICES)),
        S(shared_settings.VOICE_DEVICE_OUT_KEY, "Output device", "text",
          shared_settings.VOICE_DEVICE_OUT_DEFAULT, None),
        S(shared_settings.VOICE_DEVICE_IN_KEY, "Input device", "text",
          shared_settings.VOICE_DEVICE_IN_DEFAULT, None),
        S(shared_settings.VOICE_STT_ENGINE_KEY, "Dictation engine", "choice",
          shared_settings.VOICE_STT_ENGINE_DEFAULT,
          list(shared_settings.VOICE_STT_ENGINE_CHOICES)),
        S(shared_settings.VOICE_STT_MODEL_KEY, "Dictation model", "choice",
          shared_settings.VOICE_STT_MODEL_DEFAULT,
          list(shared_settings.VOICE_STT_MODEL_CHOICES)),
        S(shared_settings.VOICE_STT_SUBMIT_KEY, "On finishing dictation",
          "choice", shared_settings.VOICE_STT_SUBMIT_DEFAULT,
          list(shared_settings.VOICE_STT_SUBMIT_CHOICES)),
        S(shared_settings.VOICE_STT_MAX_SECONDS_KEY, "Listen limit (s)",
          "choice", shared_settings.VOICE_STT_MAX_SECONDS_DEFAULT,
          list(shared_settings.VOICE_STT_MAX_SECONDS_CHOICES)),
        S(shared_settings.VOICE_STT_SILENCE_MS_KEY, "Trailing silence (ms)",
          "choice", shared_settings.VOICE_STT_SILENCE_MS_DEFAULT,
          list(shared_settings.VOICE_STT_SILENCE_MS_CHOICES)),
        S(shared_settings.VOICE_PUNCTUATION_KEY, "Spoken punctuation"),
        S(shared_settings.VOICE_HISTORY_KEY, "Dictation history", "choice",
          shared_settings.VOICE_HISTORY_DEFAULT,
          list(shared_settings.VOICE_HISTORY_CHOICES)),
    ]),
    ("Games", [
        S(spec.key, spec.label)
        for spec in shared_settings.GAME_TOGGLES
    ]),
    ("Desktop", [
        E("KILIX_DESKTOP_PROVIDER", "Provider", "choice", "auto",
          ["auto", "builtin", "external", "cap", "tui", "land", "command",
           "none"]),
        E("KILIX_DESKTOP_FLAVOR", "Flavor", "choice", "95",
          ["95", "xp"]),
        E("KILIX_DESKTOP_COMMAND", "Custom command"),
        E("KILIX_DESKTOP_NAME", "Tab title", default="desktop"),
        E("KILIX95_AUTO_INSTALL", "Auto-install external", "bool", "0",
          ("1", "0")),
        E("KILIX95_TRUST_EXISTING_CHECKOUT", "Trust checkout origin", "bool",
          "0", ("1", "0")),
        E("KILIX95_ALLOW_MUTABLE_REF", "Allow mutable ref", "bool", "0",
          ("1", "0")),
        E("KILIX95_ALLOW_UNPINNED_INSTALL", "Allow unpinned install", "bool",
          "0", ("1", "0")),
        E("KILIX95_DIR", "External checkout",
          default="~/.local/gpu_terminal/sources/kilix-desktops/kilix-95"),
        E("KILIX95_REPO", "External repo",
          default="https://github.com/itsmygithubacct/kilix-95.git"),
        E("KILIX95_BRANCH", "External branch"),
        E("KILIX95_REF", "External ref"),
    ]),
    ("Apps", [
        S(shared_settings.CODING_YOLO_KEY, "Skip coding-agent approvals",
          "choice", shared_settings.CODING_YOLO_DEFAULT,
          list(shared_settings.CODING_YOLO_CHOICES)),
        E("KILIX_BROWSE_BACKEND", "Browser renderer", "choice", "presenter",
          ["presenter", "go"]),
        E("KILIX_RUN_AUTO_FIT", "Auto-fit X apps", "bool", "1", ("1", "0")),
        E("KILIX_NO_PANE", "Headless run default", "bool", "0", ("1", "0")),
        E("KILIX_DEBUG", "Debug metrics", "bool", "0", ("1", "0")),
        E("KILIX_HW", "Prefer hardware encode", "bool", "0", ("1", "0")),
        E("KILIX_BROWSE_LOG", "Browser log path"),
        E("KILIX_RUN_LOG", "Run log path"),
        E("KILIX_XFONTS", "X font path"),
        E("KILIX_BRIDGE_TOKEN", "Bridge token"),
    ]),
    ("Files", [
        E("KILIX_DESKTOP_DIR", "Desktop folder"),
        E("KILIX_RECYCLE_DIR", "Recycle folder"),
        E("KILIX_SAVER_IDLE", "Screensaver idle (s)", default="180"),
        E("KILIX_HOST_CLIP", "Host clipboard bridge", "bool", "1", ("1", "0")),
        E("KILIX_NO_SOUND", "Disable sounds", "bool", "0", ("1", "0")),
        E("KILIX_XPANE_WM", "XPane window controls", "bool", "1", ("1", "0")),
    ]),
    ("Build", [
        E("KILIX_REF", "Update ref"),
        E("KILIX_ALLOW_MUTABLE_REF", "Allow mutable update ref", "bool", "0",
          ("1", "0")),
        E("KILIX_REPO", "Expected update origin",
          default="https://github.com/itsmygithubacct/kilix.git"),
        E("KILIX_TRUST_EXISTING_CHECKOUT", "Trust update origin", "bool", "0",
          ("1", "0")),
        E("KILIX_PREBUILT_VERSION", "Prebuilt version"),
        E("KILIX_PREBUILT_SHA256", "Prebuilt SHA256"),
        E("KILIX_ALLOW_UNVERIFIED_PREBUILT", "Allow unverified prebuilt", "bool",
          "0", ("1", "0")),
        E("KILIX_BUILD_MODE", "Fork dependencies", "choice", "system",
          ["system", "bundle"]),
        E("KILIX_KITTY_DEPS_URL", "Kitty deps URL"),
        E("KILIX_KITTY_DEPS_SHA256", "Kitty deps SHA256"),
        E("KILIX_NERD_FONT_URL", "Nerd Font URL"),
        E("KILIX_NERD_FONT_SHA256", "Nerd Font SHA256"),
        E("KILIX_NERD_FONT_FILE_SHA256", "Nerd Font file SHA256"),
        E("KILIX_GO_TOOLCHAIN", "Exact Go toolchain", default="go1.26.4"),
    ]),
]


def config_path():
    try:
        from kilix_sdk.paths import config_dir
        d = config_dir()
    except ImportError:
        d = os.environ.get("KITTY_CONFIG_DIRECTORY") or storage.config_dir()
    return os.path.join(d, "kitty.conf")


def env_config_path():
    try:
        from kilix_sdk.paths import config_dir
        d = config_dir()
    except ImportError:
        d = os.environ.get("KITTY_CONFIG_DIRECTORY") or storage.config_dir()
    return os.environ.get("KILIX_ENV_CONFIG") or os.path.join(d, "kilix.env")


def _is_true(s):
    return s.lower() in ("yes", "y", "true", "1")


def _bool_values(spec):
    if isinstance(spec.extra, tuple) and len(spec.extra) == 2:
        return spec.extra
    return "yes", "no"


def _bool_checked(spec, val):
    val = spec.default if val is None else val
    true_value, false_value = _bool_values(spec)
    if val == true_value:
        return True
    if val == false_value:
        return False
    return _is_true(val)


def _field_default(spec):
    if spec.kind == "choice" and spec.default:
        return spec.default
    if spec.kind == "choice" and spec.extra:
        return spec.extra[0]
    return spec.default


def get_key(text, key):
    pat = re.compile(rf"^\s*{re.escape(key)}\s+(.*?)\s*$", re.M)
    hits = pat.findall(text)
    return hits[-1] if hits else None


def set_key(text, key, value):
    line = f"{key:<30} {value}".rstrip()
    pat = re.compile(rf"^\s*{re.escape(key)}\s+.*$", re.M)
    hits = list(pat.finditer(text))
    if hits:
        last = hits[-1]
        return text[:last.start()] + line + text[last.end():]
    if MARKER not in text:
        text = text.rstrip("\n") + f"\n\n{MARKER}\n"
    return text.rstrip("\n") + "\n" + line + "\n"


def has_line(text, line):
    wanted = line.strip()
    return any(ln.strip() == wanted for ln in text.splitlines())


def set_line(text, line):
    if has_line(text, line):
        return text
    if MARKER not in text:
        text = text.rstrip("\n") + f"\n\n{MARKER}\n"
    return text.rstrip("\n") + "\n" + line.rstrip() + "\n"


def unset_line(text, line):
    wanted = line.strip()
    lines = [ln for ln in text.splitlines() if ln.strip() != wanted]
    return "\n".join(lines) + ("\n" if text.endswith("\n") and lines else "")


def get_env_key(text, key):
    pat = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}=(.*?)\s*$", re.M)
    hits = pat.findall(text)
    return hits[-1] if hits else None


def _clean_env_value(value):
    return value.replace("\r", " ").replace("\n", " ").strip()


def set_env_key(text, key, value):
    value = _clean_env_value(value)
    line = f"{key}={value}"
    pat = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}=.*$", re.M)
    hits = list(pat.finditer(text))
    if hits:
        last = hits[-1]
        return text[:last.start()] + line + text[last.end():]
    if ENV_MARKER not in text:
        text = text.rstrip("\n") + f"\n\n{ENV_MARKER}\n"
    return text.rstrip("\n") + "\n" + line + "\n"


def unset_env_key(text, key):
    pat = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}=.*\n?", re.M)
    return pat.sub("", text)


def _atomic_write_private(path, text):
    """Replace a user config atomically without following a destination link."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if fd is not None:
            os.close(fd)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


class _Swatch(W.Widget):
    """Live color preview next to a #rrggbb text field."""

    def __init__(self, x, y, field):
        super().__init__(x, y, 21, 21)
        self.field = field

    def draw(self, d, img):
        col = T.FACE
        m = re.fullmatch(r"#?([0-9a-fA-F]{6})", self.field.text.strip())
        if m:
            v = int(m.group(1), 16)
            col = ((v >> 16) & 255, (v >> 8) & 255, v & 255)
        T.sunken(d, self.x, self.y, self.x + self.w - 1,
                 self.y + self.h - 1, fill=col)


class SettingsWin(wm.Window):
    def __init__(self, desk):
        super().__init__(desk, "kilix Settings", 560, 520, icon="settings")
        self.batty_mode = os.environ.get("BATTY_KILIX_DESKTOP") == "1"
        self.min_w, self.min_h = 520, 420
        self.path = config_path()
        self.env_path = env_config_path()
        self.shared_path = shared_settings.settings_path()
        self.shared_values = shared_settings.load(self.shared_path)
        self.shared_changes = {}
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                self.kitty_buffer = f.read()
        except OSError:
            defaults = os.path.join(_shell.KILIX_HOME, "config", "kitty.conf")
            self.kitty_buffer = (
                "# Kilix user overrides. Tracked defaults are loaded first.\n"
                "include .kilix-defaults.conf\n"
                if os.path.isfile(defaults) else ""
            )
        try:
            with open(self.env_path, encoding="utf-8", errors="replace") as f:
                self.env_buffer = f.read()
        except OSError:
            self.env_buffer = ""
        self.buffer = self.kitty_buffer
        self._last_saved_env_buffer = self.env_buffer
        cw, ch = self.client_size()
        self.display_pages = (
            [(label, selected) for label, specs in SETTING_PAGES
             if (selected := [spec for spec in specs if spec.source == "shared"
                              and spec.key in BATTY_SHARED_KEYS])]
            if self.batty_mode else SETTING_PAGES)
        self.page_specs = [spec for _, spec in self.display_pages]
        tab_labels = [label for label, _ in self.display_pages]
        self.raw_tab = -1 if self.batty_mode else len(self.display_pages)
        if not self.batty_mode:
            tab_labels.append("kitty.conf")
        self.tabs = self.add(W.TabBar(6, 6, cw - 12,
                                      tab_labels, cb=self._switch_tab))
        self.fields = {}              # key -> (kind, widget)
        self.field_specs = {}          # key -> SettingSpec
        self.panels = [[] for _ in tab_labels]
        for tab_i, spec in enumerate(self.page_specs):
            y = 44
            for item in spec:
                key, label, kind, extra = item.key, item.label, item.kind, item.extra
                lw = self.add(W.Label(18, y + 4, label + ":"))
                self.panels[tab_i].append(lw)
                if kind == "choice":
                    width = (128 if key == shared_settings.VOICE_STT_MODEL_KEY
                             else 230)
                    wd = self.add(W.Dropdown(250, y, width, extra))
                    if key == shared_settings.VOICE_STT_MODEL_KEY:
                        self.voice_model_button = self.add(W.Button(
                            386, y, 94, 23, "Install + use",
                            cb=self._install_voice_model))
                        self.panels[tab_i].append(self.voice_model_button)
                elif kind == "bool":
                    wd = self.add(W.Checkbox(250, y + 3, "enabled"))
                    wd.default_val = item.default
                else:
                    wd = self.add(W.TextField(250, y, 230))
                    if kind == "color":
                        sw = self.add(_Swatch(488, y, wd))
                        self.panels[tab_i].append(sw)
                        wd.on_change = lambda *_: self.invalidate()
                self.fields[key] = (kind, wd)
                self.field_specs[key] = item
                self.panels[tab_i].append(wd)
                y += 28
            note_y = y + 4
            if self.display_pages[tab_i][0] == "Games":
                note_text = "The Games menu updates the next time Start opens."
            elif self.batty_mode:
                note_text = "These shared choices reload in the Batty frontend."
            elif any(s.source in ("env", "shared") for s in spec):
                note_text = (
                    "Shared chrome refreshes live; runtime settings affect new launches.")
            else:
                note_text = "Applied live to this kilix by reloading kitty.conf."
            note = self.add(W.Label(18, note_y, note_text, font=T.SMALL,
                                    color=T.SHADOW))
            self.panels[tab_i].append(note)
        if not self.batty_mode:
            self.ta = self.add(W.TextArea(6, self.tabs.y + W.TabBar.H + 2,
                                          cw - 12, ch - 84, self.kitty_buffer))
            self.panels[self.raw_tab].append(self.ta)
        self.b_ok = self.add(W.Button(cw - 244, ch - 33, 72, 23, "OK",
                                      default=True,
                                      cb=lambda: self._apply(close=True)))
        self.b_cancel = self.add(W.Button(cw - 164, ch - 33, 72, 23,
                                          "Cancel", cb=self.close))
        self.b_apply = self.add(W.Button(cw - 84, ch - 33, 72, 23, "Apply",
                                         cb=self._apply))
        self.b_sounds = self.add(W.Button(
            10, ch - 33, 84, 23, "Sounds…", icon="soundcp",
            cb=lambda: self.desk.shell.open_app("soundcp")))
        self.status = self.add(W.Label(102, ch - 28, "", font=T.SMALL,
                                       color=T.SHADOW))
        self._cur_tab = 0
        self._populate()
        self._switch_tab(0)

    def on_resize(self):
        cw, ch = self.client_size()
        self.tabs.w = cw - 12
        if not self.batty_mode:
            self.ta.w, self.ta.h = cw - 12, ch - 84
        for b, dx in ((self.b_ok, 244), (self.b_cancel, 164),
                      (self.b_apply, 84)):
            b.x, b.y = cw - dx, ch - 33
        self.b_sounds.y = ch - 33
        self.status.y = ch - 28

    def draw_client(self, d, img):
        if self.tabs.active != self.raw_tab:
            cw, ch = self.client_size()
            T.raised(d, 6, self.tabs.y + W.TabBar.H - 2, cw - 7, ch - 44)
            # redraw widgets over the panel face happens in the widget pass;
            # the panel is drawn first because draw_client precedes widgets

    def _switch_tab(self, i):
        if self._cur_tab == self.raw_tab:
            self.kitty_buffer = self.ta.text()   # keep raw edits made on the conf tab
            self.buffer = self.kitty_buffer
        else:
            self._form_to_buffer()
        if i != self.raw_tab:
            self._populate()
        else:
            self._form_to_buffer()
            self.ta.set_text(self.kitty_buffer)
        self._cur_tab = i
        for tab_i, panel in enumerate(self.panels):
            for wdg in panel:
                wdg.visible = tab_i == i
        vis = [w for w in self.panels[i] if w.focusable and w.visible]
        self.set_focus(vis[0] if vis else None)
        self.invalidate()

    # form <-> buffer -----------------------------------------------------
    def _current_value(self, spec):
        if spec.source == "shared":
            return self.shared_values.get(spec.key)
        if spec.source == "env":
            return get_env_key(self.env_buffer, spec.key)
        if spec.source == "line":
            return "yes" if has_line(self.kitty_buffer, spec.extra) else "no"
        return get_key(self.kitty_buffer, spec.key)

    def _populate(self):
        for key, (kind, wd) in self.fields.items():
            spec = self.field_specs[key]
            val = self._current_value(spec)
            if kind == "bool":
                wd.checked = _bool_checked(spec, val)
            elif kind == "choice":
                if val is not None and val not in wd.options:
                    wd.options.append(val)   # keep a valid non-listed value
                shown = val if val is not None else _field_default(spec)
                if shown in wd.options:
                    wd.index = wd.options.index(shown)
            else:
                shown = val if val is not None else _field_default(spec)
                wd.set(shown if shown is not None else "")

    def _form_to_buffer(self):
        # only rewrite a key when its value actually changed, so keys absent
        # from the file stay absent and untouched values keep their formatting
        for key, (kind, wd) in self.fields.items():
            spec = self.field_specs[key]
            cur = self._current_value(spec)
            default = _field_default(spec)
            if kind == "bool":
                true_value, false_value = _bool_values(spec)
                v = true_value if wd.checked else false_value
                if wd.checked == _bool_checked(spec, cur):
                    continue
            elif kind == "choice":
                v = wd.value
                if v == (cur if cur is not None else default):
                    continue
            else:
                v = wd.text.strip()
                if spec.source == "env" and not v:
                    if cur is not None:
                        self.env_buffer = unset_env_key(self.env_buffer, key)
                    continue
                if not v or v == (cur if cur is not None else default):
                    continue
            if spec.source == "env":
                self.env_buffer = set_env_key(self.env_buffer, key, v)
            elif spec.source == "shared":
                self.shared_values[key] = v
                self.shared_changes[key] = v
            elif spec.source == "line":
                self.kitty_buffer = set_line(self.kitty_buffer, spec.extra) if wd.checked else unset_line(self.kitty_buffer, spec.extra)
                self.buffer = self.kitty_buffer
            else:
                self.kitty_buffer = set_key(self.kitty_buffer, key, v)
                self.buffer = self.kitty_buffer

    def _install_voice_model(self):
        model = self.fields[shared_settings.VOICE_STT_MODEL_KEY][1].value
        try:
            engine = shared_settings.stt_engine_for_model(model)
        except ValueError as error:
            self.status.set(str(error))
            self.invalidate()
            return False
        engine_widget = self.fields[shared_settings.VOICE_STT_ENGINE_KEY][1]
        engine_widget.index = engine_widget.options.index(engine)

        kilix = os.path.join(_shell.KILIX_HOME, "kilix")
        if os.path.isfile(kilix) and os.access(kilix, os.X_OK):
            target = [kilix, "stt"]
        elif executable := shutil.which("kilix-stt"):
            target = [executable]
        else:
            wm.msgbox(
                self.desk, "Speech model installer",
                "Neither the Kilix launcher nor kilix-stt could be found.",
                icon="error")
            return False
        opened = self.desk.shell._tab(
            [*target, "--install", model, "--default", model],
            f"Install speech model · {model}", os.path.expanduser("~"))
        self.status.set(
            "Installer opened; it will use the model after verification."
            if opened
            else "Could not open the model installer.")
        self.invalidate()
        return bool(opened)

    # apply ----------------------------------------------------------------
    def _apply(self, close=False):
        old_env = self._last_saved_env_buffer
        if self.tabs.active == self.raw_tab:
            self.kitty_buffer = self.ta.text()
            self.buffer = self.kitty_buffer
        else:
            self._form_to_buffer()
        shared_changed = bool(self.shared_changes)
        if not self.batty_mode:
            try:
                _atomic_write_private(self.path, self.kitty_buffer)
            except OSError as e:
                wm.msgbox(self.desk, "kilix Settings", f"Cannot write config:\n{e}",
                          icon="error")
                return
        if not self.batty_mode and (self.env_buffer != old_env or os.path.exists(self.env_path)):
            try:
                _atomic_write_private(self.env_path, self.env_buffer)
                self._last_saved_env_buffer = self.env_buffer
            except OSError as e:
                wm.msgbox(self.desk, "kilix Settings",
                          f"Cannot write runtime config:\n{e}", icon="error")
                return
        if self.shared_changes:
            try:
                shared_settings.update(self.shared_changes, self.shared_path)
                self.shared_changes.clear()
            except (OSError, KeyError) as e:
                wm.msgbox(self.desk, "kilix Settings",
                          f"Cannot write shared settings:\n{e}", icon="error")
                return
        self._apply_env_live()
        msg = self._reload_live()
        if self.env_buffer != old_env:
            msg += " Runtime settings saved for new launches."
        if shared_changed:
            msg += " Shared chrome/game settings updated."
        self.status.set(msg)
        self.invalidate()
        if close:
            self.close()

    def _apply_env_live(self):
        env_specs = [s for spec in self.page_specs for s in spec
                     if s.source == "env"]
        for spec in env_specs:
            val = get_env_key(self.env_buffer, spec.key)
            if val is None or val == "":
                os.environ.pop(spec.key, None)
            else:
                os.environ[spec.key] = val
        if hasattr(self.desk, "saver_idle"):
            try:
                self.desk.saver_idle = float(
                    os.environ.get("KILIX_SAVER_IDLE") or 180)
            except ValueError:
                self.desk.saver_idle = 180.0

    def _reload_live(self):
        if self.batty_mode:
            try:
                r = subprocess.run([os.path.join(_shell.KILIX_HOME, "kilix"),
                                    "reload-settings"], capture_output=True, timeout=5)
                if r.returncode == 0:
                    return "Saved — Batty shared settings applied live."
            except (OSError, subprocess.TimeoutExpired):
                pass
            return "Saved, but Batty could not reload shared settings."
        kitten = self.desk.shell._kitten()
        if kitten and os.environ.get("KITTY_LISTEN_ON"):
            try:
                r = subprocess.run([kitten, "@", "action", "load_config_file"],
                                   capture_output=True, timeout=5)
                if r.returncode == 0:
                    return "Saved — kilix config reloaded live."
            except (OSError, subprocess.TimeoutExpired):
                pass
        pid = os.environ.get("KITTY_PID", "")
        if pid.isdigit():
            try:
                os.kill(int(pid), signal.SIGUSR1)
                return "Saved — reload signaled (SIGUSR1)."
            except (OSError, ProcessLookupError):
                pass
        return "Saved. Reload kilix config with Ctrl+Shift+F5."
