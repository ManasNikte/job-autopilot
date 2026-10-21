"""Daily run times: config/schedule.json is the single source of truth.

Saving from the Settings page rewrites the per-user launchd agent (runs the pipeline at each time) and,
because the root wake helper watches the same file, re-books the lid-closed wake-ups 2 minutes earlier.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import CONFIG_DIR, DATA_DIR, ROOT

SCHEDULE_FILE = CONFIG_DIR / "schedule.json"
AGENT_LABEL = "com.jobautopilot.daily"
AGENT_PLIST = Path.home() / "Library/LaunchAgents" / f"{AGENT_LABEL}.plist"
WAKE_PLIST = Path("/Library/LaunchDaemons/com.jobautopilot.wake.plist")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def load_times() -> list[str]:
    try:
        times = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))["times"]
    except (OSError, ValueError, KeyError):
        times = ["08:00", "15:00"]
    return sorted({t for t in times if TIME_RE.match(t)}, key=_minutes)


def _minutes(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def save_times(times: list[str]) -> list[str]:
    clean = sorted({f"{int(t.split(':')[0]):02d}:{t.split(':')[1]}" for t in times if TIME_RE.match(t.strip())}, key=_minutes)
    if not clean:
        raise ValueError("Add at least one run time (HH:MM).")
    if len(clean) > 6:
        raise ValueError("At most 6 runs a day.")
    data = {**_raw(), "_comment": "Daily run times (24-hour, your local time). Edit on the dashboard's Settings page; "
                        "the launchd agent and the lid-closed wake-ups follow this file.", "times": clean}
    tmp = SCHEDULE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(SCHEDULE_FILE)        # one atomic write, so the wake helper sees a complete file
    install_agent(clean)
    return clean


def _raw() -> dict:
    try:
        return json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


POLL_LABEL = "com.jobautopilot.cloudpoll"
POLL_PLIST = Path.home() / "Library/LaunchAgents" / f"{POLL_LABEL}.plist"


def remote_wake() -> int:
    """Minutes between wake-ups that check for requests from the online dashboard (0 = off)."""
    try:
        return int(_raw().get("remote_wake_minutes", 0))
    except (TypeError, ValueError):
        return 0


def set_remote_wake(minutes: int) -> None:
    """Save the interval (the root wake helper watches schedule.json and books the wakes) and, on macOS,
    install a small agent that keeps the Mac awake briefly at each of those times to check for requests."""
    minutes = minutes if minutes in (0, 15, 30, 60) else 30
    data = {**_raw(), "remote_wake_minutes": minutes}
    tmp = SCHEDULE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(SCHEDULE_FILE)
    if sys.platform != "darwin":
        return                        # Windows / Linux: the dashboard's sync thread already checks every 30 s
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{POLL_LABEL}"], capture_output=True)
    if not minutes:
        POLL_PLIST.unlink(missing_ok=True)
        return
    plist = {
        "Label": POLL_LABEL,
        "ProgramArguments": [str(ROOT / ".venv/bin/python"), "-m", "app.cli", "cloud-poll"],
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": [{"Minute": m} for m in range(0, 60, minutes)],
        "StandardOutPath": str(DATA_DIR / "cloudpoll.log"), "StandardErrorPath": str(DATA_DIR / "cloudpoll.log"),
    }
    POLL_PLIST.parent.mkdir(parents=True, exist_ok=True)
    POLL_PLIST.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(POLL_PLIST)], capture_output=True)


def mode() -> str:
    """'launchd' on macOS (runs even with the dashboard closed), otherwise 'clock' (the dashboard starts the runs).
    Override with SCHEDULER=clock|launchd|off."""
    from .config import env
    m = env("SCHEDULER", "auto").lower()
    return m if m in ("clock", "launchd", "off") else ("launchd" if sys.platform == "darwin" else "clock")


def install_agent(times: list[str]) -> None:
    """(Re)write the launchd agent that runs scripts/scheduled_run.sh at each time, and reload it (macOS)."""
    if mode() != "launchd":
        return                        # Windows / Linux: the dashboard's built-in clock reads schedule.json directly
    plist = {
        "Label": AGENT_LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-i", "-s", "/bin/bash", str(ROOT / "scripts/scheduled_run.sh")],
        "StartCalendarInterval": [{"Hour": _minutes(t) // 60, "Minute": _minutes(t) % 60} for t in times],
        "StandardOutPath": str(DATA_DIR / "launchd.log"),
        "StandardErrorPath": str(DATA_DIR / "launchd.log"),
        "EnvironmentVariables": {"PATH": f"/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:{Path.home()}/.local/bin"},
    }
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    AGENT_PLIST.write_bytes(plistlib.dumps(plist))
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{AGENT_LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(AGENT_PLIST)], capture_output=True, check=False)


def next_runs(n: int = 4) -> list[datetime]:
    now, out = datetime.now(), []
    for d in range(0, 3):
        for t in load_times():
            dt = datetime.combine(date.today() + timedelta(days=d), datetime.min.time()) + timedelta(minutes=_minutes(t))
            if dt > now:
                out.append(dt)
    return out[:n]


def status() -> dict:
    """Agent installed? Wake helper installed, and new enough to follow schedule.json? Booked wakes?"""
    info = {"times": load_times(), "agent": AGENT_PLIST.exists() if mode() == "launchd" else mode() == "clock",
            "wake_installed": WAKE_PLIST.exists(), "wake_follows_settings": False, "wakes": [], "next": next_runs(),
            "mode": mode(), "mac": sys.platform == "darwin"}
    if not info["mac"]:
        info["wakes_match"] = False
        return info
    try:
        info["wake_follows_settings"] = "WatchPaths" in WAKE_PLIST.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        sched = subprocess.run(["pmset", "-g", "sched"], capture_output=True, text=True, timeout=5).stdout
        for ln in sched.splitlines():
            if "jobautopilot" in ln:
                m = re.search(r"wake at (\S+ \S+)", ln)
                if m:
                    info["wakes"].append(datetime.strptime(m.group(1), "%m/%d/%Y %H:%M:%S"))
    except Exception:
        pass
    info["wakes"].sort()
    expected = {(_minutes(t) - 2) % 1440 for t in info["times"]}
    info["wakes_match"] = bool(info["wakes"]) and all((w.hour * 60 + w.minute) in expected for w in info["wakes"][:4])
    return info
