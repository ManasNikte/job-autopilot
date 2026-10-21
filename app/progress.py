"""Per-job progress for background work, shown live on the dashboard and the Needs-you page.

Claude doesn't report partial progress, so long steps advance by elapsed time against the median of
recent real durations for that step (stored in `timings`). Bars never pass the end of their step until
the step actually finishes.
"""
from __future__ import annotations

import statistics
import time
from datetime import datetime, timezone

from . import db

DEFAULT_SECS = {"screen": 25, "generate": 420, "render": 25, "revise": 90}


def reset() -> None:
    """Called when the web app starts: progress rows left over are from work that was interrupted."""
    db.execute("DELETE FROM progress")
    db.execute("DELETE FROM kv WHERE k='run_progress'")


# ----------------------------------------------------------------------------- whole-run phases (fetch, feeds, screening)

def set_run(phase: str, label: str, done: int = 0, total: int = 0, source: str = "", **extra) -> None:
    import json
    cur = run_state() or {}
    db.kv_set("run_progress", json.dumps({
        "phase": phase, "label": label, "done": done, "total": total,
        "source": source or cur.get("source", "manual"), "started": cur.get("started") or time.time(),
        "phase_started": time.time() if cur.get("phase") != phase else cur.get("phase_started", time.time()),
        **{k: v for k, v in cur.items() if k.startswith("n_")}, **extra,
    }))


def bump_run(n: int = 1) -> None:
    cur = run_state()
    if cur:
        cur["done"] = cur.get("done", 0) + n
        import json
        db.kv_set("run_progress", json.dumps(cur))


def run_state() -> dict | None:
    import json
    row = db.kv_get("run_progress")
    return json.loads(row["v"]) if row and row["v"] else None


def clear_run() -> None:
    db.execute("DELETE FROM kv WHERE k='run_progress'")


def expected(step: str) -> float:
    rows = db.fetchall("SELECT secs FROM timings WHERE step=? ORDER BY id DESC LIMIT 10", (step,))
    return statistics.median([r["secs"] for r in rows]) if rows else DEFAULT_SECS.get(step, 60)


def record(step: str, secs: float) -> None:
    db.execute("INSERT INTO timings (step, secs, at) VALUES (?,?,?)", (step, secs, db.now()))


def set_stage(job_id: str, kind: str, stage: str, base: float, span: float = 0, step: str = "") -> None:
    """base/span in percent; when span > 0 the bar moves from base toward base+span over `step`'s expected time."""
    now = time.time()
    row = db.fetchone("SELECT started FROM progress WHERE job_id=?", (job_id,))
    db.execute(
        "INSERT OR REPLACE INTO progress (job_id, kind, stage, base, span, step, stage_started, started) VALUES (?,?,?,?,?,?,?,?)",
        (job_id, kind, stage, base, span, step, now, row["started"] if row else now),
    )


def clear(job_id: str) -> None:
    db.execute("DELETE FROM progress WHERE job_id=?", (job_id,))


class Timer:
    def __init__(self, step: str):
        self.step, self.t0 = step, time.time()

    def stop(self) -> float:
        secs = time.time() - self.t0
        record(self.step, secs)
        return secs


def snapshot() -> dict:
    """Active jobs with computed percent, plus the waiting queue and a rough ETA."""
    now = time.time()
    active = []
    for r in db.fetchall("SELECT p.*, j.company, j.title FROM progress p JOIN jobs j ON j.id=p.job_id ORDER BY p.started"):
        pct = r["base"]
        if r["span"]:
            exp = expected(r["step"]) or 60
            pct += r["span"] * min((now - r["stage_started"]) / exp, 0.97)
        active.append({
            "job_id": r["job_id"], "company": r["company"], "title": r["title"], "kind": r["kind"],
            "stage": r["stage"], "pct": int(min(pct, 99)), "elapsed": int(now - r["started"]),
        })
    busy = {a["job_id"] for a in active}
    queued = [j for j in db.fetchall(
        "SELECT id, company, title, status FROM jobs WHERE status IN ('to_tailor','to_screen') ORDER BY status DESC, match_score DESC, posted_at DESC")
        if j["id"] not in busy]
    per_doc = expected("generate") + expected("render") + 0.5 * expected("revise")
    from .config import env_int
    lanes = max(1, env_int("PARALLEL_JOBS", 3))
    n_docs = sum(1 for q in queued if q["status"] == "to_tailor") + sum(1 for a in active if a["kind"] == "tailor")
    n_screens = sum(1 for q in queued if q["status"] == "to_screen")
    eta = int((n_docs * per_doc + n_screens * expected("screen")) / lanes)
    last = db.fetchone("SELECT id, started_at, finished_at, status, source, stats_json FROM runs WHERE status != 'running' ORDER BY id DESC LIMIT 1")
    if last:
        import json as _json
        try:
            st = _json.loads(last.pop("stats_json") or "{}")
        except ValueError:
            st = {}
        f = st.get("fetch") or {}
        last.update(error=st.get("error"), fetched=f.get("fetched"), new=(f.get("new") or 0) + ((f.get("feeds") or {}).get("new") or 0),
                    screened=st.get("screened"), docs=st.get("queued_for_documents", st.get("tailored")))
    run = run_state()
    if run:
        run["elapsed"] = int(now - run.get("started", now))
        run["pct"] = int(100 * run["done"] / run["total"]) if run.get("total") else None
    return {"active": active, "queued": queued[:50], "queued_total": len(queued), "eta_secs": eta, "run": run, "last_run": last,
            "avg_doc_secs": int(per_doc), "lanes": lanes, "screen_cap": env_int("MAX_SCREEN_PER_RUN", 60),
            "usage": _usage_view(), "usage_pause_at": env_int("USAGE_PAUSE_AT", 95),
            "auto_continue": bool(env_int("AUTO_CONTINUE_SCREENING", 1)),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _usage_view() -> dict | None:
    """The last plan-usage reading for the panel; refreshed in the background when it's over 10 minutes old."""
    import threading
    import time
    from . import llm
    u = llm.USAGE
    if time.time() - u.get("checked", 0) > 600 and not getattr(_usage_view, "busy", False):
        def refresh():
            _usage_view.busy = True
            try:
                llm.usage_now(force=True)
            finally:
                _usage_view.busy = False
        threading.Thread(target=refresh, daemon=True, name="usage-check").start()
    w = u.get("worst")
    if not w:
        return None
    return {"kind": w.get("kind"), "percent": w.get("percent"), "resets_at": w.get("resets_at"),
            "checked_ago": int(time.time() - u.get("checked", 0))}
