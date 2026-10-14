#!/bin/bash
# Daily Job Autopilot run, started by launchd at 8:00 AM and 3:00 PM Eastern (scripts/install_schedule.sh).
# fetch new postings -> Claude screens them -> tailored resume + cover letter PDFs for the good ones.
#
# launchd starts this under `caffeinate`, so the Mac stays awake until the work is done, including after
# a scheduled wake with the lid closed (scripts/install_wake.sh). It first waits for the internet.
cd "$(dirname "$0")/.." || exit 1
LOG="data/scheduled.log"
mkdir -p data
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }
log "=== scheduled run starting"

# 1. Wait up to 5 minutes for a working internet connection (Wi-Fi reconnects a little after waking).
for i in $(seq 1 60); do
  if curl -s -o /dev/null -m 5 https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=false; then online=1; break; fi
  sleep 5
done
if [ -z "$online" ]; then log "no internet after 5 minutes; skipping this run"; exit 0; fi
log "online"

# 2. Run inside the open dashboard if there is one (live progress), else headless.
if curl -s -o /dev/null -m 5 http://127.0.0.1:5055/activity; then
  curl -s -o /dev/null -m 10 -X POST -d kind=run -d source=schedule http://127.0.0.1:5055/run
  log "queued in the running dashboard; staying awake until its queue is empty"
  for i in $(seq 1 720); do          # up to 6 hours
    sleep 30
    busy=$(curl -s -m 5 http://127.0.0.1:5055/progress | /usr/bin/python3 -c \
      'import json,sys; d=json.load(sys.stdin); print(1 if d["active"] or d["queued_total"] or d.get("run") else 0)' 2>/dev/null)
    [ "$busy" = "0" ] && break
  done
  log "dashboard queue finished"
else
  ./.venv/bin/python -m app.cli run >> "$LOG" 2>&1
  log "headless run finished (exit $?)"
fi
