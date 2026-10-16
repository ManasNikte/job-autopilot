"""Background worker pool so the web UI stays responsive while Claude works.

PARALLEL_JOBS threads (default 3) take tasks from one queue, so up to 3 jobs are screened or built at
the same time. The Claude client caps simultaneous calls at the same number.
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from datetime import datetime

from . import pipeline
from .config import env_int

_q: queue.Queue = queue.Queue()
log_lines: deque[str] = deque(maxlen=400)
state = {"current": None, "queued": 0, "last_finished": None, "active": {}, "paused_until": None, "pause_reason": ""}
_pending: set[tuple[str, str | None]] = set()   # tasks waiting or running, so double clicks don't double-spend
_pending_lock = threading.Lock()
_started = False


def _log(msg: str) -> None:
    log_lines.append(f"{datetime.now():%H:%M:%S} {msg}")
    print(msg, flush=True)


def _refresh_state() -> None:
    active = list(state["active"].values())
    state["current"] = ", ".join(active) if active else None
    state["queued"] = _q.qsize()


_resumed = threading.Event()


def _wait_if_paused() -> None:
    while state["paused_until"] and time.time() < state["paused_until"]:
        _resumed.wait(min(30, max(1, state["paused_until"] - time.time())))
    if state["paused_until"]:
        _log("▶ Claude usage limit has reset; resuming background work.")
        state["paused_until"], state["pause_reason"] = None, ""


def resume() -> bool:
    """Lift a usage-limit pause now (e.g. the limit was reset early). If Claude is still limited, the next
    call simply pauses again. Returns True if work was paused."""
    was = bool(state["paused_until"])
    if was:
        state["paused_until"], state["pause_reason"] = None, ""
        _log("▶ Resumed by you; continuing background work.")
        _resumed.set()
        time.sleep(0.05)
        _resumed.clear()
    return was


def _pause(e) -> None:
    from datetime import datetime as _dt
    until = max(state["paused_until"] or 0, e.retry_at)
    if state["paused_until"] != until:
        state["paused_until"], state["pause_reason"] = until, str(e)[:200]
        _log(f"⏸ {e}. Pausing all background work until {_dt.fromtimestamp(until):%I:%M %p}; it resumes on its own.")


def _loop() -> None:
    name = threading.current_thread().name
    while True:
        _wait_if_paused()
        kind, arg = _q.get()
        _wait_if_paused()
        retry = None
        state["active"][name] = f"{kind} {arg or ''}".strip()
        _refresh_state()
        try:
            if kind == "run":
                pipeline.run(_log, fetch=True, enqueue=submit, source=arg or "manual")
            elif kind == "process":
                pipeline.run(_log, fetch=False, enqueue=submit, source=arg or "manual")
            elif kind == "screen":
                pipeline.screen_job(arg, _log)
                from . import db
                j = db.get_job(arg)
                if j and j["status"] == "to_tailor":
                    submit("tailor", arg)
            elif kind == "tailor":
                pipeline.tailor_job(arg, _log)
            elif kind == "refit":
                pipeline.refit_job(arg, _log)
            elif kind == "referrals":
                from . import contacts
                contacts.queue_referral_asks(arg, _log)
        except Exception as e:  # keep the worker alive no matter what
            from . import db
            if getattr(e, "retry_at", None):          # usage limit: hold everything, retry this task after the reset
                _pause(e)
                retry = ("process", arg) if kind == "run" else (kind, arg)   # don't re-fetch; just finish the work
            else:
                _log(f"✗ task {kind} failed: {e}")
                if e.__class__.__name__ == "BackendUnavailable":
                    db.add_intervention(None, "error", "Claude backend needs setup", str(e))
        finally:
            with _pending_lock:
                _pending.discard((kind, arg))
            state["active"].pop(name, None)
            state["last_finished"] = datetime.now().strftime("%H:%M:%S")
            _refresh_state()
        if retry:
            submit(*retry)


def _scheduler(minutes: int) -> None:
    while True:
        time.sleep(minutes * 60)
        if not state["active"] and _q.empty():
            submit("run")


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    for i in range(max(1, env_int("PARALLEL_JOBS", 3))):
        threading.Thread(target=_loop, daemon=True, name=f"worker-{i + 1}").start()
    every = env_int("AUTORUN_EVERY_MINUTES", 0)
    if every > 0:
        threading.Thread(target=_scheduler, args=(every,), daemon=True, name="autopilot-scheduler").start()


def submit(kind: str, arg: str | None = None) -> bool:
    with _pending_lock:
        busy_run = kind in ("run", "process") and (
            any(k == kind for k, _ in _pending) or any(v.split(" ")[0] == kind for v in state["active"].values()))
        if (kind, arg) in _pending or busy_run:
            _log(f"already queued: {kind} {arg or ''}")
            return False
        _pending.add((kind, arg))
    _q.put((kind, arg))
    _refresh_state()
    _log(f"queued: {kind} {arg or ''}")
    return True


def resume_queue() -> int:
    """On startup, put every job that is still waiting for documents back in the pool."""
    from . import db
    n = 0
    for j in db.fetchall("SELECT id FROM jobs WHERE status='to_tailor' ORDER BY match_score DESC, posted_at DESC"):
        n += submit("tailor", j["id"])
    return n
