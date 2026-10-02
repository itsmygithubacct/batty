"""kilix desktop — the desktop surface (the shell).

Owns the wallpaper, the icon grid, the launcher files and every "open
something" verb. The desktop folder is a real directory
(under ~/.local/gpu_terminal/kilix/data by default; override with $KILIX_DESKTOP_DIR):
plain files and directories dropped there appear as icons, and "Create
Launcher…" writes freedesktop-style .desktop files there. Programs launch
into new kilix tabs/windows over kitty remote control; X11 apps go through
`kilix run`; URLs through Kilix's ordered real-browser dispatcher.
"""
import configparser
import json
import os
import shutil
import stat
import subprocess

from PIL import Image
from kilix_sdk import content as kilix_content

import icons
import recycle
import theme as T
import widgets as W
import wm
import storage

_here = os.path.dirname(os.path.abspath(__file__))
KILIX_HOME = os.environ.get("KILIX_HOME") or os.path.dirname(_here)

OPEN_MODES = ["kilix tab", "kilix os-window", "kilix fullscreen",
              "kilix run (X11 app)", "web browser"]
MODE_KEYS = {"kilix tab": "tab", "kilix os-window": "window",
             "kilix fullscreen": "fullscreen",
             "kilix run (X11 app)": "run", "web browser": "browse"}
ICON_CHOICES = ["exe", "terminal", "doc", "doc_text", "doc_image", "folder",
                "computer", "browser", "notepad", "settings", "display",
                "drive", "home", "run", "flame"]
NAME_ERROR = "Use a plain name, not a path."

TEXT_EXT = (".txt", ".md", ".rst", ".log", ".conf", ".cfg", ".ini", ".json",
            ".yaml", ".yml", ".toml", ".py", ".sh", ".c", ".h", ".go", ".rs",
            ".js", ".ts", ".html", ".css", ".xml", ".csv", ".diff", ".patch")


def child_path(base, name):
    """Return base/name, but only for a single user-visible path component."""
    if (not name or name in (".", "..") or "\0" in name
            or os.path.isabs(name) or os.path.basename(name) != name
            or (os.path.altsep and os.path.altsep in name)):
        raise ValueError(NAME_ERROR)
    return os.path.join(base, name)


IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".ppm",
           ".tiff")
# what the media player (kilix-amp, via libsndfile) can open, plus common
# audio kinds it will at least try; playlists open there too
AUDIO_EXT = (".mp3", ".flac", ".ogg", ".oga", ".opus", ".wav", ".aiff",
             ".aif", ".aifc", ".m4a", ".aac", ".wma", ".m3u", ".m3u8",
             ".pls")

WALL_COLORS = [("Teal (classic)", (0, 128, 128)), ("Navy", (0, 0, 128)),
               ("Black", (0, 0, 0)), ("Gray", (128, 128, 128)),
               ("Green", (0, 128, 0)), ("Plum", (128, 0, 128)),
               ("Maroon", (128, 0, 0)), ("Steel", (60, 90, 120))]


def _menu_label(s):
    # "-" is MenuItem's separator sentinel; keep user-named entries clickable
    if not s:
        return "(unnamed)"
    return s + " " if s == "-" else s


