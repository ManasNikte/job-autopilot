#!/bin/bash
# Runs Job Autopilot every day at 8:00 AM and 3:00 PM local (Eastern) time via a launchd agent.
# Remove with: scripts/install_schedule.sh --remove
# To also run while the lid is closed, wake the Mac first: sudo scripts/install_wake.sh
set -e
LABEL="com.jobautopilot.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
case "$ROOT" in "$HOME/Downloads"*|"$HOME/Documents"*|"$HOME/Desktop"*)
  echo "macOS blocks background jobs in $ROOT (privacy-protected folder). Move the project to your home folder first."; exit 1;;
esac
DASH="com.jobautopilot.dashboard"
DPLIST="$HOME/Library/LaunchAgents/$DASH.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/$DASH" 2>/dev/null || true
if [ "$1" = "--remove" ]; then rm -f "$PLIST" "$DPLIST"; echo "Schedule and dashboard service removed."; exit 0; fi
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data"
# Daily run agent: times come from config/schedule.json (editable on the Settings page).
"$ROOT/.venv/bin/python" -c "import sys; sys.path.insert(0, '$ROOT'); from app import schedule; schedule.install_agent(schedule.load_times())"

# The dashboard as an always-on background service: starts at login, restarts if it stops, logs to a file.
cat > "$DPLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$DASH</string>
  <key>ProgramArguments</key>
  <array><string>$ROOT/.venv/bin/python</string><string>-m</string><string>app.cli</string><string>serve</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$ROOT/data/server.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/server.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$HOME/.local/bin</string><key>PYTHONUNBUFFERED</key><string>1</string></dict>
</dict>
</plist>
PLIST
launchctl bootstrap "gui/$(id -u)" "$DPLIST"
echo "Installed: Job Autopilot runs daily at the times in config/schedule.json ($(date +%Z)). Log: $ROOT/data/scheduled.log"
echo "Dashboard runs in the background at http://127.0.0.1:5055 (log: $ROOT/data/server.log)"
