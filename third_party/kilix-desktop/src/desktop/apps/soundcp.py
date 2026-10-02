"""kilix desktop — Sounds control panel (the classic "Sounds Properties").

Edits the sound scheme model in sounds.py: pick a scheme, bind system events
to sound files (or silence), audition them, and either Apply (persist via
sounds.set_sound + save, taking effect immediately for Desk.play_sound) or
Cancel. Both per-event edits and the picked scheme live in a working copy
until Apply/OK, so Cancel backs out cleanly. A singleton like Settings.
Integrates kilix-amp: any sound can
be opened in the media player for auditioning.
"""
import os

import filedialog
import sounds
import widgets as W
import wm

_NONE = "(None)"
_AUDIO_FILTERS = [
    ("Audio Files", "*.mp3;*.ogg;*.flac;*.opus;*.wav;*.m4a;*.aac;*.aif;*.aiff"),
    ("All Files", "*.*"),
]


class SoundCP(wm.Window):
    def __init__(self, desk):
        super().__init__(desk, "Sounds Properties", 440, 360, icon="soundcp",
                         resizable=False)
        sounds.warm()                               # fill the wav cache off-thread
        self.cur = None                             # selected event id
        self.work = dict(sounds.current_scheme())   # overrides, edited until Apply
        self.library = sounds.library_sounds(generate=False)
        self.sound_paths = []
        cw, ch = self.client_size()

        # Schemes row -----------------------------------------------------
        self.add(W.Label(12, 12, "Scheme:"))
        names = sounds.scheme_names()
        active = getattr(sounds, "_active_name", sounds.DEFAULT_SCHEME)
        self.scheme = active if active in names else sounds.DEFAULT_SCHEME
        idx = names.index(active) if active in names else 0
        self.scheme_dd = self.add(W.Dropdown(70, 8, cw - 236, names, idx,
                                             cb=self._scheme_changed))
        self.add(W.Button(cw - 158, 7, 76, 23, "Save As…",
                          cb=self._save_as))
        self.add(W.Button(cw - 76, 7, 64, 23, "Delete", cb=self._delete))

        # Events list -----------------------------------------------------
        self.add(W.Label(12, 40, "Events:"))
        self.events_lb = self.add(W.ListBox(12, 56, cw - 24, 120,
                                            on_select=self._select_event))

        # Sound-for-selected-event group ---------------------------------
        gy = 184
        self.add(W.GroupBox(12, gy, cw - 24, 96, "Sound"))
        self.add(W.Label(24, gy + 24, "Sound:"))
        self.snd_dd = self.add(W.Dropdown(70, gy + 20, cw - 24 - 70 - 92,
                                          [_NONE], cb=self._sound_changed))
        self.add(W.Button(cw - 24 - 80, gy + 19, 80, 23, "Browse…",
                          cb=self._browse))
        self.add(W.Button(24, gy + 52, 96, 23, "▶ Preview",
                          cb=self._preview))
        self.add(W.Button(126, gy + 52, 158, 23, "Open in Media Player",
                          cb=self._open_amp))

        # OK / Cancel / Apply --------------------------------------------
        self.add(W.Button(cw - 244, ch - 33, 72, 23, "OK", default=True,
                          cb=self._ok))
        self.add(W.Button(cw - 164, ch - 33, 72, 23, "Cancel", cb=self.close))
        self.add(W.Button(cw - 84, ch - 33, 72, 23, "Apply", cb=self._apply))

        self._refresh_events()
        if self.events_lb.items:
            self.events_lb.sel = 0
            self._select_event(self.events_lb.items[0])
        self.set_focus(self.events_lb)

    # ── model helpers ────────────────────────────────────────────────────────
    def _resolved(self, eid):
        """The working sound for an event: an override, else the built-in cue.
        Never synthesizes — warm() fills the cue file off-thread."""
        if eid in self.work:
            return self.work[eid]
        return sounds.path_for(eid)

    def _current_path(self):
        i = self.snd_dd.index
        return self.sound_paths[i] if 0 <= i < len(self.sound_paths) else None

    def _refresh_events(self):
        items = [("soundcp" if self._resolved(eid) is not None else None,
                  label, eid) for eid, label, _ in sounds.events(generate=False)]
        self.events_lb.set_items(items, keep_sel=True)

    def _refresh_sound(self):
        eid = self.cur
        cur = self._resolved(eid) if eid is not None else None
        paths = list(self.library)
        if cur and cur not in paths:
            paths.append(cur)
        self.sound_paths = paths
        self.snd_dd.options = [os.path.basename(p) for p in paths] + [_NONE]
        if cur is None:
            self.snd_dd.index = len(paths)
        else:
            self.snd_dd.index = paths.index(cur) if cur in paths else len(paths)
        self.snd_dd.enabled = eid is not None
        self.snd_dd.invalidate()

    # ── event handlers ───────────────────────────────────────────────────────
    def _select_event(self, item):
        self.cur = item[2]
        self._refresh_sound()

    def _sound_changed(self, *_):
        if self.cur is None:
            return
        i = self.snd_dd.index
        self.work[self.cur] = (None if i >= len(self.sound_paths)
                               else self.sound_paths[i])
        self._refresh_events()

    def _set_sound_path(self, path):
        """Bind the selected event to `path` (used by Browse); refresh views."""
        if self.cur is None or not path:
            return
        self.work[self.cur] = path
        if path not in self.library:
            self.library.append(path)
        self._refresh_sound()
        self._refresh_events()

    def _browse(self):
        if self.cur is None:
            return
        filedialog.open_file(self.desk, "Browse for Sound", self._set_sound_path,
                             filters=_AUDIO_FILTERS)

    def _preview(self):
        p = self._current_path()
        if p:
            sounds.preview(p)

    def _open_amp(self):
        p = self._current_path()
        if not p:
            return
        from . import amp
        amp.open_amp(self.desk, p)

    def _scheme_changed(self, name):
        self.scheme = name                          # deferred: commit on Apply/OK
        self.work = dict(sounds.scheme_overrides(name))
        if name in self.scheme_dd.options:
            self.scheme_dd.index = self.scheme_dd.options.index(name)
        self._refresh_events()
        self._refresh_sound()
        self.invalidate()

    def _save_as(self):
        def named(name):
            name = (name or "").strip()
            if not name:
                return
            if name in (sounds.DEFAULT_SCHEME, sounds.NO_SOUNDS):
                wm.msgbox(self.desk, "Sounds",
                          f"'{name}' is a built-in scheme name.\n"
                          "Please choose a different name.", icon="warn")
                return
            self._apply()                           # commit working state, then
            sounds.save_scheme_as(name)             # snapshot it under the name
            self.scheme = name
            self.scheme_dd.options = sounds.scheme_names()
            if name in self.scheme_dd.options:
                self.scheme_dd.index = self.scheme_dd.options.index(name)
            self.scheme_dd.invalidate()
        wm.inputbox(self.desk, "Save Scheme As", "Save this sound scheme as:",
                    cb=named, icon="soundcp")

    def _delete(self):
        name = self.scheme_dd.value
        if name in (sounds.DEFAULT_SCHEME, sounds.NO_SOUNDS):
            wm.msgbox(self.desk, "Sounds",
                      "Built-in schemes cannot be deleted.", icon="warn")
            return

        def ans(a):
            if a != "Yes":
                return
            try:
                os.remove(sounds._named_path(name))
            except OSError:
                pass
            self._scheme_changed(sounds.DEFAULT_SCHEME)
            self.scheme_dd.options = sounds.scheme_names()
            self.scheme_dd.index = 0
            self.scheme_dd.invalidate()
        wm.msgbox(self.desk, "Sounds", f"Delete the scheme '{name}'?",
                  icon="question", buttons=("Yes", "No"), cb=ans)

    def _apply(self):
        sounds.load_scheme(self.scheme)             # commit the picked scheme,
        for eid, val in self.work.items():
            sounds.set_sound(eid, val)              # then the per-event edits
        self.invalidate()

    def _ok(self):
        self._apply()
        self.close()


def open(desk):
    """Singleton: focus an existing panel, else create one."""
    for w in desk.wm.windows:
        if isinstance(w, SoundCP):
            desk.wm.activate(w)
            return w
    win = SoundCP(desk)
    desk.wm.add(win)
    return win
