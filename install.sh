#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

share=~/.local/share/wallpaper-location
desktop=io.github.radixtrator.WallpaperLocation.desktop

install -Dm755 wallpaper_location.py "$share/wallpaper_location.py"
install -Dm755 wallpaper_location_gui.py "$share/wallpaper_location_gui.py"
install -Dm755 wallpaper_location_tray.py "$share/wallpaper_location_tray.py"
mkdir -p ~/.local/bin
ln -sf "$share/wallpaper_location.py" ~/.local/bin/wallpaper-location
ln -sf "$share/wallpaper_location_gui.py" ~/.local/bin/wallpaper-location-gui
ln -sf "$share/wallpaper_location_tray.py" ~/.local/bin/wallpaper-location-tray

# App launcher entry; GeoClue also authorizes location access by this file's name.
# Absolute Exec path because ~/.local/bin isn't always on PATH for the GNOME session.
sed "s|^Exec=wallpaper-location-gui|Exec=$HOME/.local/bin/wallpaper-location-gui|" "$desktop" \
    | install -Dm644 /dev/stdin ~/.local/share/applications/$desktop

# Tray icon, started at login. Turning it off in the app leaves a Hidden=true
# copy in ~/.config/autostart, so reinstalling doesn't switch it back on.
tray=io.github.radixtrator.WallpaperLocation.Tray.desktop
sed "s|^Exec=wallpaper-location-tray|Exec=$HOME/.local/bin/wallpaper-location-tray|" "$tray" \
    | install -Dm644 /dev/stdin ~/.local/share/applications/$tray
if [[ ! -f ~/.config/autostart/$tray ]]; then
    install -Dm644 ~/.local/share/applications/$tray ~/.config/autostart/$tray
fi

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
if ! grep -q '^Hidden=true' ~/.config/autostart/$tray && [[ -n ${DBUS_SESSION_BUS_ADDRESS:-} ]]; then
    # Restart a running tray so it picks up the new code.
    gapplication action io.github.radixtrator.WallpaperLocation.Tray quit 2>/dev/null || true
    setsid ~/.local/bin/wallpaper-location-tray >/dev/null 2>&1 < /dev/null &
fi
if ! gnome-extensions list --enabled 2>/dev/null | grep -q appindicatorsupport; then
    echo "Note: the tray icon needs the GNOME extension \"AppIndicator and KStatusNotifierItem Support\"."
fi
echo "Installed. Open \"Wallpaper Location\" from the app grid, or run: wallpaper-location-gui"
