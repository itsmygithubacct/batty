#!/usr/bin/env python3
"""kilix 95 — the Games section: registry + on-demand installers.

`games.py doom` is what Start ▸ Programs ▸ Games ▸ Doom runs (in a new kilix
tab). If Kilix's desktop games config points at a working DOSBox and Doom, it
boots straight in; otherwise it downloads the official shareware episode
(id's doom19s.zip from the idgames mirrors — the shareware episode is freely
redistributable) and, when no dosbox is installed, a dosbox-staging release
build, stores everything under ~/.local/gpu_terminal/kilix/data/desktop-games/,
config, and boots. Nothing is written inside the kilix tree.

No DOS needed for the install: doom19s.zip's DEICE parts (DOOMS_19.1/.2)
concatenate into a self-extracting ZIP that python's zipfile reads directly.

Inside kilix the game runs through `kilix run` (DOSBox on a private X server,
streamed into the pane); on a plain X session it just runs DOSBox.
"""
import configparser
import hashlib
import os
import shutil
import sys
import tarfile
import zipfile

import storage
from kilix_sdk import content as kilix_content
from kilix_sdk import settings as shared_settings

HOME = os.path.expanduser("~")
CONF = storage.config_dir("desktop-games.conf")
GAMES_DIR = storage.data_dir("desktop-games")
APPS_DIR = storage.data_dir("desktop-apps")
KILIX_HOME = (os.environ.get("KILIX_HOME")
              or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DOOM_URLS = [  # idgames mirrors of id Software's shareware installer
    "https://www.gamers.org/pub/idgames/idstuff/doom/doom19s.zip",
    "https://ftp.fu-berlin.de/pc/games/idgames/idstuff/doom/doom19s.zip",
    "https://youfailit.net/pub/idgames/idstuff/doom/doom19s.zip",
]
DOOM_ZIP_SHA256 = "cacf0142b31ca1af00796b4a0339e07992ac5f21bc3f81e7532fe1b5e1b486e6"
DOOM1_WAD_MD5 = "f0cefca49926d00903cf57551d901abe"      # shareware 1.9

DOSBOX_VER = "v0.82.2"
DOSBOX_URL = ("https://github.com/dosbox-staging/dosbox-staging/releases/"
              f"download/{DOSBOX_VER}/dosbox-staging-linux-x86_64-"
              f"{DOSBOX_VER}.tar.xz")
DOSBOX_SHA256 = "bc229df72ea103b7865cdca67324772dbffa8e58866477e69a79638b723a0442"

CONTENT_CATALOG = kilix_content.default_catalog()


def _catalog_source(content_id):
    spec = CONTENT_CATALOG.require(content_id)
    return spec.repository, spec.ref


BASHED_REPO, BASHED_REF = _catalog_source("bashed-earth")
JPAK_REPO, JPAK_REF = _catalog_source("kilix-jpak")
JOUSTIX_REPO, JOUSTIX_REF = _catalog_source("joustix")
CHESS_REPO, CHESS_REF = _catalog_source("chess-bash")
FISHTANK_REPO, FISHTANK_REF = _catalog_source("kilix-fishtank")
LANDER_REPO, LANDER_REF = _catalog_source("terminal-lander")
BROKEOUT_REPO, BROKEOUT_REF = _catalog_source("kitty-brokeout")
AMP_REPO, AMP_REF = _catalog_source("kilix-amp")


def _menu_entry(spec):
    return {
        "label": spec.label,
        "icon": spec.icon,
        "blurb": (spec.description.rstrip(".")
                  + ".\nInstall into Kilix's private data directory and launch?"),
    }


# The Start-menu registry is a view of the shared, pinned content catalog.
# DOSBox remains in Games even though the catalog classifies it as an app.
GAMES = {
    spec.content_id: _menu_entry(spec)
    for spec in CONTENT_CATALOG
    if spec.kind == "game" or spec.content_id == "dosbox"
}

# The two desktop-window games in the shared vocabulary (kilix_sdk.settings
# GAME_TOGGLE_IDS): not catalog content, nothing to install — each is an app
# inside the bundled desktop. `kilix games play` boots that desktop with the
# window already open, so every surface that builds its menu from the shared
# toggle list gets a working launch for ALL the ids it shows. Before this
# map, these two ids fell into ensure()'s "unknown game" and the tab died.
DESKTOP_APP_GAMES = {
    "minesweeper": ("mines", "Minesweeper"),
    "solitaire": ("sol", "Solitaire"),
}


def game_enabled(game):
    """Whether the shared GPU Terminal settings expose ``game``."""
    return shared_settings.game_enabled(game)


def available_games(availability=None):
    """The enabled registry entries, retaining catalog menu order."""
    availability = (shared_settings.game_availability()
                    if availability is None else availability)
    return {
        name: meta for name, meta in GAMES.items()
        if availability[name]
    }


def load():
    # interpolation=None so a '%' in a stored path is literal, not a token;
    # a malformed conf reads as empty (== "no game installed") instead of
    # taking down whatever called a *_ready() check.
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(CONF)
    except configparser.Error:
        cp = configparser.ConfigParser(interpolation=None)
    return cp


def save(cp):
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    with open(CONF, "w") as f:
        cp.write(f)


def _find(d, name):
    """Case-insensitive file search under d; returns a path or None."""
    for root, _dirs, files in os.walk(d):
        for f in files:
            if f.lower() == name.lower():
                return os.path.join(root, f)
    return None


def doom_ready(cp=None):
    """(dosbox, doom_exe) if the config points at a working install."""
    cp = cp or load()
    if not cp.has_section("doom"):
        return None
    dosbox = os.path.expanduser(cp.get("doom", "dosbox", fallback=""))
    ddir = os.path.expanduser(cp.get("doom", "dir", fallback=""))
    if not (dosbox and os.access(dosbox, os.X_OK) and os.path.isdir(ddir)):
        return None
    exe = _find(ddir, "DOOM.EXE")
    wad = _find(ddir, "DOOM1.WAD") or _find(ddir, "DOOM.WAD")
    return (dosbox, exe) if exe and wad else None


def dosbox_ready(cp=None):
    """Path to a runnable dosbox if one is already available (no install):
    config > $PATH > previously vendored."""
    cp = cp or load()
    cur = os.path.expanduser(cp.get("dosbox", "exe", fallback=""))
    if cur and os.access(cur, os.X_OK):
        return cur
    for name in ("dosbox", "dosbox-staging", "dosbox-x"):
        p = shutil.which(name)
        if p:
            return p
    vend = os.path.join(GAMES_DIR, "dosbox-staging")
    exe = _find(vend, "dosbox") if os.path.isdir(vend) else None
    return exe if exe and os.access(exe, os.X_OK) else None


def game_ready(game, cp=None):
    """Installed-and-runnable check dispatched by game name (None if not)."""
    cp = cp or load()
    if game in DESKTOP_APP_GAMES:
        # Always ready: the game is a window of the bundled desktop, so its
        # runtime ships with this checkout and never needs installing.
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "main.py")
    if game == "doom":
        return doom_ready(cp)
    if game == "dosbox":
        return dosbox_ready(cp)
    spec = CONTENT_CATALOG.get(game)
    if spec and spec.kind == "game" and spec.source_type == "git":
        return _catalog_ready(game, cp)
    return None


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(urls, dest, report, sha256=None):
    """Compatibility wrapper around the shared checksum-bound downloader."""
    return kilix_content.download(
        urls, dest, report=report, expected_sha256=sha256 or "")


