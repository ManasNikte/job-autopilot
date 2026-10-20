#!/bin/bash
# Wakes the Mac (even with the lid closed or locked) 2 minutes before each daily run time.
# Needs your admin password once:   sudo ~/job-autopilot/scripts/install_wake.sh
# Remove with:                       sudo ~/job-autopilot/scripts/install_wake.sh --remove
#
# Run times come from config/schedule.json (edit them on the dashboard's Settings page). A small root
# helper books one-off `pmset` wake events for the next 7 days and re-books automatically whenever that
# file changes, every night, and at startup. It changes nothing except Job Autopilot's own wake events.
set -e
[ "$(id -u)" = "0" ] || { echo "Run with sudo: sudo $0 $*"; exit 1; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCHEDULE="$ROOT/config/schedule.json"
LABEL="com.jobautopilot.wake"
PLIST="/Library/LaunchDaemons/$LABEL.plist"
HELPER="/usr/local/libexec/jobautopilot-wake.sh"

cancel_ours() {
  pmset -g sched | grep jobautopilot | sed -nE 's/.*wake at ([0-9/]+) ([0-9:]+).*/\1 \2/p' | while read -r d t; do
    yy=$(date -j -f "%m/%d/%Y" "$d" +%m/%d/%y)
    pmset schedule cancel wake "$yy $t" jobautopilot >/dev/null 2>&1 || true
  done
}

launchctl bootout "system/$LABEL" 2>/dev/null || true
if [ "$1" = "--remove" ]; then cancel_ours; rm -f "$PLIST" "$HELPER"; echo "Wake schedule removed."; exit 0; fi

mkdir -p /usr/local/libexec
cat > "$HELPER" <<HELPER
#!/bin/bash
# Re-book Job Autopilot wake events: 2 minutes before each time in $SCHEDULE, for the next 7 days.
SCHEDULE="$SCHEDULE"
wanted=()
i=0
while t=\$(/usr/bin/plutil -extract times.\$i raw -o - "\$SCHEDULE" 2>/dev/null); do
  h=\${t%%:*}; m=\${t##*:}
  mins=\$(( (10#\$h * 60 + 10#\$m - 2 + 1440) % 1440 ))
  wanted+=("\$(printf '%02d:%02d:00' \$((mins / 60)) \$((mins % 60)))")
  i=\$((i + 1))
done
[ \${#wanted[@]} -eq 0 ] && wanted=("07:58:00" "14:58:00")
# cancel our previously booked wakes, then book the current times
pmset -g sched | grep jobautopilot | sed -nE 's/.*wake at ([0-9/]+) ([0-9:]+).*/\1 \2/p' | while read -r d t; do
  yy=\$(date -j -f "%m/%d/%Y" "\$d" +%m/%d/%y)
  pmset schedule cancel wake "\$yy \$t" jobautopilot >/dev/null 2>&1 || true
done
now=\$(date +%s)
# Optional: wake every N minutes for the next 24 hours so requests from the online dashboard (your phone)
# are picked up while the lid is closed. Set on the Settings page (remote_wake_minutes in schedule.json).
every=\$(/usr/bin/plutil -extract remote_wake_minutes raw -o - "\$SCHEDULE" 2>/dev/null || echo 0)
if [ "\${every:-0}" -gt 0 ] 2>/dev/null; then
  t=\$(( (now / (every * 60) + 1) * every * 60 ))
  end=\$(( now + 86400 ))
  while [ "\$t" -le "\$end" ]; do
    pmset schedule wake "\$(date -r \$t +'%m/%d/%y %H:%M:%S')" jobautopilot >/dev/null 2>&1 || true
    t=\$(( t + every * 60 ))
  done
fi
for d in 0 1 2 3 4 5 6; do
  day=\$(date -v+\${d}d +%m/%d/%y)
  for t in "\${wanted[@]}"; do
    when=\$(date -j -f "%m/%d/%y %H:%M:%S" "\$day \$t" +%s)
    [ "\$when" -le "\$now" ] && continue
    pmset schedule wake "\$day \$t" jobautopilot >/dev/null 2>&1 || true
  done
done
HELPER
chmod 755 "$HELPER"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$HELPER</string></array>
  <key>RunAtLoad</key><true/>
  <key>WatchPaths</key><array><string>$SCHEDULE</string></array>
  <key>StartCalendarInterval</key><array>
    <dict><key>Hour</key><integer>0</integer><key>Minute</key><integer>10</integer></dict>
    <dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>10</integer></dict>
    <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>10</integer></dict>
    <dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>10</integer></dict>
  </array>
</dict>
</plist>
PLIST
chown root:wheel "$PLIST"; chmod 644 "$PLIST"
launchctl bootstrap system "$PLIST"
sleep 2
echo "Installed. Wake-ups follow config/schedule.json (Settings page). Upcoming:"
pmset -g sched | grep -i jobautopilot | head -4
