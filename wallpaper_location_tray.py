#!/usr/bin/env python3
"""Tray icon for wallpaper-location: next wallpaper, pause, open the current
image, and pick a place by hand.

GNOME needs the "AppIndicator and KStatusNotifierItem Support" extension to
show tray icons. AppIndicator menus are GTK 3, so this runs apart from the
GTK 4 settings window.
"""

import subprocess
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import Gio, GLib, Gtk  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import wallpaper_location as core  # noqa: E402

TRAY_ID = core.DESKTOP_ID + ".Tray"
ICON = "preferences-desktop-wallpaper-symbolic"
REFRESH_SECS = 5
AUTO = None  # override value meaning "pick by location"


def menu_label(text, limit=48):
    """Shorten text for a menu, and escape underscores (dbusmenu reads them as mnemonics)."""
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return text.replace("_", "__")


def format_time(ts):
    """Date and time, following GNOME's 12/24-hour clock setting."""
    try:
        twelve = Gio.Settings.new("org.gnome.desktop.interface").get_string("clock-format") == "12h"
    except Exception:
        twelve = False
    return time.strftime("%a %b %-d, %-I:%M %p" if twelve else "%a %b %-d, %H:%M", time.localtime(ts))


def item(label, on_activate=None, cls=Gtk.MenuItem, **kw):
    it = cls(label=menu_label(label), use_underline=True, **kw)
    if on_activate:
        it.connect("activate", lambda *_: on_activate())
    return it