def _inside(root, path):
    root = os.path.abspath(root)
    path = os.path.abspath(path)
    return os.path.commonpath([root, path]) == root


def _safe_extract_tar(tar, dest):
    """Compatibility wrapper around the shared safe tar extractor."""
    return kilix_content.safe_extract_tar(tar, dest)


def _safe_extract_zip(archive, dest):
    """Compatibility wrapper around the shared safe ZIP extractor."""
    return kilix_content.safe_extract_zip(archive, dest)


def ensure_dosbox(cp, report):
    """A runnable dosbox: config > $PATH > previously vendored > download."""
    cur = os.path.expanduser(cp.get("doom", "dosbox", fallback=""))
    if cur and os.access(cur, os.X_OK):
        return cur
    for name in ("dosbox", "dosbox-staging", "dosbox-x"):
        p = shutil.which(name)
        if p:
            report(f"using system {name}: {p}")
            return p
    vend = os.path.join(GAMES_DIR, "dosbox-staging")
    exe = _find(vend, "dosbox") if os.path.isdir(vend) else None
    if exe and os.access(exe, os.X_OK):
        return exe
    if os.uname().machine != "x86_64":
        raise RuntimeError(
            "no dosbox on PATH and the dosbox-staging release build is "
            f"x86_64-only (this is {os.uname().machine}) — install dosbox "
            "with your package manager, or set [doom] dosbox= in "
            f"{CONF}")
    os.makedirs(vend, exist_ok=True)
    tar = os.path.join(vend, "dosbox.tar.xz")
    _fetch(DOSBOX_URL, tar, report, sha256=DOSBOX_SHA256)
    report("unpacking dosbox-staging …")
    with tarfile.open(tar, "r:xz") as t:
        _safe_extract_tar(t, vend)
    os.unlink(tar)
    exe = _find(vend, "dosbox")
    if not (exe and os.access(exe, os.X_OK)):
        raise RuntimeError("dosbox-staging unpack yielded no dosbox binary")
    return exe


