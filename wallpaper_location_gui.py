#!/usr/bin/env python3
"""GTK4 / libadwaita front end for wallpaper-location."""

import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wallpaper_location as core  # noqa: E402

FIT_OPTIONS = ["zoom", "scaled", "centered", "stretched", "spanned", "wallpaper"]
TIMER = "wallpaper-location.timer"
CSS = b".thumb { border-radius: 6px; }"


# --- helpers ----------------------------------------------------------------

def run_bg(fn, done):
    """Run fn() in a thread, then done(result, error) back on the GTK main loop."""
    def worker():
        try:
            res, err = fn(), None
        except Exception as e:
            res, err = None, e
        GLib.idle_add(lambda: done(res, err) and False)
    threading.Thread(target=worker, daemon=True).start()


def split_list(text):
    return [s.strip() for s in text.split(",") if s.strip()]


def join_list(v):
    return ", ".join(core.as_list(v)) if v else ""


def describe_rule(rule):
    parts = []
    if rule.get("ssid"):
        parts.append("Wi-Fi: " + join_list(rule["ssid"]))
    for key, label in (("city", "City"), ("region", "Region"), ("country", "Country")):
        if rule.get(key):
            parts.append(f"{label}: {join_list(rule[key])}")
    if "lat" in rule and "lon" in rule:
        parts.append(f"Within {rule.get('radius_km', 5):g} km of {rule['lat']:.4f}, {rule['lon']:.4f}")
    desc = " · ".join(parts) or "No conditions"
    if rule.get("slideshow"):
        desc += f"\nSlideshow, every {rule['slideshow']:g} min"
    return desc


def first_image(path_str):
    if not path_str:
        return None
    p = Path(path_str).expanduser()
    if p.is_dir():
        return next((x for x in sorted(p.rglob("*")) if x.suffix.lower() in core.IMAGE_EXTS), None)
    return p if p.is_file() else None


def _draw_pixbuf(cr, pb, width, height):
    cr.scale(width / pb.get_width(), height / pb.get_height())
    Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
    cr.paint()


