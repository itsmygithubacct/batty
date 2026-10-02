"""kilix desktop — built-in apps.

apps.open(desk, name, arg) is the only entry point the shell uses; each app
is a wm.Window subclass in its own module. Settings is a singleton (opening
it again focuses the existing window); everything else opens fresh.
"""

import wm


GAME_APPS = {"mines": "minesweeper", "sol": "solitaire"}


def open(desk, name, arg=None):
    game_id = GAME_APPS.get(name)
    if game_id is not None:
        from kilix_sdk import settings as shared_settings
        if not shared_settings.game_enabled(game_id):
            wm.msgbox(
                desk, "Games",
                "This game is disabled. Re-enable it in "
                "kilix Settings → Games.",
                icon="info")
            return
    if name == "filemgr":
        from . import filemgr
        desk.wm.add(filemgr.FileWindow(desk, arg or "~"))
    elif name == "notepad":
        from . import notepad
        desk.wm.add(notepad.Notepad(desk, arg))
    elif name == "viewer":
        from . import viewer
        desk.wm.add(viewer.Viewer(desk, arg))
    elif name == "amp":
        from . import amp
        amp.open_amp(desk, arg)
    elif name == "settings":
        from . import settings
        for w in desk.wm.windows:
            if isinstance(w, settings.SettingsWin):
                desk.wm.activate(w)
                return
        desk.wm.add(settings.SettingsWin(desk))
    elif name == "soundcp":
        from . import soundcp
        soundcp.open(desk)
    elif name == "calc":
        from . import calc
        desk.wm.add(calc.Calc(desk, arg))
    elif name == "mines":
        from . import mines
        desk.wm.add(mines.Mines(desk, arg))
    elif name == "sol":
        from . import sol
        desk.wm.add(sol.Solitaire(desk, arg))
    elif name == "paint":
        from . import paint
        desk.wm.add(paint.Paint(desk, arg))
    elif name == "wordpad":
        from . import wordpad
        desk.wm.add(wordpad.WordPad(desk, arg))
    elif name == "charmap":
        from . import charmap
        desk.wm.add(charmap.CharMap(desk, arg))
    elif name == "winhelp":
        from . import winhelp
        desk.wm.add(winhelp.Help(desk, arg))
    elif name == "mycomp":
        from . import mycomp
        desk.wm.add(mycomp.MyComputer(desk, arg))
    elif name == "recyclebin":
        from . import recyclebin
        for w in desk.wm.windows:
            if isinstance(w, recyclebin.RecycleBin):
                desk.wm.activate(w)
                return
        desk.wm.add(recyclebin.RecycleBin(desk))
    elif name == "findfiles":
        from . import findfiles
        desk.wm.add(findfiles.FindFiles(desk, arg))
    elif name == "taskmgr":
        from . import taskmgr
        for w in desk.wm.windows:
            if isinstance(w, taskmgr.TaskManager):
                desk.wm.activate(w)
                return
        desk.wm.add(taskmgr.TaskManager(desk))
    else:
        raise ValueError(f"unknown app {name!r}")