def ensure_doom(cp, report):
    """A directory with DOOM.EXE + DOOM1.WAD: config > vendored > download."""
    cur = os.path.expanduser(cp.get("doom", "dir", fallback=""))
    if cur and _find(cur, "DOOM.EXE") and (_find(cur, "DOOM1.WAD")
                                           or _find(cur, "DOOM.WAD")):
        return cur
    ddir = os.path.join(GAMES_DIR, "doom")
    if _find(ddir, "DOOM.EXE") and _find(ddir, "DOOM1.WAD"):
        return ddir
    os.makedirs(ddir, exist_ok=True)
    outer = os.path.join(ddir, "doom19s.zip")
    _fetch(DOOM_URLS, outer, report, sha256=DOOM_ZIP_SHA256)
    report("extracting the shareware episode …")
    with zipfile.ZipFile(outer) as z:
        _safe_extract_zip(z, ddir)
    # DEICE's split archive: DOOMS_19.1 + DOOMS_19.2 = a self-extracting ZIP
    joined = os.path.join(ddir, "dooms_19.sfx")
    with open(joined, "wb") as out:
        for part in ("DOOMS_19.1", "DOOMS_19.2"):
            p = _find(ddir, part)
            if not p:
                raise RuntimeError(f"{part} missing from doom19s.zip")
            with open(p, "rb") as f:
                out.write(f.read())
    with zipfile.ZipFile(joined) as z:      # zipfile handles the MZ stub
        _safe_extract_zip(z, ddir)
    for junk in ("doom19s.zip", "dooms_19.sfx", "DOOMS_19.1", "DOOMS_19.2",
                 "DOOMS_19.DAT", "DEICE.EXE", "INSTALL.BAT"):
        p = _find(ddir, junk)
        if p:
            os.unlink(p)
    wad = _find(ddir, "DOOM1.WAD")
    if not (wad and _find(ddir, "DOOM.EXE")):
        raise RuntimeError("extraction yielded no DOOM.EXE/DOOM1.WAD")
    md5 = hashlib.md5(open(wad, "rb").read()).hexdigest()
    if md5 != DOOM1_WAD_MD5:
        raise RuntimeError(
            f"DOOM1.WAD md5 {md5} differs from the known 1.9 build")
    return ddir


# DOSBox: fullscreen on its private X server, which `kilix run` sizes to the
# pane's exact pixel area — so the game fills the WHOLE pane, no letterbox
# (aspect=false: pane aspect wins over VGA aspect). Sound on (Doom autodetects
# the emulated Sound Blaster). Understood by classic DOSBox and staging.
DOSBOX_CONF = """\
[sdl]
fullscreen=true
fullresolution=desktop
output=opengl

[render]
aspect=false

[mixer]
nosound=false
rate=44100
"""