def thumbnail(path_str, w=72, h=46):
    img = first_image(path_str)
    if img:
        try:
            # Load at 2x for HiDPI, scaled to cover the box, then center-crop.
            _fmt, iw, ih = GdkPixbuf.Pixbuf.get_file_info(str(img))
            scale = max(w * 2 / iw, h * 2 / ih)
            sw, sh = max(w * 2, round(iw * scale)), max(h * 2, round(ih * scale))
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(img), sw, sh, False)
            pb = pb.new_subpixbuf((sw - w * 2) // 2, (sh - h * 2) // 2, w * 2, h * 2)
            # DrawingArea has a fixed content size (a Picture would grow to the texture's).
            area = Gtk.DrawingArea(content_width=w, content_height=h, valign=Gtk.Align.CENTER,
                                   overflow=Gtk.Overflow.HIDDEN, css_classes=["thumb"])
            area.set_draw_func(lambda _a, cr, aw, ah: _draw_pixbuf(cr, pb, aw, ah))
            return area
        except (GLib.Error, TypeError, ZeroDivisionError):
            pass
    icon = Gtk.Image.new_from_icon_name("folder-pictures-symbolic" if img is None and path_str
                                        else "image-x-generic-symbolic")
    icon.set_pixel_size(32)
    icon.set_size_request(w, h)
    return icon


def choose_wallpaper(parent, folder, callback):
    """Open a file chooser for an image (or folder) and call callback(path)."""
    dialog = Gtk.FileDialog(title="Choose Folder" if folder else "Choose Image")
    pictures = Path.home() / "Pictures"
    if pictures.is_dir():
        dialog.set_initial_folder(Gio.File.new_for_path(str(pictures)))
    if not folder:
        flt = Gtk.FileFilter(name="Images")
        flt.add_mime_type("image/*")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dialog.set_filters(store)
        dialog.set_default_filter(flt)

    def finish(dlg, result):
        try:
            f = dlg.select_folder_finish(result) if folder else dlg.open_finish(result)
        except GLib.Error:
            return  # cancelled
        if f and f.get_path():
            callback(f.get_path())

    (dialog.select_folder if folder else dialog.open)(parent, None, finish)


def flat_button(icon, tooltip, on_click):
    btn = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
    btn.add_css_class("flat")
    btn.connect("clicked", lambda *_: on_click())
    return btn


class SlideshowRow(Adw.ExpanderRow):
    """Slideshow on/off, interval and shuffle; only usable when the wallpaper is a folder."""

    def __init__(self, rule, on_change=None):
        on = bool(rule.get("slideshow"))
        super().__init__(title="Slideshow", show_enable_switch=True,
                         enable_expansion=on, expanded=on)
        self.minutes_row = Adw.SpinRow.new_with_range(1, 1440, 5)
        self.minutes_row.set_title("Change every (minutes)")
        self.minutes_row.set_value(rule.get("slideshow") or 30)
        self.shuffle_row = Adw.SwitchRow(title="Shuffle", active=rule.get("shuffle", True))
        self.add_row(self.minutes_row)
        self.add_row(self.shuffle_row)
        if on_change:
            self.connect("notify::enable-expansion", lambda *_: on_change())
            self.minutes_row.connect("notify::value", lambda *_: on_change())
            self.shuffle_row.connect("notify::active", lambda *_: on_change())

    def set_folder(self, path_str):
        is_dir = bool(path_str) and Path(path_str).expanduser().is_dir()
        self.set_sensitive(is_dir)
        self.set_subtitle("Cycle through every image in the folder" if is_dir
                          else "Choose a folder to use a slideshow")

    def values(self):
        if not (self.get_sensitive() and self.get_enable_expansion()):
            return {}
        vals = {"slideshow": int(self.minutes_row.get_value())}
        if not self.shuffle_row.get_active():
            vals["shuffle"] = False
        return vals


def systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None


# --- edit dialog ------------------------------------------------------------

class RuleDialog(Adw.Dialog):
    def __init__(self, rule, loc, on_save):
        super().__init__(title="Edit Place" if rule else "New Place",
                         content_width=500, content_height=720)
        self.rule = dict(rule or {})
        self.loc = loc or {}
        self.on_save = on_save

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self.save())
        header.pack_start(cancel)
        header.pack_end(save)

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.toasts)
        self.set_child(toolbar)

        page = Adw.PreferencesPage()
        self.toasts.set_child(page)

        g = Adw.PreferencesGroup()
        self.name_row = Adw.EntryRow(title="Name", text=self.rule.get("name", ""))
        g.add(self.name_row)
        page.add(g)

        g = Adw.PreferencesGroup(
            title="Match When",
            description="Every filled-in condition must hold. Separate multiple values with commas.")
        self.ssid_row = self._entry(g, "Wi-Fi network", "ssid")
        self.city_row = self._entry(g, "City", "city")
        self.region_row = self._entry(g, "Region / state", "region")
        self.country_row = self._entry(g, "Country code (e.g. US)", "country")
        page.add(g)

        g = Adw.PreferencesGroup()
        self.near_row = Adw.ExpanderRow(title="Near a point", subtitle="Match within a distance of coordinates",
                                        show_enable_switch=True, enable_expansion="lat" in self.rule,
                                        expanded="lat" in self.rule)
        self.lat_row = Adw.EntryRow(title="Latitude", text=str(self.rule.get("lat", "")))
        self.lon_row = Adw.EntryRow(title="Longitude", text=str(self.rule.get("lon", "")))
        self.radius_row = Adw.SpinRow.new_with_range(0.1, 20000, 0.5)
        self.radius_row.set_title("Radius (km)")
        self.radius_row.set_digits(1)
        self.radius_row.set_value(self.rule.get("radius_km", 5))
        here = Adw.ButtonRow(title="Use Current Location", start_icon_name="find-location-symbolic",
                             sensitive="lat" in self.loc)
        here.connect("activated", lambda *_: self._use_here())
        for row in (self.lat_row, self.lon_row, self.radius_row, here):
            self.near_row.add_row(row)
        g.add(self.near_row)
        page.add(g)

        g = Adw.PreferencesGroup(title="Wallpaper")
        self.wall_row = Adw.ActionRow(title="Image or folder", subtitle_lines=2, use_markup=False)
        self.wall_row.add_suffix(flat_button("image-x-generic-symbolic", "Choose image",
                                             lambda: choose_wallpaper(self.get_root(), False, self._set_wall)))
        self.wall_row.add_suffix(flat_button("folder-symbolic", "Choose folder (random image or slideshow)",
                                             lambda: choose_wallpaper(self.get_root(), True, self._set_wall)))
        self.thumb = None
        self.slideshow_row = SlideshowRow(self.rule)
        self._set_wall(self.rule.get("wallpaper"))
        g.add(self.wall_row)
        g.add(self.slideshow_row)
        self.fit_row = Adw.ComboRow(title="Fit",
                                    model=Gtk.StringList.new(["Use global setting"] + FIT_OPTIONS))
        opt = self.rule.get("options")
        self.fit_row.set_selected(FIT_OPTIONS.index(opt) + 1 if opt in FIT_OPTIONS else 0)
        g.add(self.fit_row)
        page.add(g)

    def _entry(self, group, title, key):
        row = Adw.EntryRow(title=title, text=join_list(self.rule.get(key)))
        current = self.loc.get(key)
        if current:
            def fill():
                vals = split_list(row.get_text())
                if current not in vals:
                    row.set_text(join_list(vals + [current]))
            row.add_suffix(flat_button("find-location-symbolic", f"Add current: {current}", fill))
        group.add(row)
        return row

    def _use_here(self):
        self.lat_row.set_text(f"{self.loc['lat']:.5f}")
        self.lon_row.set_text(f"{self.loc['lon']:.5f}")

    def _set_wall(self, path):
        self.rule["wallpaper"] = path
        self.wall_row.set_subtitle(path or "None selected")
        if self.thumb:
            self.wall_row.remove(self.thumb)
        self.thumb = thumbnail(path)
        self.wall_row.add_prefix(self.thumb)
        self.slideshow_row.set_folder(path)

    def _fail(self, msg, row=None):
        if row:
            row.add_css_class("error")
        self.toasts.add_toast(Adw.Toast(title=msg))

    def save(self):
        for row in (self.name_row, self.lat_row, self.lon_row):
            row.remove_css_class("error")
        rule = {"name": self.name_row.get_text().strip()}
        if not rule["name"]:
            return self._fail("Give this place a name", self.name_row)
        for key, row in (("ssid", self.ssid_row), ("city", self.city_row),
                         ("region", self.region_row), ("country", self.country_row)):
            vals = split_list(row.get_text())
            if vals:
                rule[key] = vals[0] if len(vals) == 1 else vals
        if self.near_row.get_enable_expansion():
            try:
                rule["lat"] = float(self.lat_row.get_text())
            except ValueError:
                return self._fail("Latitude must be a number", self.lat_row)
            try:
                rule["lon"] = float(self.lon_row.get_text())
            except ValueError:
                return self._fail("Longitude must be a number", self.lon_row)
            rule["radius_km"] = round(self.radius_row.get_value(), 1)
        if len(rule) == 1:
            return self._fail("Add at least one condition to match on")
        if not self.rule.get("wallpaper"):
            return self._fail("Choose a wallpaper")
        rule["wallpaper"] = self.rule["wallpaper"]
        rule.update(self.slideshow_row.values())
        if self.fit_row.get_selected() > 0:
            rule["options"] = FIT_OPTIONS[self.fit_row.get_selected() - 1]
        self.on_save(rule)
        self.close()


