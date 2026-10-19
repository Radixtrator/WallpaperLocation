#!/usr/bin/env python3
"""Change the GNOME wallpaper based on where you are.

Location is detected (in order) from:
  1. the connected Wi-Fi network name (SSID), via nmcli
  2. GeoClue (GNOME's location service; needs Location Services enabled)
  3. IP geolocation via ipinfo.io (can be disabled in the config)

The first rule in the config that matches wins. See config.example.toml.
"""

import argparse
import json
import math
import random
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

APP_ID = "wallpaper-location"
# Must match the .desktop file name; GeoClue authorizes apps by it.
DESKTOP_ID = "io.github.radixtrator.WallpaperLocation"
CONFIG_PATH = Path.home() / ".config" / APP_ID / "config.toml"
STATE_PATH = Path.home() / ".local" / "state" / APP_ID / "state.json"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".svg", ".jxl", ".avif"}
TRANSITION_SECS = 2.0
MIN_SLIDE_SECS = 5
# How far ahead a shuffled slideshow is planned, and a cap on its length.
SLIDESHOW_SPAN_SECS = 7 * 24 * 3600
SLIDESHOW_MAX_SLIDES = 5000


def log(msg):
    print(msg, file=sys.stderr)


# --- location detection -----------------------------------------------------

def detect_ssid():
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi", "list", "--rescan", "no"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        active, _, ssid = line.partition(":")
        if active == "yes" and ssid:
            return ssid.replace("\\:", ":")
    return None


