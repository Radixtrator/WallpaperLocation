#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

share=~/.local/share/wallpaper-location
desktop=io.github.radixtrator.WallpaperLocation.desktop

install -Dm755 wallpaper_location.py "$share/wallpaper_location.py"
install -Dm755 wallpaper_location_gui.py "$share/wallpaper_location_gui.py"
mkdir -p ~/.local/bin
ln -sf "$share/wallpaper_location.py" ~/.local/bin/wallpaper-location
ln -sf "$share/wallpaper_location_gui.py" ~/.local/bin/wallpaper-location-gui

# App launcher entry; GeoClue also authorizes location access by this file's name.
# Absolute Exec path because ~/.local/bin isn't always on PATH for the GNOME session.
sed "s|^Exec=wallpaper-location-gui|Exec=$HOME/.local/bin/wallpaper-location-gui|" "$desktop" \
    | install -Dm644 /dev/stdin ~/.local/share/applications/$desktop

install -Dm644 systemd/wallpaper-location.service ~/.config/systemd/user/wallpaper-location.service
install -Dm644 systemd/wallpaper-location.timer ~/.config/systemd/user/wallpaper-location.timer

cfg=~/.config/wallpaper-location/config.toml
if [[ ! -f $cfg ]]; then
    # Start with no places and no default, so the wallpaper is untouched until
    # you add some (see config.example.toml for the format).
    mkdir -p "$(dirname "$cfg")"
    printf 'options = "zoom"\n\n[detection]\ngeoclue = true\nip_lookup = true\n' > "$cfg"
    echo "Created $cfg - add places in the app."
fi

systemctl --user daemon-reload
systemctl --user enable --now wallpaper-location.timer
update-desktop-database ~/.local/share/applications 2>/dev/null || true
echo "Installed. Open \"Wallpaper Location\" from the app grid, or run: wallpaper-location-gui"