class Tray(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=TRAY_ID)
        self.updating = False  # set while refresh() changes toggles, so handlers ignore it
        self.busy = False
        self.place_items = {}

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()  # no windows; keep running
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)

        self.cfg = core.load_config()
        self.indicator = AppIndicator.Indicator.new(
            core.APP_ID, ICON, AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_title("Wallpaper Location")
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.build_menu()

        self.monitors = []
        for path, on_change in ((core.CONFIG_PATH, self.reload_config), (core.STATE_PATH, self.refresh)):
            path.parent.mkdir(parents=True, exist_ok=True)
            mon = Gio.File.new_for_path(str(path)).monitor_file(Gio.FileMonitorFlags.NONE, None)
            mon.connect("changed", lambda *_a, f=on_change: f())
            self.monitors.append(mon)
        # The slideshow moves on by itself, so keep "Current" up to date.
        GLib.timeout_add_seconds(REFRESH_SECS, lambda: self.refresh() or True)
        self.refresh()

    def do_activate(self):
        pass  # launched again while running: nothing to show

    # -- menu --

    def build_menu(self):
        menu = Gtk.Menu()
        self.current_item = item("Current: unknown", self.open_current)
        self.changed_item = item("Last changed: unknown", sensitive=False)
        self.reveal_item = item("Show in Files", self.reveal_current)
        self.next_item = item("Next Wallpaper", lambda: self.run_task(core.next_wallpaper, self.cfg))
        self.pause_item = item("Pause Slideshow", cls=Gtk.CheckMenuItem)
        self.pause_item.connect("toggled", self.on_pause_toggled)
        self.place_item = item("Place")
        for it in (self.current_item, self.changed_item, self.reveal_item, Gtk.SeparatorMenuItem(),
                   self.next_item, self.pause_item, Gtk.SeparatorMenuItem(),
                   self.place_item, Gtk.SeparatorMenuItem(),
                   item("Settings…", self.open_settings), item("Quit Tray Icon", self.quit)):
            menu.append(it)
        menu.show_all()
        self.indicator.set_menu(menu)
        self.indicator.set_secondary_activate_target(self.next_item)  # middle click = next
        self.build_places()

    def build_places(self):
        sub = Gtk.Menu()
        self.place_items = {}
        group = None
        names = [(AUTO, "Automatic (by location)")]
        names += [(r.get("name", r.get("wallpaper")),) * 2 for r in self.cfg.get("rule", [])]
        if "default" in self.cfg:
            names.append(("default", "Everywhere else (default)"))
        for i, (name, label) in enumerate(names):
            it = Gtk.RadioMenuItem(label=menu_label(label), use_underline=True, group=group)
            group = group or it
            it.connect("toggled", self.on_place_toggled, name)
            self.place_items[name] = it
            sub.append(it)
            if i == 0 and len(names) > 1:
                sub.append(Gtk.SeparatorMenuItem())
        sub.show_all()
        self.place_item.set_submenu(sub)

    def refresh(self):
        state = core.load_state()
        current = core.current_wallpaper(state)
        ours = core._is_ours(state)
        slideshow = ours and core.current_slide(state) is not None
        rule = core.find_rule(self.cfg, state.get("rule")) if ours else None
        override = state.get("override") if core.find_rule(self.cfg, state.get("override")) else AUTO

        self.updating = True
        self.current_item.set_label(menu_label(f"Current: {current.name}" if current else "Current: unknown"))
        self.current_item.set_sensitive(current is not None)
        changed = core.last_changed(state)
        self.changed_item.set_label(menu_label(
            f"Last changed: {format_time(changed)}" if changed else "Last changed: unknown"))
        self.reveal_item.set_sensitive(current is not None)
        folder = rule is not None and Path(rule["wallpaper"]).expanduser().is_dir()
        self.next_item.set_sensitive(folder and not self.busy)
        self.pause_item.set_sensitive(slideshow and not self.busy)
        self.pause_item.set_label(menu_label(
            "Pause Slideshow" if slideshow or not rule
            else f"Pause Slideshow (turn on Slideshow for {rule.get('name', 'this place')})" if folder
            else "Pause Slideshow (not a folder)", limit=60))
        self.pause_item.set_active(bool(state.get("paused")))
        self.place_item.set_sensitive(not self.busy)
        if override in self.place_items:
            self.place_items[override].set_active(True)
        self.updating = False

    def reload_config(self):
        try:
            self.cfg = core.load_config()
        except Exception:
            return  # half-written file; the next change event will bring the rest
        self.build_places()
        self.refresh()

    # -- actions --

    def run_task(self, fn, *args):
        """Run a core call off the main loop (location detection can take seconds)."""
        if self.busy:
            return
        self.busy = True
        self.refresh()

        def worker():
            try:
                fn(*args)
                err = None
            except Exception as e:
                err = e
            GLib.idle_add(self._done, err)
        threading.Thread(target=worker, daemon=True).start()

    def _done(self, err):
        self.busy = False
        self.refresh()
        if err:
            note = Gio.Notification.new("Couldn't change the wallpaper")
            note.set_body(str(err))
            self.send_notification(None, note)
        return False

    def on_pause_toggled(self, it):
        if not self.updating:
            self.run_task(core.set_paused, it.get_active())

    def on_place_toggled(self, it, name):
        if not self.updating and it.get_active():
            self.run_task(core.set_override, self.cfg, name)

    def open_current(self):
        current = core.current_wallpaper()
        if current:
            Gio.AppInfo.launch_default_for_uri(current.as_uri(), None)

    def reveal_current(self):
        current = core.current_wallpaper()
        if not current:
            return
        try:
            Gio.bus_get_sync(Gio.BusType.SESSION).call_sync(
                "org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                "org.freedesktop.FileManager1", "ShowItems",
                GLib.Variant("(ass)", ([current.as_uri()], "")), None, Gio.DBusCallFlags.NONE, 5000)
        except GLib.Error:
            Gio.AppInfo.launch_default_for_uri(current.parent.as_uri(), None)

    def open_settings(self):
        subprocess.Popen([sys.executable, str(HERE / "wallpaper_location_gui.py")],
                         start_new_session=True)


if __name__ == "__main__":
    sys.exit(Tray().run(sys.argv))
