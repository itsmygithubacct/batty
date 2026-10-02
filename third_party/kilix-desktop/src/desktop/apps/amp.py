"""kilix desktop — Media Player: kilix-amp (Winamp 2.x clone) in an XPane.

Unlike the games (which live in kilix tabs), the media player opens INSIDE
the desktop: an SDL2 app on a private X server, streamed into a kilix 95
window. First run clones and builds github.com/itsmygithubacct/kilix-amp
via the InstallerWindow; the layout/config it saves is kept private under
its install dir so it never fights the user's own kilix-amp setup.
"""
import os

import wm
import storage
from . import xpane


def open_amp(desk, path=None):
    import games
    exe = games.amp_ready()
    if exe:
        _spawn(desk, exe, path)
        return

    def answered(ans):
        if ans == "Install":
            desk.wm.add(xpane.InstallerWindow(
                desk, "kilix-amp", "Media Player",
                on_ok=lambda: _spawn(desk, games.amp_ready(), path)))

    wm.msgbox(desk, "Media Player",
              "The media player isn't built yet.\n\n"
              "Clone and build kilix-amp (a Winamp 2.x clone,\n"
              "github.com/itsmygithubacct/kilix-amp) into\n"
              "~/.local/gpu_terminal/kilix/data/desktop-apps?\n"
              "(Needs libsdl2-dev, libsdl2-image-dev,\n"
              "libsndfile1-dev, zlib1g-dev, libfluidsynth-dev,\n"
              "and a GM SoundFont for MIDI playback.)",
              icon="amp", buttons=("Install", "Cancel"), cb=answered)


def _seed_sample(amp_dir):
    """Copy the bundled sample track into ~/Music (where kilix-amp's Open
    dialog opens) so the player has something to try on first use. Once."""
    try:
        src = os.path.join(amp_dir, "samples", "ode-to-joy.ogg")
        if not os.path.isfile(src):
            return
        music = os.path.expanduser("~/Music")
        os.makedirs(music, exist_ok=True)
        dst = os.path.join(music, "Ode to Joy.ogg")
        if not os.path.exists(dst):
            import shutil
            shutil.copyfile(src, dst)
    except Exception:
        pass


def _spawn(desk, exe, path=None):
    if not exe:
        return
    d = os.path.dirname(exe)
    _seed_sample(d)
    cmd = [exe] + ([os.path.abspath(os.path.expanduser(path))] if path
                   else [])
    try:
        desk.wm.add(xpane.XPane(
            desk, cmd, "Media Player", icon="amp",
            # no app_size: the region fills the desktop working area, so the
            # skin can be dragged anywhere like Winamp and its stacked windows
            # (EQ / playlist) are never clipped
            # private, persistent config: window layout survives sessions and
            # never collides with a user-level kilix-amp install
            env={
                "XDG_CONFIG_HOME": storage.config_dir("app-state"),
                "XDG_DATA_HOME": storage.data_dir("app-state"),
            },
            cwd=d))
    except Exception as e:
        wm.msgbox(desk, "Media Player",
                  f"Could not open Media Player:\n{e}", icon="error")
