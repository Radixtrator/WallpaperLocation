#!/usr/bin/env bash
set -euo pipefail
systemctl --user disable --now wallpaper-location.timer wallpaper-location-watch.service 2>/dev/null || true
gapplication action io.github.radixtrator.WallpaperLocation.Tray quit 2>/dev/null || true
rm -rf ~/.local/share/wallpaper-location
rm -f ~/.local/bin/wallpaper-location ~/.local/bin/wallpaper-location-gui ~/.local/bin/wallpaper-location-tray \
      ~/.local/share/applications/io.github.radixtrator.WallpaperLocation{,.Tray}.desktop \
      ~/.config/autostart/io.github.radixtrator.WallpaperLocation.Tray.desktop \
      ~/.config/systemd/user/wallpaper-location.{service,timer} \
      ~/.config/systemd/user/wallpaper-location-watch.service
systemctl --user daemon-reload
echo "Removed. Config left in ~/.config/wallpaper-location/"
