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


def write_slideshow(images, minutes, shuffle=True):
    """Write a GNOME background slideshow XML cycling through images.

    GNOME plays it natively (with a cross-fade), so no timer is needed for the
    image changes. A fresh file name each time makes GNOME notice the change.
    """
    images = list(images)
    if shuffle:
        random.shuffle(images)
    secs = max(float(minutes) * 60, TRANSITION_SECS + 1)
    t = time.localtime()
    parts = ["<background>\n  <starttime>"
             f"<year>{t.tm_year}</year><month>{t.tm_mon}</month><day>{t.tm_mday}</day>"
             f"<hour>{t.tm_hour}</hour><minute>{t.tm_min}</minute><second>{t.tm_sec}</second>"
             "</starttime>\n"]
    for img, nxt in zip(images, images[1:] + images[:1]):
        a, b = escape(str(img.resolve())), escape(str(nxt.resolve()))
        parts.append(f"  <static><duration>{secs - TRANSITION_SECS}</duration><file>{a}</file></static>\n")
        if len(images) > 1:
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


def resolve_wallpaper(rule):
    """The file to hand to GNOME: an image, or a generated slideshow XML."""
    if is_slideshow(rule):
        images = folder_images(Path(rule["wallpaper"]).expanduser())
        return write_slideshow(images, rule["slideshow"], rule.get("shuffle", True))
    if rule.get("slideshow"):
        log(f"slideshow needs a folder, {rule['wallpaper']} isn't one; using it as a single image")
    return resolve_image(rule["wallpaper"])


def set_wallpaper(image, options="zoom"):
    uri = image.resolve().as_uri()
    schema = "org.gnome.desktop.background"
    for key, val in (("picture-uri", uri), ("picture-uri-dark", uri), ("picture-options", options)):
        subprocess.run(["gsettings", "set", schema, key, val], check=True)


def load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


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


def apply(cfg, loc=None, force=False, dry_run=False):
    """Detect location (unless given), pick a rule and set the wallpaper.

    Returns (rule_name, image_path, changed); rule_name is None if nothing matched.
    """
    if loc is None:
        loc = detect_location(cfg)
    rule = pick_rule(cfg, loc)
    if rule is None:
        return None, None, False
    name = rule.get("name", rule.get("wallpaper"))
    state = load_state()
    if state.get("rule") == name and not force:
        return name, state.get("image"), False
    if dry_run:
        if is_slideshow(rule):
            path = Path(rule["wallpaper"]).expanduser()
            return name, f"slideshow of {len(folder_images(path))} images in {path}", True
        return name, resolve_image(rule["wallpaper"]), True
    image = resolve_wallpaper(rule)
    try:
        set_wallpaper(image, rule.get("options") or cfg.get("options", "zoom"))
    except Exception:
        remove_slideshows(keep=Path(state["image"]) if state.get("image") else None)
        raise
    remove_slideshows(keep=image)
    save_state({"rule": name, "image": str(image)})
    return name, image, True


# --- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="run", choices=["run", "where"],
                    help="run: apply wallpaper (default); where: just print detected location")
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

    loc = detect_location(cfg)
    if args.command == "where":
        print(json.dumps(loc, indent=2))
        return

    name, image, changed = apply(cfg, loc, force=args.force, dry_run=args.dry_run)
    if name is None:
        log(f"no rule matched and no [default] set; location: {loc}")
    elif not changed:
        log(f"still at '{name}', leaving wallpaper alone")
    else:
        print(f"location matched '{name}' -> {image}")


if __name__ == "__main__":
    main()