class Shell:
    def __init__(self, desk):
        self.desk = desk
        self.batty_mode = os.environ.get("BATTY_KILIX_DESKTOP") == "1"
        self.dir = os.path.expanduser(
            os.environ.get("KILIX_DESKTOP_DIR")
            or storage.data_dir("desktop"))
        self.dir = os.path.expanduser(self.dir)
        storage.ensure_private_dir(self.dir)
        self.state_path = os.path.join(self.dir, ".state.json")
        self.state = {"wall_color": list(T.DESKTOP), "wall_image": None,
                      "wall_mode": "stretch", "recent": []}
        try:
            with open(self.state_path) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.state.update(loaded)
        except (OSError, ValueError):
            pass
        self._wall = None             # cached composited wallpaper
        sw, sh = desk.size()
        self.grid = W.IconGrid(0, 0, sw, sh - T.TASKBAR_H,
                               on_activate=self._activate,
                               on_context=self._context, desktop=True)
        self.grid.window = self       # duck-typed: needs .invalidate/.desk
        self.focus = None             # part of the window duck-type
        self.caret_on = False
        self.refresh()

    # window duck-type used by IconGrid
    def invalidate(self):
        self.desk.dirty = True

    def _save_state(self):
        try:
            storage.atomic_write_private(
                self.state_path,
                lambda stream: json.dump(self.state, stream, indent=1))
        except OSError:
            pass

    # ── the icons ───────────────────────────────────────────────────────────
    def refresh(self):
        bin_full = bool(recycle.items())
        items = [
            {"label": "My Computer", "icon": "computer",
             "data": ("builtin", ("mycomp", None))},
            {"label": "Recycle Bin",
             "icon": "recyclebin_full" if bin_full else "recyclebin_empty",
             "data": ("builtin", ("recyclebin", None))},
            {"label": "Home", "icon": "home",
             "data": ("builtin", ("filemgr", os.path.expanduser("~")))},
            {"label": "kilix Settings", "icon": "settings",
             "data": ("builtin", ("settings", None))},
            {"label": "Terminal", "icon": "terminal",
             "data": ("builtin", ("terminal", None))},
            {"label": "Mux Terminal", "icon": "mux",
             "data": ("builtin", ("mux", None))},
        ]
        if self.batty_mode:
            items.append({"label": "Pane Center", "icon": "mux",
                          "data": ("builtin", ("pane-center", None))})
        try:
            names = sorted(os.listdir(self.dir), key=str.lower)
        except OSError:
            names = []
        for n in names:
            if n.startswith("."):
                continue
            p = os.path.join(self.dir, n)
            if n.endswith(".desktop"):
                spec = parse_launcher(p)
                items.append({"label": spec.get("Name") or n[:-8],
                              "icon": spec.get("Icon") or "exe",
                              "shortcut": True, "data": ("launcher", p)})
            else:
                isdir = os.path.isdir(p)
                items.append({"label": n,
                              "icon": icons.for_path(p, isdir),
                              "data": ("path", p)})
        self.grid.set_items(items)
        self.invalidate()

    def dir_changed(self, path):
        """A directory changed on disk — refresh every open view of it: the
        desktop grid and any File Manager window showing the same folder."""
        path = os.path.abspath(os.path.expanduser(path))
        if path == os.path.abspath(self.dir):
            self.refresh()
        for win in list(self.desk.wm.windows):
            if getattr(win, "is_file_window", False) and \
                    os.path.abspath(win.path) == path:
                win.refresh()

    def on_resize(self):
        sw, sh = self.desk.size()
        self.grid.w, self.grid.h = sw, sh - T.TASKBAR_H
        self._wall = None
        self.invalidate()

    # ── drawing ─────────────────────────────────────────────────────────────
    def draw(self, fb, d):
        sw, sh = self.desk.size()
        if self._wall is None or self._wall.size != (sw, sh):
            self._wall = self._build_wall(sw, sh)
        fb.paste(self._wall, (0, 0))
        self.grid.draw(d, fb)

    def _build_wall(self, sw, sh):
        img = Image.new("RGB", (sw, sh), tuple(self.state["wall_color"]))
        path = self.state.get("wall_image")
        if path:
            try:
                pic = Image.open(os.path.expanduser(path)).convert("RGB")
                mode = self.state.get("wall_mode", "stretch")
                if mode == "stretch":
                    img.paste(pic.resize((sw, sh - T.TASKBAR_H)), (0, 0))
                elif mode == "tile":
                    for ty in range(0, sh, pic.height):
                        for tx in range(0, sw, pic.width):
                            img.paste(pic, (tx, ty))
                else:                 # center
                    img.paste(pic, ((sw - pic.width) // 2,
                                    (sh - T.TASKBAR_H - pic.height) // 2))
            except OSError:
                pass
        return img

    # ── input ───────────────────────────────────────────────────────────────
    def on_mouse(self, gev):
        return self.grid.on_mouse(gev)

    def on_key(self, ev):
        if self.grid.on_key(ev):
            return True
        if ev.key == "F5":
            self.refresh()
            return True
        if ev.key == "Delete":
            sel = self.grid.selected_items()
            if sel:
                self._delete_items(sel)
            return True
        if ev.key == "F2":
            sel = self.grid.selected_items()
            if sel:
                self._rename_item(sel[0])
            return True
        return False

    # ── activation / context menus ──────────────────────────────────────────
    def _activate(self, item):
        kind, arg = item["data"]
        if kind == "builtin":
            app, param = arg
            if app == "terminal":
                self.open_terminal()
            elif app == "mux":
                self.open_mux_terminal()
            elif app == "pane-center":
                self.open_pty_manager()
            else:
                self.open_app(app, param)
        elif kind == "launcher":
            self.launch(parse_launcher(arg), arg)
        else:
            self.open_path(arg)

    def _context(self, item, ev):
        MI, sep = W.MenuItem, W.sep
        if item is not None:
            kind, arg = item["data"]
            items = [MI("Open", action=lambda: self._activate(item))]
            if kind == "launcher":
                items.append(MI("Edit Launcher…",
                                action=lambda: self.create_launcher_dialog(
                                    edit_path=arg)))
            if kind == "path" and not os.path.isdir(arg):
                items.append(MI("Open with Notepad", icon="notepad",
                                action=lambda: self.open_app("notepad", arg)))
            if kind in ("launcher", "path"):
                items += [sep(),
                          MI("Rename…", action=lambda: self._rename_item(item)),
                          MI("Delete…", action=lambda: self._delete_items(
                              self._sel_or_one(item)))]
            if kind == "path":
                items.append(MI("Create Launcher…", icon="exe",
                                action=lambda: self.create_launcher_dialog(
                                    prefill_cmd=shell_quote(arg))))
        else:
            items = [
                MI("New Launcher…", icon="exe",
                   action=self.create_launcher_dialog),
                MI("New Folder…", icon="folder", action=self._new_folder),
                MI("New Text File…", icon="doc_text", action=self._new_file),
                sep(),
                MI("Refresh", action=self.refresh),
                MI("Open Desktop Folder", icon="folder_open",
                   action=lambda: self.open_app("filemgr", self.dir)),
                sep(),
                MI("Display…", icon="display", action=self.display_properties),
                MI("Sounds…", icon="soundcp",
                   action=lambda: self.open_app("soundcp")),
                MI(f"About {T.PRODUCT_NAME}…", icon="flame", action=self.about_dialog),
            ]
        self.desk.menus.open(items, ev.x, ev.y)

    # ── file ops on the desktop folder ──────────────────────────────────────
    def _sel_or_one(self, item):
        # a context action on a still-selected icon acts on the whole selection
        sel = self.grid.selected_items()
        return sel if item in sel else [item]

    def _writable(self, item):
        return item["data"][0] in ("launcher", "path")

    def _rename_item(self, item):
        if not self._writable(item):
            wm.msgbox(self.desk, "Desktop", "System icons cannot be renamed.",
                      icon="info")
            return
        kind, path = item["data"]

        def do(name):
            if not name:
                return
            if kind == "launcher":
                spec = parse_launcher(path)
                spec["Name"] = name
                try:
                    write_launcher(path, spec)
                except OSError as e:
                    wm.msgbox(self.desk, "Rename", str(e), icon="error")
                    return
            else:
                try:
                    target = child_path(self.dir, name)
                except ValueError as e:
                    wm.msgbox(self.desk, "Rename", str(e), icon="error")
                    return
                if (os.path.lexists(target)
                        and os.path.abspath(target) != os.path.abspath(path)):
                    wm.msgbox(self.desk, "Rename",
                              f"'{name}' already exists.", icon="error")
                    return
                try:
                    os.rename(path, target)
                except OSError as e:
                    wm.msgbox(self.desk, "Rename", str(e), icon="error")
            self.dir_changed(self.dir)

        wm.inputbox(self.desk, "Rename", "New name:", item["label"], cb=do)

    def _delete_items(self, sel):
        real = [i for i in sel if self._writable(i)]
        if not real:
            wm.msgbox(self.desk, "Desktop", "System icons cannot be deleted.",
                      icon="info")
            return
        names = ", ".join(i["label"] for i in real[:4]) + (
            "…" if len(real) > 4 else "")

        def do(answer):
            if answer != "Yes":
                return
            for it in real:
                p = it["data"][1]
                try:
                    recycle.send(p)
                except (OSError, shutil.Error) as e:
                    wm.msgbox(self.desk, "Delete", str(e), icon="error")
            self.dir_changed(self.dir)

        wm.msgbox(self.desk, "Confirm Delete",
                  f"Send {names} to the Recycle Bin?",
                  icon="warn", buttons=("Yes", "No"), default=1, cb=do)

    def _new_folder(self):
        def do(name):
            if name:
                try:
                    os.makedirs(child_path(self.dir, name), exist_ok=False)
                except (OSError, ValueError) as e:
                    wm.msgbox(self.desk, "New Folder", str(e), icon="error")
                self.dir_changed(self.dir)
        wm.inputbox(self.desk, "New Folder", "Folder name:", "New Folder",
                    cb=do, icon="folder")

    def _new_file(self):
        def do(name):
            if name:
                try:
                    p = child_path(self.dir, name)
                    open(p, "x").close()
                except (OSError, ValueError) as e:
                    wm.msgbox(self.desk, "New File", str(e), icon="error")
                self.dir_changed(self.dir)
        wm.inputbox(self.desk, "New Text File", "File name:", "New File.txt",
                    cb=do, icon="doc_text")

    # ── launchers ───────────────────────────────────────────────────────────
    def launcher_menu_items(self):
        out = []
        try:
            names = sorted(os.listdir(self.dir), key=str.lower)
        except OSError:
            names = []
        for n in names:
            if not n.endswith(".desktop"):
                continue
            p = os.path.join(self.dir, n)
            spec = parse_launcher(p)
            out.append(W.MenuItem(_menu_label(spec.get("Name") or n[:-8]),
                                  icon=spec.get("Icon") or "exe",
                                  action=lambda s=spec, p=p: self.launch(s, p)))
        return out

    def create_launcher_dialog(self, prefill_cmd=None, edit_path=None):
        """The Create Launcher wizard (also edits existing launchers)."""
        desk = self.desk
        spec = parse_launcher(edit_path) if edit_path else {}
        win = wm.Window(desk, "Edit Launcher" if edit_path
                        else "Create Launcher", 380, 262, icon="exe",
                        resizable=False, modal=True)
        cw = win.client_size()[0]
        y = 12
        win.add(W.Label(12, y + 3, "Name:"))
        f_name = win.add(W.TextField(90, y, cw - 102,
                                     spec.get("Name", "")))
        y += 28
        win.add(W.Label(12, y + 3, "Command:"))
        cmd0 = spec.get("URL") or spec.get("Exec") or prefill_cmd or ""
        f_cmd = win.add(W.TextField(90, y, cw - 136, cmd0))

        def browse_cmd():
            import filedialog
            filedialog.open_file(desk, "Choose Program",
                                 lambda p: p and f_cmd.set(shell_quote(p)))
        win.add(W.Button(cw - 44, y - 1, 32, 23, "…", cb=browse_cmd))
        y += 28
        win.add(W.Label(12, y + 3, "Start in:"))
        f_dir = win.add(W.TextField(90, y, cw - 136, spec.get("Path", "")))

        def browse_dir():
            import filedialog
            filedialog.pick_folder(desk, "Start in Folder",
                                   lambda p: p and f_dir.set(p),
                                   start=f_dir.text or None)
        win.add(W.Button(cw - 44, y - 1, 32, 23, "…", cb=browse_dir))
        y += 28
        win.add(W.Label(12, y + 3, "Open in:"))
        mode0 = spec.get("X-Kilix-Open", "tab")
        if spec.get("Type") == "Link":
            mode0 = "browse"
        rev = {v: k for k, v in MODE_KEYS.items()}
        d_mode = win.add(W.Dropdown(90, y, cw - 102, OPEN_MODES,
                                    OPEN_MODES.index(rev.get(mode0,
                                                             "kilix tab"))))
        y += 28
        win.add(W.Label(12, y + 3, "Icon:"))
        icon0 = spec.get("Icon", "exe")
        d_icon = win.add(W.Dropdown(90, y, cw - 150, ICON_CHOICES,
                                    ICON_CHOICES.index(icon0)
                                    if icon0 in ICON_CHOICES else 0))

        class _Preview(W.Widget):
            def __init__(self):
                super().__init__(cw - 36, y - 6, 32, 32)

            def draw(self, d, img):
                icons.paint(img, d_icon.value, self.x, self.y, 32,
                            shortcut=True)

        win.add(_Preview())
        d_icon.cb = lambda *_: win.invalidate()
        y += 34
        hint = "Command runs in a new kilix tab; pick another mode above."
        win.add(W.Label(12, y, hint, font=T.SMALL, color=T.SHADOW))

        def save():
            name = f_name.text.strip() or "Launcher"
            cmd = f_cmd.text.strip()
            if not cmd:
                wm.msgbox(desk, "Create Launcher",
                          "A command (or URL) is required.", icon="warn")
                return
            mode = MODE_KEYS[d_mode.value]
            out = {"Name": name, "Icon": d_icon.value}
            if mode == "browse":
                out.update({"Type": "Link", "URL": cmd})
            else:
                out.update({"Type": "Application", "Exec": cmd,
                            "X-Kilix-Open": mode})
                if f_dir.text.strip():
                    out["Path"] = f_dir.text.strip()
            path = edit_path or unique_path(
                os.path.join(self.dir, safe_name(name) + ".desktop"))
            try:
                write_launcher(path, out)
            except OSError as e:
                wm.msgbox(desk, "Create Launcher", str(e), icon="error")
                return
            win.close()
            self.dir_changed(self.dir)

        ch = win.client_size()[1]
        win.add(W.Button(cw - 164, ch - 33, 72, 23, "OK", cb=save,
                         default=True))
        win.add(W.Button(cw - 84, ch - 33, 72, 23, "Cancel", cb=win.close))
        win.set_focus(f_name)
        desk.wm.add(win)

    def launch(self, spec, path=None):
        name = spec.get("Name") or "app"
        if spec.get("Type") == "Link" or spec.get("URL"):
            self.open_url(spec.get("URL"))
            return
        cmd = spec.get("Exec", "")
        if not cmd:
            wm.msgbox(self.desk, name, "Launcher has no Exec line.",
                      icon="error")
            return
        mode = spec.get("X-Kilix-Open")
        if not mode:
            terminal = str(spec.get("Terminal", "")).strip().lower() == "true"
            mode = "tab" if terminal else "run"
        cwd = os.path.expanduser(spec.get("Path") or "~")
        argv = desktop_exec_argv(spec, path)
        if not argv:
            wm.msgbox(self.desk, name, "Launcher has an invalid Exec line.",
                      icon="error")
            return
        sanitized_cmd = " ".join(shell_quote(arg) for arg in argv)
        if mode == "run":
            self.open_x11_tab(argv, name, cwd=cwd)
        elif mode == "window":
            self._spawn_kitty_launch(
                ["--type=os-window"], sanitized_cmd, name, cwd)
        elif mode == "fullscreen":
            # X11 app filling the whole screen, on a private Xvfb (XPane)
            self.open_in_xpane(argv, name, cwd=cwd,
                               app_size=self.desk.size())
        else:
            self._spawn_kitty_launch(
                ["--type=tab"], sanitized_cmd, name, cwd)

    # ── spawning into kilix ─────────────────────────────────────────────────
    def _kitten(self):
        k = os.environ.get("KILIX_KITTEN")
        if k and os.access(k, os.X_OK):
            return k
        if self.batty_mode:
            return None
        storage_home = os.environ.get(
            "KILIX_STORAGE_HOME", os.path.expanduser(
                "~/.local/gpu_terminal/kilix"))
        for cand in (os.path.join(storage_home,
                                  "build/current/src/kitty/launcher/kitten"),
                     os.path.join(storage_home, "prebuilt/kitty.app/bin/kitten"),
                     shutil.which("kitten")):
            if cand and os.access(cand, os.X_OK):
                return cand
        return None

    def _kitten_remote(self, kitten, command):
        argv = [kitten, "@"]
        password_file = os.environ.get("KILIX_RC_PASSWORD_FILE")
        if password_file:
            argv.extend(["--password-file", password_file])
        argv.append(command)
        return argv

    def _spawn_kitty_launch(self, opts, cmd, title, cwd=None,
                            pause_on_error=True):
        """Run shell command `cmd` in a new kilix tab/window."""
        kitten = self._kitten()
        if not kitten or not os.environ.get("KITTY_LISTEN_ON"):
            wm.msgbox(self.desk, "kilix",
                      "Cannot reach kilix remote control\n"
                      "(KITTY_LISTEN_ON is not set).", icon="error")
            return False
        script = cmd
        if pause_on_error:
            script = (f'{cmd}; rc=$?; [ $rc -ne 0 ] && '
                      f'{{ echo; echo "[exit $rc -- Enter to close]"; '
                      f'read -r _; }}; exit $rc')
        argv = [*self._kitten_remote(kitten, "launch"), *opts,
                "--self", "--tab-title", title,
                "--cwd", cwd or os.path.expanduser("~"), "--",
                "bash", "-lc", script]
        return self._popen(argv)

    def _tab(self, argv, title, cwd=None, env=None):
        kitten = self._kitten()
        if not kitten or not os.environ.get("KITTY_LISTEN_ON"):
            wm.msgbox(self.desk, "kilix", "Cannot reach kilix remote control\n"
                      "(KITTY_LISTEN_ON is not set).", icon="error")
            return False
        env_args = []
        for key, value in (env or {}).items():
            env_args.extend(["--env", f"{key}={value}"])
        return self._popen([*self._kitten_remote(kitten, "launch"),
                            "--type=tab", "--self", "--tab-title",
                            title, "--cwd", cwd or os.path.expanduser("~"),
                            *env_args, "--"]
                           + argv)

    def open_x11_tab(self, argv, title, cwd=None, fill=False):
        run = [os.path.join(KILIX_HOME, "kilix"), "run"]
        if fill:
            run.append("--fill")
        return self._tab(run + list(argv), title, cwd, env={
            "KILIX_IN_OVERLAY": "1",
            "KILIX_STREAM": os.environ.get("KILIX_STREAM", ""),
        })

    def _popen(self, argv, cwd=None):
        try:
            subprocess.Popen(argv, cwd=cwd, start_new_session=True,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL)
        except OSError as e:
            wm.msgbox(self.desk, "kilix", f"Launch failed:\n{e}",
                      icon="error")
            return False
        return True

    def open_terminal(self, cwd=None):
        kitten = self._kitten()
        if not kitten or not os.environ.get("KITTY_LISTEN_ON"):
            wm.msgbox(self.desk, "kilix", "Cannot reach kilix remote control\n"
                      "(KITTY_LISTEN_ON is not set).", icon="error")
            return False
        return self._popen([*self._kitten_remote(kitten, "launch"),
                            "--type=tab", "--self", "--tab-title",
                            "Terminal", "--cwd", cwd or os.path.expanduser("~")])

    def open_mux_terminal(self, session="main", cwd=None):
        session = session or "main"
        kilix = os.path.join(KILIX_HOME, "kilix")
        if self.batty_mode:
            return self._popen([kilix, "serve", session])
        return self._tab([kilix, "serve", session],
                         f"Mux: {session}", cwd or os.path.expanduser("~"))

    def open_kilix_pdf(self, path=None):
        """Choose or open a PDF in the catalog viewer."""
        if path:
            return self.open_catalog_application(
                "kilix-pdf", action="open", arguments=(path,))

        import filedialog

        return filedialog.open_file(
            self.desk, "Open PDF",
            lambda selected: selected and self.open_kilix_pdf(selected),
            filters=[("PDF Documents", "*.pdf"), ("All Files", "*.*")])

    def open_catalog_application(self, content_id, action=None, arguments=()):
        """Render any shared catalog application as a managed desktop box."""
        kilix = os.path.join(KILIX_HOME, "kilix")
        try:
            plan = kilix_content.application_plan(
                content_id, "window", arguments, launcher=kilix, action=action)
        except (kilix_content.CatalogError, ValueError) as error:
            wm.msgbox(self.desk, "Kilix Application", str(error), icon="error")
            return False
        if plan.single_instance:
            for window in self.desk.wm.windows:
                if getattr(window, "kilix_application_id", "") == content_id:
                    self.desk.wm.activate(window)
                    return True
        argv = plan.argv
        if self.batty_mode and content_id == "dosbox":
            # The shared SDK's custom host verb targets the reference Kilix
            # games launcher. Batty owns the DOSBox adapter through `app`.
            argv = (kilix, "app", "window", "dosbox")
        return self.open_in_xpane(
            argv, plan.label, icon=plan.icon,
            app_size=plan.preferred_size, cwd=os.path.expanduser("~"),
            application_id=content_id, fill=plan.launch_mode != "xpane")

    def catalog_application_menu_items(self):
        """Start-menu rows derived from the SDK's plain catalog records."""
        try:
            records = kilix_content.menu_records()
        except Exception:                         # an unreadable catalog is empty
            return []
        return [
            W.MenuItem(
                record["label"],
                icon="exe",
                action=lambda content_id=record["id"]:
                self.open_catalog_application(content_id),
            )
            for record in records
            if record.get("kind") == "app"
        ]

    @staticmethod
    def kilix_temps_target():
        if os.environ.get("BATTY_KILIX_DESKTOP") == "1":
            return [os.path.join(KILIX_HOME, "kilix"), "temps", "--graphics"], None
        if executable := shutil.which("kilix-temps"):
            return [executable, "--graphics"], None
        source_home = os.environ.get("GPU_TERMINAL_SOURCE_HOME") or \
            os.path.expanduser("~/.local/gpu_terminal/sources")
        project = os.path.join(
            os.path.abspath(os.path.expanduser(source_home)), "kilix-desktops",
            "kilix-tui-utils")
        entry = os.path.join(project, "tools", "temps", "main.py")
        if os.path.isfile(entry):
            return ["python3", entry, "--graphics"], project
        kilix = os.path.join(KILIX_HOME, "kilix")
        if os.environ.get("BATTY_KILIX_DESKTOP") != "1" and \
                os.path.isfile(kilix) and os.access(kilix, os.X_OK):
            return [kilix, "temps", "--graphics"], None
        return None

    def open_kilix_temps(self):
        target = self.kilix_temps_target()
        if target is not None:
            argv, cwd = target
            return self._tab(argv, "Kilix Temps", cwd)
        wm.msgbox(
            self.desk, "Kilix Temps",
            "Neither an installed Kilix Temps dashboard nor the pinned Kilix "
            "installer could be found.", icon="error")
        return False

    @staticmethod
    def kilix_memory_target():
        if os.environ.get("BATTY_KILIX_DESKTOP") == "1":
            return [os.path.join(KILIX_HOME, "kilix"), "memory", "--graphics"], None
        if executable := shutil.which("kilix-memory"):
            return [executable, "--graphics"], None
        source_home = os.environ.get("GPU_TERMINAL_SOURCE_HOME") or \
            os.path.expanduser("~/.local/gpu_terminal/sources")
        project = os.path.join(
            os.path.abspath(os.path.expanduser(source_home)), "kilix-desktops",
            "kilix-tui-utils")
        entry = os.path.join(project, "tools", "memory", "main.py")
        if os.path.isfile(entry):
            return ["python3", entry, "--graphics"], project
        kilix = os.path.join(KILIX_HOME, "kilix")
        if os.environ.get("BATTY_KILIX_DESKTOP") != "1" and \
                os.path.isfile(kilix) and os.access(kilix, os.X_OK):
            return [kilix, "memory", "--graphics"], None
        return None

    def open_kilix_memory(self):
        target = self.kilix_memory_target()
        if target is not None:
            argv, cwd = target
            return self._tab(argv, "Kilix Memory", cwd)
        wm.msgbox(
            self.desk, "Kilix Memory",
            "Neither an installed Kilix Memory dashboard nor the unified "
            "Kilix utilities checkout could be found.", icon="error")
        return False

    @staticmethod
    def pty_manager_target():
        """Use Kilix so the manager shares its private, pinned broker runtime."""
        kilix = os.path.join(KILIX_HOME, "kilix")
        if os.path.isfile(kilix) and os.access(kilix, os.X_OK):
            return [kilix, "pty"]
        return None

    def open_pty_manager(self):
        if self.batty_mode:
            return self._popen([os.path.join(KILIX_HOME, "kilix"), "panes"])
        target = self.pty_manager_target()
        if target is not None:
            return self._tab(
                target,
                "PTY Sessions",
                os.path.expanduser("~"),
                env={"KITTY_PTY_BROKER_BYPASS": "1"},
            )
        wm.msgbox(
            self.desk, "PTY Sessions",
            "The Kilix persistent-session manager could not be found.",
            icon="error")
        return False

    @staticmethod
    def tmux_manager_target():
        """Installed manager first; the Kilix pinned installer is the fallback."""
        if os.environ.get("BATTY_KILIX_DESKTOP") == "1":
            return [os.path.join(KILIX_HOME, "kilix"), "tmux"]
        if executable := shutil.which("tmux-tui"):
            return [executable]
        kilix = os.path.join(KILIX_HOME, "kilix")
        if os.environ.get("BATTY_KILIX_DESKTOP") != "1" and \
                os.path.isfile(kilix) and os.access(kilix, os.X_OK):
            return [kilix, "tmux"]
        return None

    def open_tmux_manager(self):
        target = self.tmux_manager_target()
        if target is not None:
            return self._tab(target, "Tmux Manager", os.path.expanduser("~"))
        wm.msgbox(
            self.desk, "Tmux Manager",
            "Neither an installed Tmux Manager nor the pinned Kilix installer "
            "could be found.", icon="error")
        return False

    def run_maintenance(self, cmd, title):
        """Run an update/maintenance command in a new kilix tab, pausing at the
        end so its output — and any prompt, e.g. `pleb update`'s restart
        question — stays readable. Backs the Start ▸ System launchers."""
        done = f"== {title} finished -- press Enter to close =="
        self._spawn_kitty_launch(
            ["--type=tab"],
            f'{cmd}; rc=$?; echo; printf "%s\\n" {shell_quote(done)}; '
            f'read -r _; exit $rc',
            title,
            pause_on_error=False)

    def open_url(self, url):
        if url is None:
            wm.inputbox(self.desk, "Web Browser", "Address:", "https://",
                        cb=lambda u: u and self.open_url(u), icon="browser")
            return
        self.open_default_browser_tab(url, "browser")

    def open_default_browser_tab(self, url, title="Browser"):
        """Open a URL in a browser contained by a Kilix tab."""
        if not url:
            return False
        browser = self._first_on_path(self.REAL_BROWSER_CANDS)
        if browser:
            return self.open_x11_tab(
                self._browser_argv(browser, url), title or "Browser",
                fill=True)
        return self._tab(
            [os.path.join(KILIX_HOME, "kilix"), "open-url", url],
            title or "Browser", None, env={"KILIX_IN_OVERLAY": "1"})

    FIREFOX_CANDS = ("firefox-esr", "firefox")
    CHROME_CANDS = ("google-chrome", "google-chrome-stable", "chromium",
                    "chromium-browser")
    REAL_BROWSER_CANDS = CHROME_CANDS + FIREFOX_CANDS
    BROWSER_HOME = "https://duckduckgo.com/"

    @staticmethod
    def _first_on_path(cands):
        for c in cands:
            p = shutil.which(c)
            if p:
                return p
        return None

    @staticmethod
    def _browser_argv(browser, url):
        name = os.path.basename(browser)
        if name.startswith("firefox"):
            return [browser, "--no-remote", url]
        return [browser, url]

    def open_browser(self, which="firefox", mode=None, url=None):
        """Launch a web browser from the desktop.

        Firefox and Chromium open in a filled kilix-run tab by default so they
        stay inside the terminal pane. mode overrides: "window", "tab",
        "fullscreen".
        """
        url = url or self.BROWSER_HOME
        if which == "chromium":
            chromium = self._first_on_path(self.CHROME_CANDS)
            if not chromium:
                wm.msgbox(self.desk, "Chromium", "Chromium is not installed.",
                          icon="error")
                return
            mode = mode or "tab"
            if mode == "tab":
                self.open_x11_tab(
                    self._browser_argv(chromium, url), "Chromium", fill=True)
            else:                           # GUI chromium (works where GL does)
                self._browser_window(
                    self._browser_argv(chromium, url),
                    "Chromium", mode)
            return
        # firefox — the default browser
        ff = self._first_on_path(self.FIREFOX_CANDS)
        if not ff:
            wm.msgbox(self.desk, "Firefox", "Firefox is not installed.",
                      icon="error")
            return
        mode = mode or "tab"
        argv = [ff, "--no-remote", url]
        if mode == "tab":
            self.open_x11_tab(argv, "Firefox", fill=True)
        else:                               # window / fullscreen
            self._browser_window(argv, "Firefox", mode)

    def _browser_window(self, argv, title, mode):
        """Open a GUI browser as an XPane desktop window, sized per mode."""
        import app_profiles
        try:
            argv, temporary_profile = app_profiles.prepare_app_command(argv)
        except (OSError, RuntimeError) as error:
            wm.msgbox(self.desk, title, f"Could not prepare browser:\n{error}",
                      icon="error")
            return False
        cleanup = lambda: app_profiles.cleanup_app_profile(temporary_profile)
        sw, sh = self.desk.size()
        size = (sw, sh) if mode == "fullscreen" \
            else (int(sw * 0.82), int(sh * 0.82))
        return self.open_in_xpane(
            argv, title, icon="browser", app_size=size, cleanup=cleanup)

    def open_path(self, path, from_app=None):
        """The desktop's 'what do I do with this file' verb."""
        path = os.path.expanduser(path)
        if os.path.isdir(path):
            self.open_app("filemgr", path)
            return
        try:
            # a FIFO/device would block open(2) on this single-threaded loop
            if not stat.S_ISREG(os.stat(path).st_mode):
                wm.msgbox(self.desk, os.path.basename(path) or path,
                          "This is a special file (pipe or device) and "
                          "cannot be opened.", icon="warn")
                return
        except OSError:
            pass
        low = path.lower()
        if low.endswith(".desktop"):
            self.launch(parse_launcher(path), path)
            return
        if low.endswith((".krt", ".rtf")):
            self.open_app("wordpad", path)
            self.add_recent(path)
            return
        if low.endswith(".pdf"):
            self.open_kilix_pdf(path)
        elif low.endswith(IMG_EXT):
            self.open_app("viewer", path)
        elif low.endswith(AUDIO_EXT):
            self.open_app("amp", path)
        elif low.endswith(TEXT_EXT) or self._looks_texty(path):
            self.open_app("notepad", path)
        elif os.access(path, os.X_OK):
            def do(ans):
                if ans == "Run":
                    self._spawn_kitty_launch(["--type=tab"],
                                             shell_quote(path),
                                             os.path.basename(path))
                elif ans == "Notepad":
                    self.open_app("notepad", path)
            wm.msgbox(self.desk, os.path.basename(path),
                      "This file is executable. Run it in a kilix tab?",
                      icon="question", buttons=("Run", "Notepad", "Cancel"),
                      cb=do)
        else:
            def do2(ans):
                if ans == "Notepad":
                    self.open_app("notepad", path)
            wm.msgbox(self.desk, os.path.basename(path),
                      "No association for this file type.",
                      icon="question", buttons=("Notepad", "Cancel"), cb=do2)
        self.add_recent(path)

    @staticmethod
    def _looks_texty(path):
        try:
            with open(path, "rb") as f:
                chunk = f.read(2048)
            return b"\0" not in chunk
        except OSError:
            return False

    def game_menu_items(self, availability=None):
        """Start ▸ Programs ▸ Games, built from the games.py registry."""
        import games
        return [W.MenuItem(meta["label"], icon=meta["icon"],
                           action=lambda g=name: self.play_game(g))
                for name, meta in games.available_games(availability).items()]

    def system_menu_items(self):
        """Start ▸ System: update + maintenance launchers, each shown only when
        the thing it drives is actually present. Every item runs in a visible tab
        (see run_maintenance)."""
        if self.batty_mode:
            return []
        items = [
            W.MenuItem("Install", icon="folder", submenu=self.install_menu_items()),
            W.MenuItem("Update", icon="folder", submenu=self.update_menu_items()),
        ]
        scripts = self.extra_script_menu_items()
        if scripts:
            items.append(W.MenuItem("Scripts", icon="folder", submenu=scripts))
        return items

    def desktop_script(self, name):
        return os.path.join(_here, "scripts", name)

    def tool_installed(self, name):
        if shutil.which(name):
            return True
        home = os.path.expanduser("~")
        for base in (os.path.join(home, ".local", "bin"),
                     "/usr/local/bin", "/usr/bin"):
            path = os.path.join(base, name)
            if os.path.exists(path) and os.access(path, os.X_OK):
                return True
        return False

    def script_launcher(self, script_name, title, icon="run"):
        script = self.desktop_script(script_name)
        return W.MenuItem(title, icon=icon,
                          action=lambda s=script, t=title:
                          self.run_maintenance(shell_quote(s), t))

    def install_menu_items(self):
        """Start ▸ System ▸ Install."""
        items = [
            self.script_launcher("install-codex.sh", "Install Codex"),
            self.script_launcher("install-google-chrome.sh",
                                 "Install Google Chrome", icon="browser"),
        ]
        deps = "/usr/local/sbin/plebian-os-install-deps"
        if os.path.exists(deps):
            items.append(W.MenuItem("-"))
            items.append(W.MenuItem(
                "Reinstall dependencies", icon="settings",
                action=lambda: self.run_maintenance(f'sudo "{deps}"',
                                                    "Reinstall dependencies")))
        return items

    def update_menu_items(self):
        """Start ▸ System ▸ Update."""
        home = os.path.expanduser("~")
        items = []

        def launcher(cmd, title, icon="run"):
            return W.MenuItem(title, icon=icon,
                              action=lambda c=cmd, t=title:
                              self.run_maintenance(c, t))

        items.append(self.script_launcher("update-system.sh", "Update System",
                                          icon="settings"))
        if self.tool_installed("codex"):
            items.append(self.script_launcher("update-codex.py",
                                              "Update Codex"))

        # kilix, when this is a git checkout with the launcher
        kilix = os.path.join(KILIX_HOME, "kilix")
        if os.path.isdir(os.path.join(KILIX_HOME, ".git")) \
                and os.path.exists(kilix):
            items.append(launcher(f'"{kilix}" update', "Update kilix"))
        # pleb, when the session manager is present
        pleb = os.path.join(home, "pleb", "bin", "pleb")
        if os.path.exists(pleb):
            items.append(launcher(f'"{pleb}" update',
                                  "Update Pleb (kilix + session)"))
        # the whole Plebian-OS stack + dependency reinstall, when installed
        pos = "/usr/local/bin/plebian-os-update"
        if os.path.exists(pos):
            items.append(launcher(pos, "Update Plebian-OS (kilix + pleb)"))

        return items

    def extra_script_menu_items(self):
        """Executable *.sh files under checkout scripts dirs."""
        home = os.path.expanduser("~")
        # any other executable *.sh shipped under the checkouts' scripts/ dirs
        extra = []
        for base in (os.path.join(home, "pleb", "scripts"),
                     os.path.join(KILIX_HOME, "scripts")):
            if not os.path.isdir(base):
                continue
            for fn in sorted(os.listdir(base)):
                fp = os.path.join(base, fn)
                if fn.endswith(".sh") and os.access(fp, os.X_OK):
                    extra.append(W.MenuItem(
                        fn, icon="exe", action=lambda p=fp, n=fn:
                        self.run_maintenance(f'"{p}"', n)))
        return extra

    def play_game(self, game):
        """Plays immediately when games.conf points at a working install;
        otherwise asks once, then a new tab installs the pieces (showing
        progress) and boots the game."""
        import games
        if not games.game_enabled(game):
            wm.msgbox(
                self.desk, "Games",
                "This game is disabled. Re-enable it in "
                "kilix Settings → Games.",
                icon="info")
            return
        meta = games.GAMES[game]

        def go():
            self._tab(["python3", os.path.join(_here, "games.py"), game],
                      meta["label"], None)

        ready = games.game_ready(game)
        if ready:
            go()
            return

        def answered(ans):
            if ans == "Install":
                go()
        wm.msgbox(self.desk, "Games",
                  f"{meta['label']} isn't set up yet.\n\n{meta['blurb']}\n"
                  "(Paths are remembered in Kilix's private config directory.)",
                  icon=meta["icon"], buttons=("Install", "Cancel"),
                  cb=answered)

    def open_app(self, app, arg=None):
        if self.batty_mode:
            try:
                record = kilix_content.default_catalog().require(app)
            except kilix_content.CatalogError:
                pass
            else:
                if record.kind == "app":
                    return self.open_catalog_application(
                        app, arguments=(arg,) if arg is not None else ())
        import apps
        try:
            apps.open(self.desk, app, arg)
        except Exception as e:            # an app must never take the desk down
            wm.msgbox(self.desk, T.PRODUCT_NAME, f"{app}: {e}", icon="error")

    def open_in_xpane(self, argv, title, icon="exe", cwd=None, app_size=None,
                      cleanup=None, application_id="", fill=True):
        """Open an X11 command (already-split argv) as a window ON the desktop,
        the way apps/amp.py runs kilix-amp. app_size (w, h) sizes the private
        Xvfb / window; None fills the desktop (minus the taskbar). An Xvfb/XPane
        failure shows an error dialog — it must never take the desktop down."""
        from apps import xpane
        try:
            pane = xpane.XPane(
                self.desk, list(argv), title, icon=icon, app_size=app_size,
                cwd=cwd or os.path.expanduser("~"), fill=fill,
                cleanup=cleanup)
            pane.kilix_application_id = application_id
            self.desk.wm.add(pane)
            return True
        except Exception as e:
            if cleanup is not None:
                cleanup()
            wm.msgbox(self.desk, title,
                      f"Could not open '{title}' in a window:\n{e}",
                      icon="error")
            return False

    # ── recents ─────────────────────────────────────────────────────────────
    def add_recent(self, path):
        r = [p for p in self.state.get("recent", []) if p != path]
        r.insert(0, path)
        self.state["recent"] = r[:10]
        self._save_state()

    def recent_docs(self):
        return [(_menu_label(os.path.basename(p)), p)
                for p in self.state.get("recent", [])
                if os.path.exists(p)]

    # ── dialogs ─────────────────────────────────────────────────────────────
    def run_dialog(self):
        def do(cmd):
            if cmd:
                self._spawn_kitty_launch(["--type=tab"], cmd,
                                         split_cmd(cmd)[0] if cmd else "run")
        wm.inputbox(self.desk, "Run",
                    "Type the name of a program to open it in a kilix tab:",
                    "", cb=do, icon="run", width=320)

    def change_password_dialog(self):
        """Open a masked change-password dialog backed by the OS helper."""
        import security
        desk = self.desk
        win = wm.Window(desk, "Change Password", 322, 210,
                        icon="warn", resizable=False, modal=True)
        cw = win.client_size()[0]
        win.add(W.Label(12, 12, "Set a new login password for this account."))
        win.add(W.Label(12, 42, "New password:"))
        new = win.add(W.TextField(122, 40, cw - 134, "", mask=True))
        win.add(W.Label(12, 70, "Confirm:"))
        confirm = win.add(W.TextField(122, 68, cw - 134, "", mask=True))
        status = win.add(W.Label(12, 96, "", color=T.SHADOW))

        def fail(message):
            status.set(message)
            win.invalidate()

        def apply(*_):
            if not new.text:
                return fail("Enter a new password.")
            if new.text != confirm.text:
                return fail("The passwords do not match.")
            if new.text == "plebian":
                return fail("Choose something other than the default.")
            ok, message = security.change_password(new.text)
            if ok:
                win.close()
                desk._refresh_password_nag()
                wm.msgbox(desk, "Password Changed", message, icon="info")
            else:
                fail(message[:46])

        new.on_enter = lambda *_: win.set_focus(confirm)
        confirm.on_enter = apply
        win.add(W.Button(cw - 168, 130, 76, 24, "OK", cb=apply,
                         default=True))
        win.add(W.Button(cw - 84, 130, 76, 24, "Cancel", cb=win.close))
        win.set_focus(new)
        desk.wm.add(win)
        return win

    def shutdown_dialog(self):
        desk = self.desk
        win = wm.Window(desk, f"Shut Down {T.PRODUCT_NAME}", 300, 250,
                        icon="shutdown", resizable=False, modal=True)
        cw, ch = win.client_size()
        win.add(W.Label(14, 12, "What do you want to do?"))

        def choose(fn):
            def go():
                win.close()
                fn()
            return go

        # one machine action (power off); the rest act on the desktop session
        opts = [
            ("Shut Down", self._power_off),          # turn the computer off
            ("Restart", self._restart_desktop),      # relaunch the desktop
            ("Exit to Terminal", desk.quit),         # leave the desktop → shell
        ]
        if not self.batty_mode:
            opts.append(("Update and Restart", self._update_and_restart))
        y = 40
        for label, fn in opts:
            win.add(W.Button(14, y, cw - 28, 26, label, cb=choose(fn)))
            y += 32
        win.add(W.Button(cw - 90, y + 6, 76, 24, "Cancel", cb=win.close))
        desk.wm.add(win)

    # ── shutdown actions ────────────────────────────────────────────────────
    def _power_off(self):
        # run in a tab so a permission error (rather than a silent no-op) shows
        self._spawn_kitty_launch(["--type=tab"], "systemctl poweroff",
                                 "Shut Down")

    def _restart_desktop(self):
        """Relaunch the kilix 95 desktop on a fresh process (loads updated
        code), then quit this one — the new tab takes over."""
        kilix = os.path.join(KILIX_HOME, "kilix")
        if self._tab(["env", "KILIX_IN_OVERLAY=1", kilix, "desktop", T.FLAVOR],
                     T.PRODUCT_NAME):
            self.desk.quit()

    def _update_and_restart(self):
        kilix = os.path.join(KILIX_HOME, "kilix")
        if self._spawn_kitty_launch(
            ["--type=tab"],
            f'{self._best_update_command()} && '
            f'exec env KILIX_IN_OVERLAY=1 "{kilix}" desktop',
            "Update and Restart"):
            self.desk.quit()

    def _best_update_command(self):
        """The most complete updater present: the whole-stack script, else
        `pleb update`, else `kilix update`."""
        if os.path.exists("/usr/local/bin/plebian-os-update"):
            return "/usr/local/bin/plebian-os-update"
        pleb = os.path.join(os.path.expanduser("~"), "pleb", "bin", "pleb")
        if os.path.exists(pleb):
            return f'"{pleb}" update'
        return f'"{os.path.join(KILIX_HOME, "kilix")}" update'

    def about_dialog(self):
        wm.msgbox(self.desk, f"About {T.PRODUCT_NAME}",
                  f"{T.PRODUCT_NAME}\nA {T.STYLE_NAME} desktop for kilix.\n\n"
                  "Rendered as pixels over the kitty graphics protocol.\n"
                  "All artwork drawn in-house — no Redmond bits inside.",
                  icon="flame")

    def display_properties(self):
        desk = self.desk
        win = wm.Window(desk, "Display Properties", 380, 250, icon="display",
                        resizable=False, modal=True)
        cw, ch = win.client_size()
        win.add(W.GroupBox(10, 6, cw - 20, 96, "Background color"))
        sw_size, gap = 34, 8
        cur = list(self.state["wall_color"])

        class _Swatch(W.Widget):
            def __init__(s, i, col, x, y):
                super().__init__(x, y, sw_size, sw_size)
                s.col = col

            def draw(s, d, img):
                sel = list(s.col) == cur
                T.sunken(d, s.x, s.y, s.x + s.w - 1, s.y + s.h - 1,
                         fill=s.col)
                if sel:
                    T.focus_rect(d, s.x - 2, s.y - 2, s.x + s.w + 1,
                                 s.y + s.h + 1)

            def on_mouse(s, ev):
                if ev.press:
                    cur[:] = list(s.col)
                    win.invalidate()
                return True

        x = 22
        for i, (name, col) in enumerate(WALL_COLORS):
            win.add(_Swatch(i, col, x, 26))
            x += sw_size + gap
        win.add(W.Label(22, 70, "Wallpaper (image file, optional):"))
        f_img = win.add(W.TextField(22, 108, cw - 166,
                                    self.state.get("wall_image") or ""))

        def browse_img():
            import filedialog
            filedialog.open_file(
                desk, "Choose Wallpaper",
                lambda p: p and f_img.set(p),
                start=os.path.dirname(f_img.text) if f_img.text.strip()
                else None,
                filters=[("Images", "*.png;*.jpg;*.jpeg;*.bmp;*.gif"),
                         ("All Files", "*.*")])
        win.add(W.Button(cw - 140, 107, 30, 23, "…", cb=browse_img))
        modes = ["stretch", "tile", "center"]
        d_mode = win.add(W.Dropdown(cw - 100, 108, 78, modes,
                                    modes.index(self.state.get("wall_mode",
                                                               "stretch"))))

        def apply(close=False):
            self.state["wall_color"] = cur
            self.state["wall_image"] = f_img.text.strip() or None
            self.state["wall_mode"] = d_mode.value
            self._save_state()
            self._wall = None
            self.invalidate()
            if close:
                win.close()

        win.add(W.Button(cw - 244, ch - 33, 72, 23, "OK", default=True,
                         cb=lambda: apply(True)))
        win.add(W.Button(cw - 164, ch - 33, 72, 23, "Cancel", cb=win.close))
        win.add(W.Button(cw - 84, ch - 33, 72, 23, "Apply", cb=apply))
        desk.wm.add(win)


# ── launcher file helpers ────────────────────────────────────────────────────

def parse_launcher(path):
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    out = {}
    try:
        cp.read(path)
        if cp.has_section("Desktop Entry"):
            out = dict(cp["Desktop Entry"])
    except (OSError, ValueError, configparser.Error):  # ValueError: bad UTF-8
        pass
    return out


def write_launcher(path, spec):
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp["Desktop Entry"] = {"Version": "1.0", **spec}
    with open(path, "w") as f:
        cp.write(f)


def safe_name(name):
    return "".join(c if c.isalnum() or c in "-_ ." else "_" for c in name)


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def split_cmd(cmd):
    import shlex
    try:
        return shlex.split(cmd) or [cmd]
    except ValueError:
        return [cmd]


def desktop_exec_argv(spec, desktop_path=None):
    """Expand a freedesktop Exec line when no files/URLs are selected."""
    import shlex
    try:
        words = shlex.split(spec.get("Exec", ""))
    except ValueError:
        return []
    out = []
    unavailable = set("fFuUhdDnNvm")
    for word in words:
        if word == "%i":
            icon = spec.get("Icon")
            if icon:
                out.extend(["--icon", icon])
            continue
        if word == "%c":
            if spec.get("Name"):
                out.append(spec["Name"])
            continue
        if word == "%k":
            if desktop_path:
                out.append(os.path.abspath(desktop_path))
            continue
        expanded = []
        drop = False
        index = 0
        while index < len(word):
            if word[index] != "%" or index + 1 >= len(word):
                expanded.append(word[index])
                index += 1
                continue
            code = word[index + 1]
            if code == "%":
                expanded.append("%")
            elif code in unavailable or code == "i":
                drop = True
                break
            elif code == "c":
                expanded.append(spec.get("Name", ""))
            elif code == "k":
                expanded.append(os.path.abspath(desktop_path)
                                if desktop_path else "")
            else:
                expanded.extend(("%", code))
            index += 2
        value = "".join(expanded)
        if not drop and value:
            out.append(value)
    return out


def shell_quote(s):
    import shlex
    return shlex.quote(s)