# --- main window ------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Wallpaper Location",
                         default_width=640, default_height=800)
        self.cfg = core.load_config() or {
            "options": "zoom", "detection": {"geoclue": True, "ip_lookup": True}, "rule": []}
        self.cfg.setdefault("rule", [])
        self.cfg.setdefault("detection", {})
        self.loc = None
        self.rule_rows = []

        header = Adw.HeaderBar()
        self.detect_btn = flat_button("view-refresh-symbolic", "Detect location again", self.detect)
        header.pack_start(self.detect_btn)
        self.apply_btn = Gtk.Button(label="Apply Now")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", lambda *_: self.apply_now())
        header.pack_end(self.apply_btn)

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)

        page = Adw.PreferencesPage()
        self.toasts.set_child(page)

        # Current location
        g = Adw.PreferencesGroup(title="Current Location")
        self.spinner = Adw.Spinner(visible=False)
        g.set_header_suffix(self.spinner)
        self.wifi_row = self._info_row(g, "Wi-Fi", "network-wireless-symbolic")
        self.place_row = self._info_row(g, "Place", "mark-location-symbolic")
        self.coords_row = self._info_row(g, "Coordinates", "find-location-symbolic")
        self.match_row = self._info_row(g, "Wallpaper chosen", "preferences-desktop-wallpaper-symbolic")
        page.add(g)

        # Places
        self.rules_group = Adw.PreferencesGroup(
            title="Places", description="Checked top to bottom — the first match wins.")
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add place")
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self.edit_rule(None))
        self.rules_group.set_header_suffix(add)
        page.add(self.rules_group)

        # Default
        g = Adw.PreferencesGroup(title="Everywhere Else",
                                 description="Used when no place matches.")
        self.default_row = Adw.ActionRow(title="Default wallpaper", subtitle_lines=2, use_markup=False)
        self.default_row.add_suffix(flat_button(
            "image-x-generic-symbolic", "Choose image",
            lambda: choose_wallpaper(self, False, self.set_default)))
        self.default_row.add_suffix(flat_button(
            "folder-symbolic", "Choose folder (random image or slideshow)",
            lambda: choose_wallpaper(self, True, self.set_default)))
        self.default_row.add_suffix(flat_button(
            "edit-clear-symbolic", "Don't change wallpaper when nowhere matches",
            lambda: self.set_default(None)))
        self.default_thumb = None
        g.add(self.default_row)
        self.default_slideshow = SlideshowRow(self.cfg.get("default", {}), self._on_default_slideshow)
        g.add(self.default_slideshow)
        page.add(g)

        # Options
        g = Adw.PreferencesGroup(title="Options")
        self.auto_row = Adw.SwitchRow(title="Change automatically",
                                      subtitle="Check your location every 10 minutes in the background")
        g.add(self.auto_row)
        det = self.cfg["detection"]
        self.geoclue_row = Adw.SwitchRow(title="Use GNOME Location Services",
                                         subtitle="Needs Settings › Privacy › Location turned on",
                                         active=det.get("geoclue", True))
        self.ip_row = Adw.SwitchRow(title="Use IP address lookup",
                                    subtitle="Sends your public IP to ipinfo.io to find your city",
                                    active=det.get("ip_lookup", True))
        for row, key in ((self.geoclue_row, "geoclue"), (self.ip_row, "ip_lookup")):
            row.connect("notify::active", self._on_detection_toggled, key)
            g.add(row)
        self.fit_row = Adw.ComboRow(title="Fit", model=Gtk.StringList.new(FIT_OPTIONS))
        opt = self.cfg.get("options", "zoom")
        self.fit_row.set_selected(FIT_OPTIONS.index(opt) if opt in FIT_OPTIONS else 0)
        self.fit_row.connect("notify::selected", self._on_fit_changed)
        g.add(self.fit_row)
        page.add(g)

        self.refresh_rules()
        self.set_default(self.cfg.get("default", {}).get("wallpaper"), save=False)
        self.init_timer_row()
        self.detect()

    # -- current location --

    def _info_row(self, group, title, icon):
        row = Adw.ActionRow(title=title, subtitle="…", icon_name=icon, use_markup=False)
        row.add_css_class("property")
        group.add(row)
        return row

    def detect(self):
        self.spinner.set_visible(True)
        self.detect_btn.set_sensitive(False)
        run_bg(lambda: core.detect_location(self.cfg), self._on_detected)

    def _on_detected(self, loc, err):
        self.spinner.set_visible(False)
        self.detect_btn.set_sensitive(True)
        if err:
            self.toasts.add_toast(Adw.Toast(title=f"Location detection failed: {err}"))
            return
        self.loc = loc
        self.wifi_row.set_subtitle(loc.get("ssid") or "Not connected")
        place = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
        self.place_row.set_subtitle(place or "Unknown")
        if "lat" in loc:
            src = {"geoclue": "Location Services", "ip": "IP lookup"}.get(loc.get("source"), "")
            self.coords_row.set_subtitle(f"{loc['lat']:.4f}, {loc['lon']:.4f}  ({src})")
        else:
            self.coords_row.set_subtitle("Unknown")
        self.update_match()

    def update_match(self):
        if self.loc is None:
            return
        rule = core.pick_rule(self.cfg, self.loc)
        self.match_row.set_subtitle(
            "Nothing matches — wallpaper left alone" if rule is None
            else "Default wallpaper" if rule.get("name") == "default"
            else rule.get("name", "?"))

    # -- places --

    def refresh_rules(self):
        for row in self.rule_rows:
            self.rules_group.remove(row)
        self.rule_rows = []
        rules = self.cfg["rule"]
        if not rules:
            row = Adw.ActionRow(title="No places yet", subtitle="Use + to add home, work, a city…")
            row.add_css_class("dim-label")
            self.rule_rows.append(row)
            self.rules_group.add(row)
        for i, rule in enumerate(rules):
            row = Adw.ActionRow(title=rule.get("name", "Unnamed"), subtitle=describe_rule(rule),
                                activatable=True, subtitle_lines=2, use_markup=False)
            row.add_prefix(thumbnail(rule.get("wallpaper")))
            up = flat_button("go-up-symbolic", "Move up (higher priority)", lambda i=i: self.move_rule(i, -1))
            up.set_sensitive(i > 0)
            down = flat_button("go-down-symbolic", "Move down", lambda i=i: self.move_rule(i, 1))
            down.set_sensitive(i < len(rules) - 1)
            for w in (up, down, flat_button("user-trash-symbolic", "Remove", lambda i=i: self.delete_rule(i))):
                row.add_suffix(w)
            row.connect("activated", lambda _r, i=i: self.edit_rule(i))
            self.rule_rows.append(row)
            self.rules_group.add(row)
        self.update_match()

    def edit_rule(self, index):
        rule = self.cfg["rule"][index] if index is not None else None

        def on_save(new):
            if index is None:
                self.cfg["rule"].append(new)
            else:
                self.cfg["rule"][index] = new
            self.save()
            self.refresh_rules()

        RuleDialog(rule, self.loc, on_save).present(self)

    def move_rule(self, i, delta):
        rules = self.cfg["rule"]
        rules[i], rules[i + delta] = rules[i + delta], rules[i]
        self.save()
        self.refresh_rules()

    def delete_rule(self, i):
        removed = self.cfg["rule"].pop(i)
        self.save()
        self.refresh_rules()
        toast = Adw.Toast(title=f"Removed “{removed.get('name', 'place')}”", button_label="Undo")

        def undo(*_):
            self.cfg["rule"].insert(i, removed)
            self.save()
            self.refresh_rules()
        toast.connect("button-clicked", undo)
        self.toasts.add_toast(toast)

    def set_default(self, path, save=True):
        if path:
            self.cfg.setdefault("default", {})["wallpaper"] = path
        else:
            self.cfg.pop("default", None)
        self.default_row.set_subtitle(path or "None — keep the current wallpaper")
        if self.default_thumb:
            self.default_row.remove(self.default_thumb)
        self.default_thumb = thumbnail(path)
        self.default_row.add_prefix(self.default_thumb)
        self.default_slideshow.set_folder(path)
        if save:
            self._on_default_slideshow()
            self.update_match()

    def _on_default_slideshow(self):
        default = self.cfg.get("default")
        if default:
            for key in ("slideshow", "shuffle"):
                default.pop(key, None)
            default.update(self.default_slideshow.values())
        self.save()

    # -- options --

    def _on_detection_toggled(self, row, _pspec, key):
        self.cfg["detection"][key] = row.get_active()
        self.save()
        self.detect()

    def _on_fit_changed(self, row, _pspec):
        self.cfg["options"] = FIT_OPTIONS[row.get_selected()]
        self.save()

    def init_timer_row(self):
        res = systemctl("is-enabled", TIMER)
        state = res.stdout.strip() if res else ""
        if state not in ("enabled", "disabled"):
            self.auto_row.set_sensitive(False)
            self.auto_row.set_subtitle("Run install.sh to enable background checks")
            return
        self.auto_row.set_active(state == "enabled")
        self.auto_row.connect("notify::active", self._on_auto_toggled)

    def _on_auto_toggled(self, row, _pspec):
        res = systemctl("enable" if row.get_active() else "disable", "--now", TIMER)
        if not res or res.returncode != 0:
            self.toasts.add_toast(Adw.Toast(title="Couldn't change the background timer"))

    # -- actions --

    def save(self):
        try:
            core.save_config(self.cfg)
        except OSError as e:
            self.toasts.add_toast(Adw.Toast(title=f"Couldn't save config: {e}"))

    def apply_now(self):
        self.apply_btn.set_sensitive(False)
        loc = self.loc
        run_bg(lambda: core.apply(self.cfg, loc, force=True), self._on_applied)

    def _on_applied(self, res, err):
        self.apply_btn.set_sensitive(True)
        if err:
            msg = f"Couldn't set wallpaper: {err}"
        elif res[0] is None:
            msg = "No place matched and no default is set"
        else:
            msg = f"Wallpaper set for “{res[0]}”"
        self.toasts.add_toast(Adw.Toast(title=msg))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=core.DESKTOP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        (self.props.active_window or Window(self)).present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