# Doom merges this with its built-in defaults: fire on Space (57), use/open
# on Ctrl (157 = right-ctrl) — swapped from the DOS defaults per taste — and
# the Sound Blaster settings SETUP.EXE would normally write (without them the
# engine defaults to NO sound devices). Device 3 = Sound Blaster: digital sfx
# + OPL FM music, exactly what DOSBox emulates at 220/7/1.
DOOM_CFG = """\
key_fire\t\t57
key_use\t\t\t157
sfx_volume\t\t8
music_volume\t\t8
snd_channels\t\t3
snd_musicdevice\t\t3
snd_sfxdevice\t\t3
snd_sbport\t\t544
snd_sbirq\t\t7
snd_sbdma\t\t1
"""


def _write_settings(ddir, report):
    """Drop the dosbox conf + key bindings next to the game (created once;
    a user-edited file is never overwritten)."""
    conf = os.path.join(ddir, "dosbox-kilix.conf")
    if not os.path.exists(conf):
        with open(conf, "w") as f:
            f.write(DOSBOX_CONF)
        report("wrote dosbox-kilix.conf (fullscreen, aspect, sound on)")
    exe = _find(ddir, "DOOM.EXE")
    cfg = os.path.join(os.path.dirname(exe), "DEFAULT.CFG") if exe else None
    if cfg and not os.path.exists(cfg):
        with open(cfg, "w") as f:
            f.write(DOOM_CFG)
        report("wrote DEFAULT.CFG (fire = Space, use = Ctrl)")
    return conf


def _write_dosbox_conf(report):
    """The shared fullscreen/sound dosbox conf in the games dir (once)."""
    os.makedirs(GAMES_DIR, exist_ok=True)
    conf = os.path.join(GAMES_DIR, "dosbox-kilix.conf")
    if not os.path.exists(conf):
        with open(conf, "w") as f:
            f.write(DOSBOX_CONF)
        report("wrote dosbox-kilix.conf (fullscreen, sound on)")
    return conf


def _audio_check(report):
    import subprocess
    try:
        r = subprocess.run(["pactl", "info"], capture_output=True, text=True,
                           timeout=5)
        if r.returncode == 0:
            srv = next((ln.split(":", 1)[1].strip()
                        for ln in r.stdout.splitlines()
                        if ln.startswith("Server Name")), "PulseAudio")
            report(f"audio: {srv} reachable — DOSBox sound will work")
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    report("audio: no PulseAudio/PipeWire found — Doom will run silent")


def _verify_source_checkout(repo, ref, dest):
    """Require the expected origin, pinned HEAD, and clean tracked source."""
    return kilix_content.verify_git_checkout(repo, ref, dest)


def _clone_and_make(repo, ref, dest, binary, dep_hint, report):
    """Compatibility entry point backed by the shared atomic installer."""
    # Historical callers may point this helper at a prebuilt fixture.  Real
    # catalog entries always declare their build command explicitly.
    build = [] if os.path.isdir(repo) and os.access(
        os.path.join(repo, binary), os.X_OK) else ["make"]
    spec = kilix_content.ContentSpec.from_mapping({
        "id": os.path.basename(dest),
        "label": os.path.basename(dest),
        "source": {"type": "git", "repository": repo, "ref": ref},
        "binary": binary,
        "build": build,
        "dependency_hint": dep_hint,
    })
    return kilix_content.Installer(os.path.dirname(dest)).ensure(spec, report)


def _repo_ready(cp, section, binary, managed_dir, repo, ref):
    if not cp.has_section(section):
        return None
    d = os.path.expanduser(cp.get(section, "dir", fallback=""))
    if not d:
        return None
    spec = kilix_content.ContentSpec.from_mapping({
        "id": os.path.basename(managed_dir),
        "label": section,
        "source": {"type": "git", "repository": repo, "ref": ref},
        "binary": binary,
    })
    return kilix_content.Installer(os.path.dirname(managed_dir)).ready(
        spec, directory=d)


