"""In-app updates: notice when the GitHub repo has newer commits, show what's new, and update in one click.

How it works
  * check(): `git fetch`, then count commits we're behind our upstream branch. "What's new" comes from the
    upstream CHANGELOG.md sections newer than our VERSION, else from the new commit subjects.
  * apply(): only when no job is running. `git pull --ff-only --autostash` keeps the user's own edits to
    tracked settings files (if one conflicts with the update, the user's version wins); dependencies are
    reinstalled when requirements.txt changed; then the dashboard restarts itself in place.
Nothing here runs if the folder isn't a git checkout (for example a downloaded zip).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .version import VERSION

ROOT = Path(__file__).resolve().parent.parent
CHECK_EVERY = 6 * 3600
STATE: dict = {"behind": 0, "remote_version": None, "notes": [], "commits": [], "checked_at": None, "error": "",
               "updating": False}
_lock = threading.Lock()


def _git(*args: str, timeout: int = 60) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else f"git {args[0]} failed")
    return r.stdout


def available() -> bool:
    return (ROOT / ".git").exists()


def _vkey(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:3]) or (0,)


def parse_changelog(text: str, newer_than: str) -> list[dict]:
    """[{'version', 'date', 'items': [...]}] for '## 3.2 (2026-10-21)' style sections newer than newer_than."""
    out, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^##\s+v?(\d+(?:\.\d+){0,2})\s*(?:[(\-–—·]\s*([^)]*)\)?)?\s*$", line.strip())
        if m:
            cur = {"version": m.group(1), "date": (m.group(2) or "").strip(), "items": []}
            out.append(cur)
        elif cur and re.match(r"^\s*[-*]\s+", line):
            cur["items"].append(re.sub(r"^\s*[-*]\s+", "", line).strip())
        elif cur and line.startswith("  ") and cur["items"]:
            cur["items"][-1] += " " + line.strip()
    return [s for s in out if _vkey(s["version"]) > _vkey(newer_than) and s["items"]]


def check(force: bool = False) -> dict:
    """Refresh STATE from the remote (at most every 6 hours unless forced). Never raises."""
    if not available():
        STATE.update(error="This copy isn't a git checkout, so it can't update itself. Re-download it with git clone.")
        return STATE
    last = (db.kv_get("update_checked_at") or {}).get("v")
    if not force and last and STATE["checked_at"] and time.time() - float(last) < CHECK_EVERY:
        return STATE
    with _lock:
        try:
            _git("fetch", "--quiet", timeout=60)
            behind = int(_git("rev-list", "--count", "HEAD..@{u}").strip() or 0)
            commits = [s for s in _git("log", "--format=%s", "-n", "15", "HEAD..@{u}").splitlines() if s.strip()] if behind else []
            remote_version, notes = None, []
            if behind:
                try:
                    m = re.search(r'VERSION\s*=\s*"([^"]+)"', _git("show", "@{u}:app/version.py"))
                    remote_version = m.group(1) if m else None
                except RuntimeError:
                    pass
                try:
                    notes = parse_changelog(_git("show", "@{u}:CHANGELOG.md"), VERSION)
                except RuntimeError:
                    notes = []
            STATE.update(behind=behind, remote_version=remote_version, notes=notes, commits=commits,
                         checked_at=datetime.now(timezone.utc).isoformat(), error="")
        except Exception as e:      # offline, no upstream branch, git missing
            STATE.update(checked_at=datetime.now(timezone.utc).isoformat(), error=str(e)[:200])
        db.kv_set("update_checked_at", str(time.time()))
    return STATE


def update_available() -> bool:
    return bool(STATE.get("behind")) and not STATE.get("updating")


def _busy() -> str:
    from . import progress
    snap = progress.snapshot()
    if snap.get("run"):
        return "A run is in progress. Update when it finishes."
    building = [q for q in snap.get("queued") or [] if q.get("status") != "to_screen"]   # a screening backlog can wait
    if snap.get("active") or building:
        return "Jobs are still being worked on. Update when the Background page shows nothing running."
    try:
        from . import mailsync
        if (mailsync.STATE or {}).get("active"):
            return "An email check is running. Try again in a minute."
    except Exception:
        pass
    return ""


def _resolve_conflicts_keeping_mine() -> list[str]:
    """After an autostash pop conflict, keep the user's version of each conflicted file."""
    files = [f for f in _git("diff", "--name-only", "--diff-filter=U").splitlines() if f.strip()]
    for f in files:
        _git("checkout", "--theirs", "--", f)       # in a stash pop, "theirs" is the stashed (user's) version
        _git("reset", "-q", "--", f)
    if files:
        try:
            _git("stash", "drop")
        except RuntimeError:
            pass
    return files


def apply() -> dict:
    """Pull the update. Returns {'ok', 'message', 'version', 'restart'}. Restart happens via restart_soon()."""
    if not available():
        return {"ok": False, "message": STATE.get("error") or "Not a git checkout."}
    busy = _busy()
    if busy:
        return {"ok": False, "message": busy}
    with _lock:
        STATE["updating"] = True
        try:
            before = _git("rev-parse", "HEAD").strip()
            try:
                _git("pull", "--ff-only", "--autostash", "--quiet", timeout=180)
                pull_error = None
            except RuntimeError as e:
                pull_error = e
            # git reports success even when restoring the user's stashed edits conflicts; always check
            kept = _resolve_conflicts_keeping_mine()
            if pull_error and not kept:
                raise RuntimeError(f"git pull failed: {pull_error}") from pull_error
            after = _git("rev-parse", "HEAD").strip()
            if after == before:
                STATE.update(behind=0, updating=False)
                return {"ok": True, "message": "Already up to date.", "restart": False}
            changed = _git("diff", "--name-only", before, after).splitlines()
            if "requirements.txt" in changed:
                r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
                if r.returncode != 0:
                    raise RuntimeError("The code updated, but installing new packages failed: "
                                       + (r.stderr.strip().splitlines() or ["pip error"])[-1][:200])
            m = re.search(r'VERSION\s*=\s*"([^"]+)"', (ROOT / "app" / "version.py").read_text(encoding="utf-8"))
            STATE.update(behind=0, notes=[], commits=[])
            return {"ok": True, "message": "Updated.", "version": m.group(1) if m else None, "restart": True}
        except Exception as e:
            STATE["updating"] = False
            return {"ok": False, "message": str(e)[:300]}


def restart_soon(delay: float = 1.5) -> None:
    """Replace this process with a fresh copy of the dashboard (same PID on macOS/Linux, so launchd and
    systemd keep tracking it). Called only after apply() succeeded and nothing was running."""
    def go():
        time.sleep(delay)
        sys.stdout.flush()
        sys.stderr.flush()
        os.chdir(ROOT)
        flags = ["-X", "utf8"] if sys.flags.utf8_mode else []     # keep Windows' UTF-8 mode across the restart
        os.execv(sys.executable, [sys.executable, *flags, "-m", "app.cli", "serve"])
    threading.Thread(target=go, daemon=True).start()


def loop() -> None:
    """Background thread: check shortly after start, then every 6 hours."""
    time.sleep(30)
    while True:
        check()
        time.sleep(CHECK_EVERY)