def detect_geoclue(timeout=10):
    try:
        import gi
        gi.require_version("Geoclue", "2.0")
        from gi.repository import Geoclue
    except (ImportError, ValueError):
        return None

    result = {}

    def worker():
        try:
            simple = Geoclue.Simple.new_sync(DESKTOP_ID, Geoclue.AccuracyLevel.CITY, None)
            loc = simple.get_location()
            result["lat"], result["lon"] = loc.get_property("latitude"), loc.get_property("longitude")
        except Exception as e:  # GLib.Error when denied/disabled
            result["error"] = str(e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if "lat" in result:
        return {"lat": result["lat"], "lon": result["lon"], "source": "geoclue"}
    if "error" in result:
        log(f"geoclue: {result['error']}")
    return None


def detect_ip(timeout=8):
    try:
        req = urllib.request.Request("https://ipinfo.io/json", headers={"User-Agent": APP_ID})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except Exception as e:
        log(f"ip lookup failed: {e}")
        return None
    loc = {"city": data.get("city"), "region": data.get("region"),
           "country": data.get("country"), "source": "ip"}
    if "," in data.get("loc", ""):
        lat, lon = data["loc"].split(",")
        loc["lat"], loc["lon"] = float(lat), float(lon)
    return loc


def detect_location(cfg):
    det = cfg.get("detection", {})
    loc = {"ssid": detect_ssid()}
    if det.get("geoclue", True):
        loc.update(detect_geoclue() or {})
    # IP lookup also supplies city/country names, so run it unless disabled.
    if det.get("ip_lookup", True):
        ip = detect_ip() or {}
        for k, v in ip.items():
            loc.setdefault(k, v)
    return loc


# --- rule matching ----------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def as_list(v):
    return v if isinstance(v, list) else [v]


def rule_matches(rule, loc):
    """A rule matches if every condition it specifies is satisfied."""
    checks = []
    if "ssid" in rule:
        checks.append(loc.get("ssid") in as_list(rule["ssid"]))
    for key in ("city", "region", "country"):
        if key in rule:
            want = {s.casefold() for s in as_list(rule[key])}
            checks.append((loc.get(key) or "").casefold() in want)
    if "lat" in rule and "lon" in rule:
        if "lat" in loc:
            dist = haversine_km(rule["lat"], rule["lon"], loc["lat"], loc["lon"])
            checks.append(dist <= rule.get("radius_km", 5))
        else:
            checks.append(False)
    return bool(checks) and all(checks)


def pick_rule(cfg, loc):
    for rule in cfg.get("rule", []):
        if rule_matches(rule, loc):
            return rule
    if "default" in cfg:
        return {"name": "default", **cfg["default"]}
    return None


# --- wallpaper --------------------------------------------------------------

def list_images(folder):
    """All images in a folder, including subfolders."""
    return sorted(p for p in folder.rglob("*") if p.suffix.lower() in IMAGE_EXTS and p.is_file())


def folder_images(path):
    images = list_images(path)
    if not images:
        raise FileNotFoundError(f"no images in {path}")
    return images


def resolve_image(path_str):
    path = Path(path_str).expanduser()
    if path.is_dir():
        return random.choice(folder_images(path))
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def slide_seconds(value):
    """Parse a slideshow duration: a number of minutes, or "30s" / "10m" / "2h"."""
    if isinstance(value, str):
        units = {"s": 1, "m": 60, "h": 3600}
        v = value.strip().lower()
        secs = float(v[:-1]) * units[v[-1]] if v and v[-1] in units else float(v) * 60
    else:
        secs = float(value) * 60
    return max(secs, MIN_SLIDE_SECS)


def shuffled_passes(images, secs):
    """Several back-to-back shuffled passes over images.

    Each pass shows every image exactly once, and a new pass never starts with
    the image the previous one ended on, so nothing repeats back to back.
    """
    passes = max(1, min(math.ceil(SLIDESHOW_SPAN_SECS / (secs * len(images))),
                        SLIDESHOW_MAX_SLIDES // len(images)))
    order = []
    for _ in range(passes):
        p = random.sample(images, len(images))
        if order and len(p) > 1 and p[0] == order[-1]:
            j = random.randrange(1, len(p))
            p[0], p[j] = p[j], p[0]
        order += p
    # The XML loops, so the last slide must not equal the first either.
    if len(images) > 1 and order[-1] == order[0]:
        order[-1], order[-2] = order[-2], order[-1]
    return order


def write_slideshow(order, secs, start):
    """Write a GNOME background slideshow XML that loops through order from start.

    GNOME plays it natively (with a cross-fade), so no timer is needed for the
    image changes. A fresh file name each time makes GNOME notice the change.
    """
    t = time.localtime(start)
    parts = ["<background>\n  <starttime>"
             f"<year>{t.tm_year}</year><month>{t.tm_mon}</month><day>{t.tm_mday}</day>"
             f"<hour>{t.tm_hour}</hour><minute>{t.tm_min}</minute><second>{t.tm_sec}</second>"
             "</starttime>\n"]
    for img, nxt in zip(order, order[1:] + order[:1]):
        a, b = escape(str(img)), escape(str(nxt))
        parts.append(f"  <static><duration>{secs - TRANSITION_SECS}</duration><file>{a}</file></static>\n")
        if len(order) > 1:
            parts.append(f'  <transition type="overlay"><duration>{TRANSITION_SECS}</duration>'
                         f"<from>{a}</from><to>{b}</to></transition>\n")
    parts.append("</background>\n")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    path = STATE_PATH.parent / f"slideshow-{time.time_ns()}.xml"
    path.write_text("".join(parts))
    return path


def remove_slideshows(keep=None):
    for old in STATE_PATH.parent.glob("slideshow-*.xml"):
        if old != keep:
            old.unlink(missing_ok=True)


def is_slideshow(rule):
    return bool(rule.get("slideshow")) and Path(rule["wallpaper"]).expanduser().is_dir()


def slideshow_order(rule, shuffle=None):
    """(images in play order, seconds per image) for a slideshow rule.

    shuffle overrides the rule's own shuffle setting when not None.
    """
    images = [p.resolve() for p in folder_images(Path(rule["wallpaper"]).expanduser())]
    secs = max(slide_seconds(rule["slideshow"]), TRANSITION_SECS + 1)
    if shuffle is None:
        shuffle = rule.get("shuffle", True)
    if shuffle and len(images) > 1:
        images = shuffled_passes(images, secs)
    return images, secs


def set_wallpaper(image, options=None):
    uri = image.resolve().as_uri()
    schema = "org.gnome.desktop.background"
    pairs = [("picture-uri", uri), ("picture-uri-dark", uri)]
    if options:
        pairs.append(("picture-options", options))
    for key, val in pairs:
        subprocess.run(["gsettings", "set", schema, key, val], check=True)


def gnome_wallpaper():
    """The file GNOME is currently showing (an image or a slideshow XML), or None."""
    try:
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.background", "picture-uri"],
                             capture_output=True, text=True, timeout=5).stdout.strip().strip("'")
    except (OSError, subprocess.TimeoutExpired):
        return None
    url = urllib.parse.urlparse(out)
    return Path(urllib.parse.unquote(url.path)) if url.scheme == "file" else None


def load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


# --- slideshow controls -----------------------------------------------------
#
# state.json remembers what was applied: "rule", "image" (the file handed to
# GNOME), and for a slideshow {"order", "secs", "start"} so the image on screen
# can be worked out from the clock. "paused" and "override" are the tray toggles.

def _is_ours(state):
    """True if the desktop still shows what this tool last set."""
    return bool(state.get("image")) and gnome_wallpaper() == Path(state["image"])


def current_slide(state):
    """Index into the slideshow order of the image on screen, or None."""
    show = state.get("slideshow")
    if not show or not show.get("order"):
        return None
    if state.get("paused"):
        return 0  # pausing rotates the order so the frozen image is first
    return int((time.time() - show["start"]) // show["secs"]) % len(show["order"])


def current_wallpaper(state=None):
    """The image currently on the desktop, or None if unknown."""
    state = load_state() if state is None else state
    if _is_ours(state):
        i = current_slide(state)
        if i is not None:
            return Path(state["slideshow"]["order"][i])
    image = gnome_wallpaper()
    return image if image and image.suffix.lower() != ".xml" else None


def play(state, order, secs, options=None):
    """Put a slideshow on the desktop (just its first image while paused) and save state."""
    start = int(time.time())
    if state.get("paused") or len(order) == 1:
        image = Path(order[0])
    else:
        image = write_slideshow(order, secs, start)
    try:
        set_wallpaper(image, options)
    except Exception:
        if image.parent == STATE_PATH.parent:
            image.unlink(missing_ok=True)
        raise
    remove_slideshows(keep=image)
    state["image"] = str(image.resolve())
    state["slideshow"] = {"order": [str(p) for p in order], "secs": secs, "start": start}
    save_state(state)
    return image


def _rotate(order, i):
    return order[i:] + order[:i]


def next_wallpaper(cfg):
    """Skip to the next slideshow image, or another random image from a folder.

    Returns the new image, or None if the current place has nothing to skip to.
    """
    state = load_state()
    i = current_slide(state) if _is_ours(state) else None
    if i is not None:
        show = state["slideshow"]
        play(state, _rotate(show["order"], (i + 1) % len(show["order"])), show["secs"])
        return Path(state["slideshow"]["order"][0])
    rule = find_rule(cfg, state.get("rule"))
    if rule and Path(rule["wallpaper"]).expanduser().is_dir():
        current = current_wallpaper(state)
        images = folder_images(Path(rule["wallpaper"]).expanduser())
        image = random.choice([p for p in images if p.resolve() != current] or images)
        set_wallpaper(image)
        remove_slideshows()
        state.pop("slideshow", None)
        state["image"] = str(image.resolve())
        save_state(state)
        return image
    return None


def set_paused(paused):
    """Freeze the slideshow on the image showing now, or carry on from it."""
    state = load_state()
    if bool(state.get("paused")) == paused:
        return
    i = current_slide(state) if _is_ours(state) else None  # before flipping "paused"
    if paused:
        state["paused"] = True
    else:
        state.pop("paused", None)
    if i is None:
        save_state(state)  # nothing playing; applies to the next slideshow
        return
    show = state["slideshow"]
    play(state, _rotate(show["order"], i), show["secs"])


def find_rule(cfg, name):
    if not name:
        return None
    for rule in cfg.get("rule", []):
        if rule.get("name", rule.get("wallpaper")) == name:
            return rule
    if name == "default" and "default" in cfg:
        return {"name": "default", **cfg["default"]}
    return None


def override_rule(cfg, state=None):
    """The place picked by hand, if any (and it still exists)."""
    state = load_state() if state is None else state
    return find_rule(cfg, state.get("override"))


def set_override(cfg, name):
    """Use place `name` regardless of location; None goes back to automatic."""
    state = load_state()
    if name:
        state["override"] = name
    else:
        state.pop("override", None)
    save_state(state)
    return apply(cfg, force=True)


# --- config -----------------------------------------------------------------

def load_config(path=CONFIG_PATH):
    try:
        return tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}


def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)  # JSON strings are valid TOML


def _toml_table(d):
    return "".join(f"{k} = {_toml_value(v)}\n" for k, v in d.items() if v not in (None, "", []))


def save_config(cfg, path=CONFIG_PATH):
    """Write the (flat) config structure this tool uses back out as TOML."""
    top = {k: v for k, v in cfg.items() if not isinstance(v, (dict, list)) or
           (isinstance(v, list) and not any(isinstance(x, dict) for x in v))}
    out = ["# wallpaper-location config (written by the GUI)\n", _toml_table(top)]
    if cfg.get("detection"):
        out.append("\n[detection]\n" + _toml_table(cfg["detection"]))
    for rule in cfg.get("rule", []):
        out.append("\n[[rule]]\n" + _toml_table(rule))
    if cfg.get("default"):
        out.append("\n[default]\n" + _toml_table(cfg["default"]))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(out))