def _content_root(spec):
    return APPS_DIR if spec.kind == "app" else GAMES_DIR


def _catalog_ready(content_id, cp=None):
    cp = cp or load()
    spec = CONTENT_CATALOG.require(content_id)
    if spec.source_type != "git" or not cp.has_section(content_id):
        return None
    directory = os.path.expanduser(cp.get(content_id, "dir", fallback=""))
    if not directory:
        return None
    return kilix_content.Installer(_content_root(spec)).ready(
        spec, directory=directory)


def _catalog_ensure(content_id, cp, report):
    spec = CONTENT_CATALOG.require(content_id)
    if spec.source_type != "git":
        raise RuntimeError(f"{content_id} does not use the shared Git installer")
    return (_catalog_ready(content_id, cp)
            or kilix_content.Installer(_content_root(spec)).ensure(spec, report))


def bashed_ready(cp=None):
    return _catalog_ready("bashed-earth", cp)


def ensure_bashed(cp, report):
    return _catalog_ensure("bashed-earth", cp, report)


def jpak_ready(cp=None):
    return _catalog_ready("kilix-jpak", cp)


def ensure_jpak(cp, report):
    return _catalog_ensure("kilix-jpak", cp, report)


def joustix_ready(cp=None):
    return _catalog_ready("joustix", cp)


def ensure_joustix(cp, report):
    return _catalog_ensure("joustix", cp, report)


def chess_ready(cp=None):
    return _catalog_ready("chess-bash", cp)


def ensure_chess(cp, report):
    return _catalog_ensure("chess-bash", cp, report)


def fishtank_ready(cp=None):
    return _catalog_ready("kilix-fishtank", cp)


def ensure_fishtank(cp, report):
    return _catalog_ensure("kilix-fishtank", cp, report)


def lander_ready(cp=None):
    return _catalog_ready("terminal-lander", cp)


def ensure_lander(cp, report):
    return _catalog_ensure("terminal-lander", cp, report)


def brokeout_ready(cp=None):
    return _catalog_ready("kitty-brokeout", cp)


def ensure_brokeout(cp, report):
    return _catalog_ensure("kitty-brokeout", cp, report)


def amp_ready(cp=None):
    return _catalog_ready("kilix-amp", cp)


def ensure_amp(cp, report):
    return _catalog_ensure("kilix-amp", cp, report)


def ensure(game, report=print):
    cp = load()
    if not cp.has_section(game):
        cp.add_section(game)
    if game == "doom":
        dosbox = ensure_dosbox(cp, report)
        ddir = ensure_doom(cp, report)
        conf = _write_settings(ddir, report)
        _audio_check(report)
        cp.set("doom", "dosbox", dosbox)
        cp.set("doom", "dir", ddir)
        payload = (dosbox, conf, _find(ddir, "DOOM.EXE"))
    elif game == "dosbox":
        dosbox = ensure_dosbox(cp, report)
        conf = _write_dosbox_conf(report)
        _audio_check(report)
        cp.set("dosbox", "exe", dosbox)
        payload = (dosbox, conf)
    elif ((spec := CONTENT_CATALOG.get(game)) is not None
          and spec.source_type == "git"):
        exe = _catalog_ensure(game, cp, report)
        cp.set(game, "dir", os.path.dirname(exe))
        payload = exe
    elif game in DESKTOP_APP_GAMES:
        # Nothing to fetch or build: the game ships inside the desktop.
        _app, label = DESKTOP_APP_GAMES[game]
        report(f"{label} is built into the desktop — nothing to install")
        return None
    else:
        # No "kilix games:" prefix — main() adds it when it shows the words.
        raise SystemExit(
            f"unknown game {game!r} — run 'kilix games list' for the ids")
    save(cp)
    report(f"ready — config saved to {CONF}")
    return payload


