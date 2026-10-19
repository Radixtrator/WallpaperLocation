# Wallpaper Location

Change your GNOME wallpaper automatically based on where you are: one wallpaper at home, another at the office, something else when you're travelling.

A small GTK4 / libadwaita app plus a background timer. It's written in Python, uses only the standard library and PyGObject, and was built and tested on CachyOS with GNOME. It should work on any recent GNOME desktop.

<p align="center">
  <img src="screenshots/screenshot-main.png" width="420" alt="Main window: current location, a list of places with wallpaper previews, and options">
  <img src="screenshots/screenshot-edit.png" width="420" alt="Editing a place: Wi-Fi, city, country and distance-based conditions">
</p>

## Features

- **Match places by** Wi-Fi network name, city, region, country, or distance from a point on the map. Combine them however you like.
- **An image or a folder per place.** With a folder, a random image is picked from it, including its subfolders.
- **First match wins.** Reorder places to set priority, and set a fallback wallpaper for everywhere else, or leave it unset to keep your current wallpaper.
- **Runs in the background.** A systemd user timer checks every 10 minutes and at login. It only changes the wallpaper when you've moved to a different place.
- **One-click helpers** fill in your current Wi-Fi, city or coordinates while you edit a place.
- **CLI too.** `wallpaper-location where` / `run` work without the GUI.

## How location is detected

Every check uses all of these sources and combines what each one finds:

| Source | Gives | Notes |
| --- | --- | --- |
| **Wi-Fi SSID** (`nmcli`) | Network name | Most reliable way to tell home from work. Fully offline. |
| **GNOME Location Services** (GeoClue) | Coordinates | Needs *Settings → Privacy → Location* turned on. |
| **IP lookup** ([ipinfo.io](https://ipinfo.io)) | City, region, country, rough coordinates | Sends your public IP to ipinfo.io. Turn it off in the app or config to stay offline. |

## Requirements

- GNOME 47 or newer (GTK 4, libadwaita ≥ 1.6)
- Python 3.11+ with PyGObject
- NetworkManager (for Wi-Fi matching), GeoClue (optional)

On Arch / CachyOS, all of these are usually already installed with GNOME:

```bash
sudo pacman -S --needed python-gobject gtk4 libadwaita geoclue networkmanager
```

## Install

```bash
git clone https://github.com/Radixtrator/WallpaperLocation.git
cd WallpaperLocation
./install.sh
```

This installs everything for your user only (no root):

- the app into `~/.local/share/wallpaper-location/`, with `wallpaper-location` and `wallpaper-location-gui` linked into `~/.local/bin/`
- a launcher entry, so **Wallpaper Location** shows up in the app grid
- the systemd user timer `wallpaper-location.timer`, enabled and started
- an empty config at `~/.config/wallpaper-location/config.toml`, so your wallpaper isn't touched until you add places

To remove everything except your config:

```bash
./uninstall.sh
```

## Usage

Open **Wallpaper Location**, click **+** to add a place, choose what it matches on and pick a wallpaper. Changes are saved straight away. Click **Apply Now** to switch immediately; otherwise the background timer picks the change up within 10 minutes.

From a terminal:

```bash
wallpaper-location where          # print what was detected
wallpaper-location run            # apply if the matched place changed
wallpaper-location run --force    # apply even if it didn't (re-rolls folder picks)
wallpaper-location run --dry-run  # show what would happen
```

## Configuration

The GUI edits `~/.config/wallpaper-location/config.toml`, but it's plain TOML if you prefer editing by hand. See [`config.example.toml`](config.example.toml).

```toml
options = "zoom"          # zoom, scaled, centered, stretched, spanned, wallpaper

[detection]
geoclue = true
ip_lookup = true

[[rule]]                  # checked top to bottom; first match wins
name = "home"
ssid = "MyHomeWifi"       # or a list: ["Wifi-A", "Wifi-B"]
wallpaper = "~/Pictures/Wallpapers/home"   # file or folder

[[rule]]
name = "office"
lat = 45.5231
lon = -122.6765
radius_km = 1.5
wallpaper = "~/Pictures/Wallpapers/office.jpg"
options = "scaled"        # per-place override

[[rule]]
name = "travelling"
country = ["CA", "MX", "JP"]
wallpaper = "~/Pictures/Wallpapers/travel"

[default]                 # optional fallback
wallpaper = "/usr/share/backgrounds/gnome"
```

If a place has several conditions, **all** of them must match. Text matches ignore upper/lower case.

The wallpaper is set for both light and dark style (`picture-uri` and `picture-uri-dark`).

## Troubleshooting

- **"Geolocation disabled" in the log:** turn on *Settings → Privacy → Location*, or rely on Wi-Fi and IP lookup.
- **Check the background job:**
  ```bash
  journalctl --user -u wallpaper-location.service -n 20
  ```
- **"Change automatically" is greyed out:** the timer isn't installed. Run `./install.sh`.

## License

[MIT](LICENSE)
