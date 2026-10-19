#!/usr/bin/env bash
set -euo pipefail
systemctl --user disable --now wallpaper-location.timer 2>/dev/null || true
rm -rf ~/.local/share/wallpaper-location
rm -f ~/.local/bin/wallpaper-location ~/.local/bin/wallpaper-location-gui \
      ~/.local/share/applications/io.github.radixtrator.WallpaperLocation.desktop \
      ~/.config/systemd/user/wallpaper-location.{service,timer}
systemctl --user daemon-reload
echo "Removed. Config left in ~/.config/wallpaper-location/"