def _launch_doom(payload):
    dosbox, conf, exe = payload
    kilix = os.path.join(KILIX_HOME, "kilix")
    argv = [dosbox, "-conf", conf, exe, "-exit"]
    if os.environ.get("KITTY_WINDOW_ID") and os.access(kilix, os.X_OK):
        # already in our own tab: run DOSBox in-place through `kilix run`.
        # 640x400 = Doom's own 16:10, so DOSBox fullscreen has zero bars;
        # --fill then stretches the placement over the WHOLE pane.
        os.environ["KILIX_IN_OVERLAY"] = "1"
        os.execv(kilix, [kilix, "run", "--fill", "--size", "640x400"] + argv)
    elif os.environ.get("DISPLAY"):
        os.execv(dosbox, argv)                # plain X session
    raise SystemExit("kilix games: no display (run inside kilix or X)")


def _launch_dosbox(payload):
    dosbox, conf = payload
    kilix = os.path.join(KILIX_HOME, "kilix")
    # boot to a DOS prompt with C: mounted at the games folder, so any DOS
    # programs dropped there are one `C:` away
    argv = [dosbox, "-conf", conf,
            "-c", f'mount c "{GAMES_DIR}"', "-c", "c:"]
    if os.environ.get("KITTY_WINDOW_ID") and os.access(kilix, os.X_OK):
        # 640x400 = 80x25 VGA text, so the prompt is crisp; --fill stretches
        # the placement over the whole pane (aspect=false in the conf)
        os.environ["KILIX_IN_OVERLAY"] = "1"
        os.execv(kilix, [kilix, "run", "--fill", "--size", "640x400"] + argv)
    elif os.environ.get("DISPLAY"):
        os.execv(dosbox, argv)                # plain X session
    raise SystemExit("kilix games: no display (run inside kilix or X)")


def _launch_native(exe):
    # a native terminal game (Bashed Earth, Terminal Lander) that speaks the
    # kitty graphics protocol itself: runs right here in the tab
    os.chdir(os.path.dirname(exe))
    os.execv(exe, [exe])


def _launch_desktop_app(game):
    """Boot the bundled desktop with this game's window already open.

    Minesweeper and Solitaire are desktop windows, not programs: the desktop
    is their runtime. The bundled desktop sits at the same pinned version as
    this backend, so `kilix games play minesweeper` from any other desktop's
    menu opens the same build the built-in provider would."""
    app, _label = DESKTOP_APP_GAMES[game]
    main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "main.py")
    os.execv(sys.executable, [sys.executable, main_py, "--app", app])


def main():
    try:
        shared_settings.ensure_file()
    except OSError as error:
        raise SystemExit(
            f"kilix games: cannot initialize shared settings: {error}")
    args = [a for a in sys.argv[1:]]
    setup_only = "--setup-only" in args
    args = [a for a in args if a != "--setup-only"]
    game = args[0] if args else "doom"
    if ((game in GAMES or game in DESKTOP_APP_GAMES)
            and not game_enabled(game)):
        label = (GAMES[game]["label"] if game in GAMES
                 else DESKTOP_APP_GAMES[game][1])
        raise SystemExit(
            f"kilix games: {label} is disabled; "
            f"run 'kilix games enable {game}' to make it available")
    if game in DESKTOP_APP_GAMES:
        # A desktop window, not installable content: setup is a no-op and
        # playing boots the bundled desktop with the window open.
        _app, label = DESKTOP_APP_GAMES[game]
        if setup_only:
            print(f"{label} is built into the desktop — nothing to install")
            return
        _launch_desktop_app(game)
        return
    try:
        payload = ensure(game)
    except (SystemExit, Exception) as e:
        # BadZipFile/TarError/configparser.Error too — and refusals raised as
        # SystemExit, which used to fly out of here and take the tab with
        # them before anyone could read the words.
        print(f"\x1b[1;31mkilix games: {e}\x1b[0m", file=sys.stderr)
        try:
            input("\n[Enter to close]")
        except EOFError:
            pass
        sys.exit(1)
    if setup_only:
        return
    if game == "doom":
        _launch_doom(payload)
    elif game == "dosbox":
        _launch_dosbox(payload)
    else:
        _launch_native(payload)


if __name__ == "__main__":
    main()
