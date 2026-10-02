"""kilix desktop — the start bar.

Start button + menu, one button per open window (stable launch order), and
a sunken clock well. The Start menu is a MenuHost popup with the classic
vertical "kilix 95" sidebar; its content is assembled from the shell's
built-in apps and the user's launchers.
"""
import calendar
import time

import icons
import theme as T
import widgets as W
import wm
import xdgapps

# The Start-button width is a flavor metric now (theme.START_W: 58 for 95, 64
# for xp) so it varies with KILIX_DESKTOP_FLAVOR — read T.START_W at the use sites.
CLOCK_W = 76
QL_BTN = 23                       # quick-launch button width


class Taskbar:
    def __init__(self, desk):
        self.desk = desk
        self.menu_open = -1           # duck-types as a MenuBar for MenuHost
        self._minute = ""
        self._pressed_btn = None
        self._popup = None            # tray flyout / clock calendar (a window)
        self._popup_owner = None      # "volume" | "clock"
        self._sd_restore = []         # windows hidden by Show Desktop

    # geometry -----------------------------------------------------------
    def rect(self):
        sw, sh = self.desk.size()
        return 0, sh - T.TASKBAR_H, sw, sh - 1

    # quick-launch toolbar (right of Start) --------------------------------
    def _ql_defs(self):
        shell = self.desk.shell
        return [
            ("show_desktop", "Show Desktop", self._show_desktop),
            ("browser", "Web Browser",
             lambda: shell.open_browser("firefox", "window")),
            ("folder_open", "File Manager",
             lambda: shell.open_app("filemgr")),
        ]

    def _ql_rect(self):
        x0, y0, x1, y1 = self.rect()
        lx = x0 + 2 + T.START_W + 6
        return lx, y0 + 4, lx + len(self._ql_defs()) * QL_BTN + 3, y1 - 3

    def _ql_buttons(self):
        """[(i, name, tip, action, x0, x1)] quick-launch buttons."""
        rx0, ry0, rx1, ry1 = self._ql_rect()
        out, bx = [], rx0 + 2
        for i, (name, tip, act) in enumerate(self._ql_defs()):
            out.append((i, name, tip, act, bx, bx + QL_BTN - 1))
            bx += QL_BTN
        return out

    def _ql_end(self):
        return self._ql_rect()[2] + 6

    # system tray (left of the clock) --------------------------------------
    def _tray_defs(self):
        entries = []
        if getattr(self.desk, "password_nag", False):
            entries.append(("password", "Default password — click to change it"))
        return entries + [("display", "Display"), ("speaker", "Volume")]

    def _tray_rect(self):
        x0, y0, x1, y1 = self.rect()
        w = len(self._tray_defs()) * 18 + 6
        tx1 = (x1 - CLOCK_W - 2) - 4
        return tx1 - w + 1, y0 + 4, tx1, y1 - 3

    def _tray_icons(self):
        """[(name, tip, x0, x1)] tray status icons, left to right."""
        tx0, ty0, tx1, ty1 = self._tray_rect()
        out, ix = [], tx0 + 3
        for name, tip in self._tray_defs():
            out.append((name, tip, ix, ix + 15))
            ix += 18
        return out

    def _clock_rect(self):
        x0, y0, x1, y1 = self.rect()
        return x1 - CLOCK_W - 2, y0 + 4, x1 - 2, y1 - 3

    def _buttons(self):
        """[(win, x0, x1)] task buttons in stable launch order."""
        wins = sorted((w for w in self.desk.wm.windows if not w.modal
                       and not getattr(w, "_no_taskbar", False)),
                      key=lambda w: w.seq)
        if not wins:
            return []
        bx = self._ql_end()
        lim = self._tray_rect()[0] - 6
        bw = min(150, max(22, (lim - bx) // len(wins) - 3))
        out = []
        for w in wins:
            if bx + bw - 1 > lim:     # never run under the tray/clock
                break
            out.append((w, bx, bx + bw - 1))
            bx += bw + 3
        return out

    def invalidate(self):
        self.desk.dirty = True

    def hover_switch(self, gev):      # MenuBar duck-type hook (unused here)
        pass

    def tick(self, now):
        m = time.strftime("%H:%M", time.localtime(now))
        if m != self._minute:
            self._minute = m
            self.invalidate()

    # drawing --------------------------------------------------------------
    def draw(self, fb, d):
        x0, y0, x1, y1 = self.rect()
        T.taskbar(d, x0, y0, x1, y1)
        # Start
        sb = (x0 + 2, y0 + 4, x0 + 2 + T.START_W - 1, y1 - 3)
        off, txcol = T.start_button(d, *sb, is_pressed=self.menu_open >= 0)
        icons.paint(fb, T.START_ICON, sb[0] + 4 + off, sb[1] + 2 + off, 16)
        d.text((sb[0] + 24 + off, sb[1] + 3 + off), T.START_LABEL, font=T.BOLD,
               fill=txcol)
        # quick launch
        self._draw_quicklaunch(fb, d)
        # task buttons
        active = self.desk.wm.active
        for win, bx0, bx1 in self._buttons():
            r = (bx0, y0 + 4, bx1, y1 - 3)
            if win is active and not win.minimized:
                T.pressed(d, *r, fill=T.LTGRAY)
                o = 1
            else:
                T.raised(d, *r)
                o = 0
            icons.paint(fb, win.icon, r[0] + 3 + o, r[1] + 2 + o, 16)
            tw = bx1 - bx0 - 28
            if tw > 6:                # icon-only when squeezed
                d.text((r[0] + 23 + o, r[1] + 3 + o),
                       T.ellipsize(T.FONT, win.title, tw),
                       font=T.FONT, fill=T.TEXT)
        # system tray + clock well
        self._draw_tray(fb, d)
        cx0, cy0, cx1, cy1 = self._clock_rect()
        T.sunken(d, cx0, cy0, cx1, cy1, fill=T.FACE)
        clock = self._minute or time.strftime("%H:%M")
        d.text((cx1 - 7 - T.text_w(T.FONT, clock), cy0 + 3), clock,
               font=T.FONT, fill=T.TEXT)

    def _draw_quicklaunch(self, fb, d):
        rx0, ry0, rx1, ry1 = self._ql_rect()
        T.raised_thin(d, rx0, ry0, rx1, ry1)
        cy = (ry0 + ry1) // 2
        for i, name, tip, act, bx0, bx1 in self._ql_buttons():
            ix = bx0 + (QL_BTN - 16) // 2
            if name == "show_desktop":
                self._draw_showdesktop(d, ix, cy - 8)
            else:
                icons.paint(fb, name, ix, cy - 8, 16)

    def _draw_tray(self, fb, d):
        tx0, ty0, tx1, ty1 = self._tray_rect()
        T.sunken(d, tx0, ty0, tx1, ty1, fill=T.FACE)
        cy = (ty0 + ty1) // 2
        for name, tip, ix0, ix1 in self._tray_icons():
            if name == "speaker":
                self._draw_speaker(d, ix0, cy, self._muted())
            elif name == "password":
                icons.paint(fb, "warn", ix0, cy - 8, 16)
            else:
                icons.paint(fb, name, ix0, cy - 8, 16)

    def _draw_speaker(self, d, ix0, cy, muted):
        k = T.TEXT
        sx, sy = ix0 + 1, cy - 5
        d.rectangle([sx, sy + 3, sx + 2, sy + 7], fill=k)      # magnet
        d.polygon([(sx + 2, sy + 3), (sx + 6, sy - 1),
                   (sx + 6, sy + 11), (sx + 2, sy + 7)], fill=k)   # cone
        if muted:
            r = (200, 0, 0)
            d.line([(sx + 8, sy - 1), (sx + 13, sy + 9)], fill=r)
            d.line([(sx + 8, sy + 9), (sx + 13, sy - 1)], fill=r)
        else:
            d.arc([sx + 6, sy, sx + 11, sy + 10], -60, 60, fill=k)
            d.arc([sx + 8, sy - 2, sx + 15, sy + 12], -60, 60, fill=k)

    def _draw_showdesktop(self, d, ix, iy):
        d.rectangle([ix + 1, iy + 3, ix + 11, iy + 13], fill=T.DESKTOP,
                    outline=T.TEXT)                              # desktop pad
        d.rectangle([ix + 3, iy + 5, ix + 6, iy + 8], fill=T.LIGHT,
                    outline=T.SHADOW)                            # a window
        d.line([(ix + 6, iy + 11), (ix + 13, iy + 2)], fill=(255, 206, 0))
        d.line([(ix + 12, iy + 3), (ix + 14, iy + 1)], fill=T.TEXT)  # pencil tip

    # input ------------------------------------------------------------------
    def hit(self, gx, gy):
        x0, y0, x1, y1 = self.rect()
        return y0 <= gy <= y1

    def on_mouse(self, gev):
        x0, y0, x1, y1 = self.rect()
        if not gev.press:
            return True
        modal = self.desk.wm.modal_top()
        if gev.btn == 1 and x0 + 2 <= gev.x < x0 + 2 + T.START_W:
            if modal:
                self.desk.wm.activate(modal)
            else:
                self._close_popup()
                self.open_start_menu()
            return True
        if not modal:
            for i, name, tip, act, bx0, bx1 in self._ql_buttons():
                if bx0 <= gev.x <= bx1 and gev.btn == 1:
                    self._close_popup()
                    act()
                    return True
            for name, tip, ix0, ix1 in self._tray_icons():
                if ix0 <= gev.x <= ix1 and gev.btn == 1:
                    self._tray_click(name)
                    return True
            cx0, cy0, cx1, cy1 = self._clock_rect()
            if cx0 <= gev.x <= cx1 and gev.btn == 1 and gev.clicks >= 2:
                self._toggle_popup("clock")
                return True
        for win, bx0, bx1 in self._buttons():
            if bx0 <= gev.x <= bx1:
                if modal:
                    self.desk.wm.activate(modal)
                elif gev.btn == 3:
                    win._system_menu(bx0, y0)
                elif win is self.desk.wm.active and not win.minimized:
                    self.desk.wm.minimize(win)
                else:
                    self._close_popup()
                    self.desk.wm.activate(win)
                return True
        if gev.btn == 3:                          # empty space → context menu
            if modal:
                self.desk.wm.activate(modal)
            else:
                self._context_menu(gev.x, gev.y)
        return True

    def tooltip_at(self, gx, gy):
        x0, y0, x1, y1 = self.rect()
        if not (y0 <= gy <= y1):
            return None
        cx0, cy0, cx1, cy1 = self._clock_rect()
        if cx0 <= gx <= cx1:
            return time.strftime("%A, %B %d, %Y")
        for name, tip, ix0, ix1 in self._tray_icons():
            if ix0 <= gx <= ix1:
                if name == "speaker":
                    return ("Volume: muted" if self._muted()
                            else "Volume: %d%%" % self._volume())
                return tip
        for i, name, tip, act, bx0, bx1 in self._ql_buttons():
            if bx0 <= gx <= bx1:
                return tip
        return None

    # ── tray flyouts / calendar popup ─────────────────────────────────────
    def _tray_click(self, name):
        if name == "speaker":
            self._toggle_popup("volume")
        elif name == "display":
            self._close_popup()
            self.desk.shell.display_properties()
        elif name == "password":
            self._close_popup()
            self.desk.shell.change_password_dialog()

    def show_password_balloon(self):
        """Show a dismissible reminder; the tray icon remains persistent."""
        self._close_popup()
        width, height = 254, 100
        x0, y0, x1, y1 = self.rect()
        screen_w, _screen_h = self.desk.size()
        tray_right = self._tray_rect()[2]
        x = max(4, min(tray_right - width + 16, screen_w - width - 2))
        popup = _PasswordBalloon(
            self.desk, self, x, max(0, y0 - height - 1))
        self.desk.wm.add(popup)
        self._popup, self._popup_owner = popup, "password"

    def _toggle_popup(self, kind):
        if self._popup_owner == kind:
            self._close_popup()
            return
        self._close_popup()
        win = (self._make_volume_flyout() if kind == "volume"
               else self._make_clock_popup())
        self._popup, self._popup_owner = win, kind

    def _close_popup(self):
        w, self._popup, self._popup_owner = self._popup, None, None
        if w is not None and w in self.desk.wm.windows:
            w.close()

    def _make_volume_flyout(self):
        fw, fh = 72, 136
        x0, y0, x1, y1 = self.rect()
        sw, sh = self.desk.size()
        px = max(0, min(self._tray_rect()[0] - 6, sw - fw))
        win = _VolFlyout(self.desk, self, px, max(0, y0 - fh - 1))
        self.desk.wm.add(win)
        return win

    def _make_clock_popup(self):
        cw, ch = 154, 160
        x0, y0, x1, y1 = self.rect()
        sw, sh = self.desk.size()
        px = max(0, min(self._clock_rect()[2] - cw + 1, sw - cw))
        win = _ClockPopup(self.desk, self, px, max(0, y0 - ch - 1))
        self.desk.wm.add(win)
        return win

    # ── volume state (persisted to shell state) ───────────────────────────
    def _volume(self):
        return int(self.desk.shell.state.get("volume", 75))

    def _muted(self):
        return bool(self.desk.shell.state.get("muted", False))

    def _set_volume(self, v):
        self.desk.shell.state["volume"] = max(0, min(100, int(v)))
        self.desk.shell._save_state()
        self.invalidate()

    def _set_muted(self, on):
        self.desk.shell.state["muted"] = bool(on)
        self.desk.shell._save_state()
        self.invalidate()

    # ── context menu: window arrangement ──────────────────────────────────
    def _context_menu(self, gx, gy):
        self._close_popup()
        shell = self.desk.shell
        MI = W.MenuItem
        items = [
            MI("Cascade Windows", action=self._cascade),
            MI("Tile Windows Horizontally", action=self._tile_h),
            MI("Minimize All Windows", action=self._minimize_all),
            W.sep(),
            MI("Task Manager", action=lambda: shell.open_app("taskmgr")),
        ]
        self.desk.menus.open(items, gx, gy)

    def _arrangeable(self):
        return [w for w in self.desk.wm.windows
                if not w.modal and not w.minimized
                and not getattr(w, "_no_taskbar", False)]

    def _resize_win(self, win, x, y, w, h):
        win.maximized = False
        win.x, win.y = x, y
        if (win.w, win.h) != (w, h):
            win.w, win.h = w, h
            win.surface = None
            win.on_resize()
        self.desk.dirty = True

    def _cascade(self):
        wins = self._arrangeable()
        if not wins:
            return
        sw, sh = self.desk.size()
        ah = sh - T.TASKBAR_H
        w, h = max(240, sw * 6 // 10), max(160, ah * 6 // 10)
        step = T.TITLE_H + T.BORDER + 4
        for i, win in enumerate(wins):
            self._resize_win(win, (i * step) % max(1, sw - w),
                             (i * step) % max(1, ah - h), w, h)
            self.desk.wm.activate(win)

    def _tile_h(self):
        wins = self._arrangeable()
        if not wins:
            return
        sw, sh = self.desk.size()
        ah = sh - T.TASKBAR_H
        band = max(80, ah // len(wins))
        for i, win in enumerate(wins):
            self._resize_win(win, 0, min(i * band, ah - band), sw, band)
            self.desk.wm.activate(win)

    def _minimize_all(self):
        for win in self._arrangeable():
            self.desk.wm.minimize(win)

    def _show_desktop(self):
        self._close_popup()
        wmgr = self.desk.wm
        live = [w for w in wmgr.windows if not w.modal
                and not getattr(w, "_no_taskbar", False)]
        shown = [w for w in live if not w.minimized]
        if shown:                     # hide everything, remember what we hid
            self._sd_restore = shown
            for w in shown:
                wmgr.minimize(w)
        else:                         # nothing visible → restore the last set
            for w in self._sd_restore:
                if w in wmgr.windows:
                    wmgr.activate(w)
            self._sd_restore = []

    # the Start menu -----------------------------------------------------------
    def open_start_menu(self):
        modal = self.desk.wm.modal_top()
        if modal:                     # a modal dialog owns all input
            self.desk.wm.activate(modal)
            return
        if self.menu_open >= 0:       # pressing Start again closes it
            self.desk.menus.close_all()
            return
        shell = self.desk.shell
        MI, sub = W.MenuItem, W.sep
        # discovered freedesktop apps, grouped by category (scan once)
        groups = xdgapps.grouped()

        def app_items(bucket):
            return [MI(e["name"], icon=xdgapps.icon_for(e),
                       action=lambda e=e: xdgapps.launch(shell, e),
                       context=xdgapps.app_context(shell, e))
                    for e in groups.get(bucket, [])]

        def games():
            from kilix_sdk import settings as shared_settings
            availability = shared_settings.game_availability()
            builtin = []
            if availability["minesweeper"]:
                builtin.append(MI(
                    "Minesweeper", icon="mines",
                    action=lambda: shell.open_app("mines")))
            if availability["solitaire"]:
                builtin.append(MI(
                    "Solitaire", icon="cards",
                    action=lambda: shell.open_app("sol")))
            items = builtin + shell.game_menu_items(availability)
            disc = app_items("Games")
            if disc:
                if items:
                    items.append(sub())
                items.append(MI("System", icon="games", submenu=disc))
            if not items:
                items.append(MI("(No games enabled)", enabled=False))
            return items

        def accessories():
            items = [
                MI("Calculator", icon="calc",
                   action=lambda: shell.open_app("calc")),
                MI("Character Map", icon="charmap",
                   action=lambda: shell.open_app("charmap")),
                MI("Help", icon="help",
                   action=lambda: shell.open_app("winhelp")),
                MI("Notepad", icon="notepad",
                   action=lambda: shell.open_app("notepad")),
                MI("Paint", icon="paint",
                   action=lambda: shell.open_app("paint")),
                MI("Task Manager", icon="taskmgr",
                   action=lambda: shell.open_app("taskmgr")),
                MI("WordPad", icon="wordpad",
                   action=lambda: shell.open_app("wordpad")),
            ]
            disc = app_items("Accessories")
            if disc:
                items.append(sub())
                items.extend(disc)
            return items

        # discovered buckets that get their own Programs submenu
        OTHER_BUCKETS = ["Development", "Education", "Graphics", "Internet",
                         "Multimedia", "Office", "System", "Other"]

        def programs():
            items = [
                MI("Accessories", icon="folder", submenu=accessories()),
                MI("Games", icon="games", submenu=games()),
                MI("Kilix Applications", icon="folder",
                   submenu=shell.catalog_application_menu_items()),
                sub(),
                MI("File Manager", icon="folder_open",
                   action=lambda: shell.open_app("filemgr")),
                MI("Terminal", icon="terminal", action=shell.open_terminal),
                MI("Mux Terminal", icon="mux", action=shell.open_mux_terminal),
                MI("Pane Center" if shell.batty_mode else "PTY Sessions", icon="mux",
                   action=shell.open_pty_manager),
                MI("Tmux Manager", icon="mux",
                   action=shell.open_tmux_manager),
                MI("Web Browser", icon="browser",
                   action=lambda: shell.open_browser("firefox", "window"),
                   context=[
                       MI("Open in Window",
                          action=lambda: shell.open_browser("firefox", "window")),
                       MI("Open in Tab",
                          action=lambda: shell.open_browser("firefox", "tab")),
                       MI("Open Fullscreen",
                          action=lambda: shell.open_browser("firefox", "fullscreen")),
                   ]),
                MI("Chromium", icon="browser",
                   action=lambda: shell.open_browser("chromium", "tab"),
                   context=[
                       MI("Open in Tab",
                          action=lambda: shell.open_browser("chromium", "tab")),
                       MI("Open in Window",
                          action=lambda: shell.open_browser("chromium", "window")),
                       MI("Open Fullscreen",
                          action=lambda: shell.open_browser("chromium", "fullscreen")),
                   ]),
                # opens empty; the player has its own Open dialog (the eject
                # button → a zenity file picker) for choosing tracks
                MI("Media Player", icon="amp",
                   action=lambda: shell.open_app("amp")),
                MI("PDF Viewer", icon="doc_text",
                   action=shell.open_kilix_pdf),
                MI("Kilix Temps", icon="flame",
                   action=shell.open_kilix_temps),
                MI("Kilix Memory", icon="memory",
                   action=shell.open_kilix_memory),
            ]
            user = shell.launcher_menu_items()
            if user:
                items.append(sub())
                items.extend(user)
            disc = [MI(b, icon="folder", submenu=app_items(b))
                    for b in OTHER_BUCKETS if groups.get(b)]
            if disc:
                items.append(sub())
                items.extend(disc)
            return items

        def documents():
            docs = [MI(label, icon=icons.for_path(p),
                       action=lambda p=p: shell.open_path(p))
                    for label, p in shell.recent_docs()]
            return docs or [MI("(Empty)", enabled=False)]

        settings_sub = [
            MI("kilix Settings", icon="settings",
               action=lambda: shell.open_app("settings")),
            MI("Display…", icon="display", action=shell.display_properties),
            MI("Sounds…", icon="soundcp",
               action=lambda: shell.open_app("soundcp")),
        ]
        find_sub = [
            MI("Files or Folders…", icon="find",
               action=lambda: shell.open_app("findfiles")),
        ]
        items = [
            MI("Programs", icon="folder", submenu=programs()),
            MI("Documents", icon="doc_text", submenu=documents()),
            MI("Settings", icon="settings", submenu=settings_sub),
        ]
        sys_items = shell.system_menu_items()      # update/maintenance launchers
        if sys_items:
            items.append(MI("System", icon="computer", submenu=sys_items))
        items += [
            MI("Find", icon="find", submenu=find_sub),
            sub(),
            MI("Create Launcher…", icon="exe",
               action=lambda: shell.create_launcher_dialog()),
            MI("Run…", icon="run", action=shell.run_dialog),
            sub(),
            MI("Shut Down…", icon="shutdown", action=shell.shutdown_dialog),
        ]
        x0, y0, x1, y1 = self.rect()
        self.desk.menus.open(items, x0 + 2, y0, item_h=24,
                             sidebar=T.START_SIDEBAR, bar=self, min_w=150)
        self.menu_open = 1
        self.invalidate()


# ── tray popup furniture (chromeless, no taskbar button) ────────────────────

class _VSlider(W.Widget):
    """Vertical 0-100 volume slider."""
    focusable = True

    def __init__(self, x, y, h, value, cb):
        super().__init__(x, y, 16, h)
        self.value = value
        self.cb = cb

    def _from_y(self, py):
        rel = (self.y + self.h - 1 - py) / max(1, self.h - 1)
        return max(0, min(100, round(rel * 100)))

    def _set(self, v):
        if v != self.value:
            self.value = v
            self.invalidate()
            if self.cb:
                self.cb(v)

    def draw(self, d, img):
        cx = self.x + self.w // 2
        T.sunken(d, cx - 2, self.y, cx + 1, self.y + self.h - 1, fill=T.FACE)
        ty = self.y + (self.h - 1) - (self.h - 1) * self.value // 100
        T.raised(d, self.x, ty - 4, self.x + self.w - 1, ty + 4)
        if self.window and self.window.focus is self:
            T.focus_rect(d, self.x, ty - 4, self.x + self.w - 1, ty + 4)

    def on_mouse(self, ev):
        if (ev.press and ev.btn == 1) or (ev.move and ev.btn & 1):
            self._set(self._from_y(ev.y))
        return True

    def on_key(self, ev):
        if ev.key in ("ArrowUp", "ArrowRight", "PageUp"):
            self._set(min(100, self.value + 5))
        elif ev.key in ("ArrowDown", "ArrowLeft", "PageDown"):
            self._set(max(0, self.value - 5))
        else:
            return False
        return True


class _Popup(wm.Window):
    """Chromeless taskbar popup: no taskbar button, Escape dismisses."""

    def __init__(self, desk, taskbar, w, h, x, y):
        super().__init__(desk, "", w, h, x=x, y=y, chromeless=True)
        self._no_taskbar = True
        self.tb = taskbar

    def on_key(self, ev):
        if ev.key == "Escape":
            self.tb._close_popup()
            return True
        return super().on_key(ev)


class _VolFlyout(_Popup):
    def __init__(self, desk, taskbar, x, y):
        super().__init__(desk, taskbar, 72, 136, x, y)
        v = taskbar._volume()
        self.slider = self.add(_VSlider(28, 22, 74, v, self._set))
        self.pct = self.add(W.Label(0, 100, "%d%%" % v))
        self._center_pct()
        self.mute = self.add(W.Checkbox(8, 116, "Mute",
                                        checked=taskbar._muted(),
                                        cb=self._mute))
        self.set_focus(self.slider)

    def _center_pct(self):
        self.pct.x = (self.client_size()[0] - self.pct.w) // 2

    def _set(self, v):
        self.tb._set_volume(v)
        self.pct.set("%d%%" % v)
        self._center_pct()
        self.invalidate()

    def _mute(self, on):
        self.tb._set_muted(on)

    def draw_client(self, d, img):
        w, h = self.client_size()
        T.raised(d, 0, 0, w - 1, h - 1)
        t = "Volume"
        d.text(((w - T.text_w(T.FONT, t)) // 2, 5), t, font=T.FONT,
               fill=T.TEXT)


class _PasswordBalloon(_Popup):
    def __init__(self, desk, taskbar, x, y):
        super().__init__(desk, taskbar, 254, 100, x, y)
        self.add(W.Label(34, 9, "Change your password", bold=True))
        self.add(W.Label(34, 27, "This account still uses the default"))
        self.add(W.Label(34, 41, "password 'plebian'. Change it to"))
        self.add(W.Label(34, 55, "secure the computer."))
        change = self.add(W.Button(34, 72, 96, 22, "Change…",
                                   cb=self._change, default=True))
        self.add(W.Button(140, 72, 74, 22, "Later", cb=self._dismiss))
        self.set_focus(change)

    def _change(self):
        self.tb._close_popup()
        self.desk.shell.change_password_dialog()

    def _dismiss(self):
        self.tb._close_popup()

    def draw_client(self, d, img):
        w, h = self.client_size()
        T.raised(d, 0, 0, w - 1, h - 1)
        icons.paint(img, "warn", 8, 10, 22)


class _ClockPopup(_Popup):
    def __init__(self, desk, taskbar, x, y):
        super().__init__(desk, taskbar, 154, 160, x, y)

    def draw_client(self, d, img):
        w, h = self.client_size()
        T.raised(d, 0, 0, w - 1, h - 1)
        now = time.localtime()
        hdr = time.strftime("%B %Y", now)
        d.text(((w - T.text_w(T.BOLD, hdr)) // 2, 6), hdr, font=T.BOLD,
               fill=T.TEXT)
        T.hsep(d, 6, w - 7, 21)
        cw = (w - 12) // 7
        for i, c in enumerate(("Su", "Mo", "Tu", "We", "Th", "Fr", "Sa")):
            cx = 6 + i * cw
            d.text((cx + (cw - T.text_w(T.SMALL, c)) // 2, 26), c,
                   font=T.SMALL, fill=T.SHADOW)
        weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(
            now.tm_year, now.tm_mon)
        ry = 40
        for wk in weeks:
            for i, day in enumerate(wk):
                if day == 0:
                    continue
                cx = 6 + i * cw
                s = str(day)
                tx = cx + (cw - T.text_w(T.FONT, s)) // 2
                if day == now.tm_mday:
                    d.rectangle([cx + 1, ry - 1, cx + cw - 2, ry + 11],
                                fill=T.SEL_BG)
                    d.text((tx, ry), s, font=T.FONT, fill=T.SEL_TX)
                else:
                    d.text((tx, ry), s, font=T.FONT, fill=T.TEXT)
            ry += 14
        tstr = time.strftime("%H:%M:%S", now)
        d.text(((w - T.text_w(T.FONT, tstr)) // 2, h - 16), tstr,
               font=T.FONT, fill=T.TEXT)