def apply(cfg, loc=None, force=False, dry_run=False, shuffle=None):
    """Pick a rule (the manual override, else by location) and set the wallpaper.

    Location is detected unless given. shuffle=True/False overrides the matched
    slideshow's own shuffle setting.

    Returns (rule_name, image_path, changed); rule_name is None if nothing matched.
    """
    state = load_state()
    rule = override_rule(cfg, state)
    if rule is None:
        if loc is None:
            loc = detect_location(cfg)
        rule = pick_rule(cfg, loc)
    if rule is None:
        return None, None, False
    name = rule.get("name", rule.get("wallpaper"))
    if state.get("rule") == name and not force:
        return name, state.get("image"), False
    if dry_run:
        if is_slideshow(rule):
            path = Path(rule["wallpaper"]).expanduser()
            return name, f"slideshow of {len(folder_images(path))} images in {path}", True
        return name, resolve_image(rule["wallpaper"]), True
    options = rule.get("options") or cfg.get("options", "zoom")
    state["rule"] = name
    if is_slideshow(rule):
        image = play(state, *slideshow_order(rule, shuffle), options)
        return name, image, True
    if rule.get("slideshow"):
        log(f"slideshow needs a folder, {rule['wallpaper']} isn't one; using it as a single image")
    image = resolve_image(rule["wallpaper"])
    set_wallpaper(image, options)
    remove_slideshows()
    state.pop("slideshow", None)
    state["image"] = str(image.resolve())
    save_state(state)
    return name, image, True


