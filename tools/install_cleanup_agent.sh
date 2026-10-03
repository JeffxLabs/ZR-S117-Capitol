#!/bin/sh
# Installs a per-user LaunchAgent that deletes local full-size capture screenshots
# (~/Library/Caches/s117-zroute-captures) once they are older than KEEP_DAYS (default 3).
# The compressed copies committed under events/<id>/screenshots/ are not touched.
# Usage: sh tools/install_cleanup_agent.sh [KEEP_DAYS]      Uninstall: sh tools/install_cleanup_agent.sh --uninstall
set -e
LABEL="com.jeffxlabs.s117-capture-cleanup"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$HOME/Library/Caches/s117-zroute-captures"

if [ "$1" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL"
  exit 0
fi

KEEP_DAYS="${1:-3}"
MINUTES=$((KEEP_DAYS * 1440))
mkdir -p "$DIR" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string><string>-c</string>
    <string>/usr/bin/find "$DIR" -type f -mmin +$MINUTES -delete; /usr/bin/find "$DIR" -mindepth 1 -type d -empty -delete</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>15</integer></dict>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
PLISTEOF
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed $LABEL: deletes files in $DIR older than $KEEP_DAYS days (daily 04:15 and at login)."