# --- watching for wake-up and network changes ------------------------------

SETTLE_SECS = 3             # let a new connection finish coming up before checking
RESUME_FALLBACK_SECS = 20   # check this long after waking even if the network stays the same


def check(cfg, force=False, dry_run=False, shuffle=False):
    """Pick and apply the wallpaper for here, logging what happened."""
    loc = None if override_rule(cfg) else detect_location(cfg)
    name, image, changed = apply(cfg, loc, force=force or shuffle, dry_run=dry_run,
                                 shuffle=shuffle or None)
    if name is None:
        log(f"no rule matched and no [default] set; location: {loc}")
    elif not changed:
        log(f"still at '{name}', leaving wallpaper alone")
    else:
        how = "manually chosen" if loc is None else "location matched"
        print(f"{how} '{name}' -> {image}", flush=True)


def watch(config_path):
    """Check soon after waking from suspend and whenever the network connection changes.

    Events while going to sleep, and plain disconnects, are ignored: with no
    network nothing can be detected, and a check then would wrongly fall back
    to [default] (which the lock screen would then show).
    """
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
    st = {"sleeping": False, "timer": 0, "running": False, "again": False}

    def schedule(secs, why):
        if st["timer"]:
            GLib.source_remove(st["timer"])
        st["timer"] = GLib.timeout_add_seconds(secs, fire, why)

    def fire(why):
        st["timer"] = 0
        if st["running"]:
            st["again"] = True
            return False
        st["running"] = True

        def worker():
            log(f"checking: {why}")
            try:
                check(load_config(config_path))
            except Exception as e:  # keep watching whatever happens
                log(f"check failed: {e}")
            GLib.idle_add(finished)
        threading.Thread(target=worker, daemon=True).start()
        return False

    def finished():
        st["running"] = False
        if st["again"]:
            st["again"] = False
            schedule(SETTLE_SECS, "network changed during the last check")
        return False

    def on_sleep(*args):
        going = args[-1].unpack()[0]
        st["sleeping"] = going
        if going and st["timer"]:
            GLib.source_remove(st["timer"])
            st["timer"] = 0
        elif not going:
            schedule(RESUME_FALLBACK_SECS, "woke up")

    def on_network(*args):
        _iface, changed, _ = args[-1].unpack()
        if st["sleeping"] or changed.get("PrimaryConnection", "/") == "/":
            return
        schedule(SETTLE_SECS, "network changed")

    bus.signal_subscribe("org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
                         "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE, on_sleep)
    bus.signal_subscribe("org.freedesktop.NetworkManager", "org.freedesktop.DBus.Properties",
                         "PropertiesChanged", "/org/freedesktop/NetworkManager",
                         "org.freedesktop.NetworkManager", Gio.DBusSignalFlags.NONE, on_network)
    log("watching for wake-up and network changes")
    GLib.MainLoop().run()


# --- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "where", "shuffle", "next", "pause", "resume", "watch"],
                    help="run: apply wallpaper (default); where: just print detected location; "
                         "shuffle: re-apply with a freshly shuffled slideshow; "
                         "next: skip to the next image; pause/resume: freeze or continue the slideshow; "
                         "watch: stay running and check after waking up or changing networks")
    ap.add_argument("-c", "--config", type=Path, default=CONFIG_PATH)
    ap.add_argument("-n", "--dry-run", action="store_true", help="don't change the wallpaper")
    ap.add_argument("-f", "--force", action="store_true",
                    help="apply even if the matched rule hasn't changed since last run")
    args = ap.parse_args()

    try:
        cfg = tomllib.loads(args.config.read_text())
    except FileNotFoundError:
        if args.command != "where":
            sys.exit(f"No config at {args.config} - copy config.example.toml there.")
        cfg = {}

    if args.command == "where":
        print(json.dumps(detect_location(cfg), indent=2))
        return
    if args.command == "next":
        image = next_wallpaper(cfg)
        print(image if image else "nothing to skip to: the current place isn't a folder")
        return
    if args.command in ("pause", "resume"):
        set_paused(args.command == "pause")
        return
    if args.command == "watch":
        watch(args.config)
        return

    check(cfg, force=args.force, dry_run=args.dry_run, shuffle=args.command == "shuffle")


if __name__ == "__main__":
    main()
