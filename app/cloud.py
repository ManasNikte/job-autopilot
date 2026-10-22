"""Online dashboard sync: lets you watch progress and start runs from your phone.

How it stays safe
  * Outbound only. This Mac connects OUT to the online dashboard (Supabase); nothing on the internet can
    connect in. No ports are opened.
  * Your own account. You sign in once on the Settings page; the password is used for that one request and
    never stored. Only the refresh token is kept, in the OS keychain.
  * Summaries only. Counts, today's greeting, background status, job metadata (company, title, status,
    match), and email-update summaries. Resumes, cover letters, the master prompt, and email text stay here.
  * A fixed menu of requests. The website can only queue run / process / mail / resume, and every request is
    checked here again before anything happens.

The shared site's address and public "anon" key live in config/cloud.json. The anon key is public by
design: every table is protected by row-level security (supabase/schema.sql).
"""
from __future__ import annotations

import json
import platform
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from . import db
from .config import CONFIG_DIR, DATA_DIR

from .version import VERSION as APP_VERSION
ALLOWED = {"run", "process", "mail", "resume"}
STATE_FILE = DATA_DIR / "cloud.json"
KEY = "cloud:refresh"
STATUS: dict = {"connected": False, "last_sync": None, "error": "", "queued_from_phone": 0, "operator": None}
_lock = threading.Lock()
_token: dict = {"access": "", "exp": 0.0}


# ----------------------------------------------------------------------------- config & local state

def config() -> dict:
    try:
        c = json.loads((CONFIG_DIR / "cloud.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return c if c.get("supabase_url") and c.get("anon_key") else {}


def state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def connected() -> bool:
    return bool(config() and state().get("machine_id") and _secret())


def _secret() -> str | None:
    from .mailsync import get_secret
    return get_secret(KEY)


def _headers(access: str | None = None) -> dict:
    c = config()
    return {"apikey": c["anon_key"], "Authorization": f"Bearer {access or c['anon_key']}", "Content-Type": "application/json"}


# ----------------------------------------------------------------------------- auth

def _auth(grant: str, body: dict) -> dict:
    c = config()
    r = requests.post(f"{c['supabase_url']}/auth/v1/token?grant_type={grant}", json=body, headers=_headers(), timeout=30)
    if r.status_code >= 400:
        try:
            msg = r.json().get("error_description") or r.json().get("msg") or r.text
        except ValueError:
            msg = r.text
        raise RuntimeError(msg[:200])
    return r.json()


def _store_session(res: dict) -> None:
    from .mailsync import set_secret
    set_secret(KEY, res["refresh_token"])
    _token.update(access=res["access_token"], exp=time.time() + int(res.get("expires_in", 3600)) - 60)


def access_token() -> str:
    with _lock:
        if _token["access"] and time.time() < _token["exp"]:
            return _token["access"]
        rt = _secret()
        if not rt:
            raise RuntimeError("not connected")
        _store_session(_auth("refresh_token", {"refresh_token": rt}))
        return _token["access"]


def default_city() -> str:
    """The city from your form presets (profile/applicant.json "location"), unless it's still the placeholder."""
    from .config import APPLICANT_FILE
    try:
        loc = (json.loads(APPLICANT_FILE.read_text(encoding="utf-8")).get("location") or "").strip()
    except (OSError, ValueError):
        return ""
    return "" if loc.lower() in ("", "city, st") else loc[:80]


def city() -> str:
    """The city shown to the site operator: what you saved in Settings, else your form-preset location."""
    st = state()
    return (st["city"] if "city" in st else default_city())[:80]


def set_city(value: str) -> str:
    st = state()
    st["city"] = " ".join((value or "").split())[:80]
    _save_state(st)
    if connected():
        try:
            _rest("PATCH", "machines", params={"id": f"eq.{st['machine_id']}"}, json={"city": st["city"]})
        except Exception:
            pass                                     # the next sync sends it
    return st["city"]


PENDING_FILE = DATA_DIR / "cloud_pending.json"    # a sign-up waiting for its emailed code (never the password)


def strong_password(pw: str) -> bool:
    """Same rule as the website and the Supabase project: 10+ characters, lowercase, uppercase, and a number."""
    return len(pw) >= 10 and any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)


def pending_signup() -> dict:
    try:
        return json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def signup(name: str, email: str, password: str, machine_name: str, city_name: str = "", opt_in: bool = False) -> dict:
    """Create the online-dashboard account from this computer. Supabase emails a 6-digit code; verify() finishes
    the job (confirms the account and connects this computer). The password goes straight to Supabase over HTTPS
    and is not kept anywhere."""
    if not config():
        raise RuntimeError("The online dashboard isn't configured for this copy (config/cloud.json).")
    email = email.strip().lower()
    if not name.strip():
        raise RuntimeError("Tell us your name.")
    if "@" not in email:
        raise RuntimeError("Enter a valid email.")
    if not strong_password(password):
        raise RuntimeError("Use at least 10 characters, with a lowercase letter, an uppercase letter, and a number.")
    c = config()
    r = requests.post(f"{c['supabase_url']}/auth/v1/signup", headers=_headers(), timeout=30,
                      json={"email": email, "password": password,
                            "data": {"display_name": name.strip()[:60], "marketing_opt_in": bool(opt_in)}})
    if r.status_code >= 400:
        try:
            msg = r.json().get("msg") or r.json().get("error_description") or r.text
        except ValueError:
            msg = r.text
        raise RuntimeError(("Too many attempts. Wait a minute and try again." if r.status_code == 429 else msg)[:200])
    body = r.json() if r.text else {}
    user = body.get("user") or body
    if isinstance(user, dict) and user.get("identities") == []:
        # Supabase's answer when the email already has an account (it doesn't say so, to protect privacy)
        raise RuntimeError("That email may already have an account. Use \"I already have an account\" to sign in.")
    PENDING_FILE.write_text(json.dumps({"email": email, "name": name.strip()[:60], "machine_name": machine_name[:80],
                                        "city": " ".join(city_name.split())[:80], "at": db.now()}), encoding="utf-8")
    return pending_signup()


def resend_code() -> None:
    p = pending_signup()
    if not p:
        raise RuntimeError("Start again: no sign-up is waiting for a code.")
    c = config()
    r = requests.post(f"{c['supabase_url']}/auth/v1/resend", headers=_headers(), timeout=30,
                      json={"type": "signup", "email": p["email"]})
    if r.status_code >= 400:
        raise RuntimeError("Couldn't send a new code yet. Wait a minute and try again.")


def verify(code: str) -> dict:
    """Confirm the emailed code: that signs in, so register this computer right away."""
    p = pending_signup()
    if not p:
        raise RuntimeError("Start again: no sign-up is waiting for a code.")
    code = "".join(ch for ch in code if ch.isdigit())
    c = config()
    r = requests.post(f"{c['supabase_url']}/auth/v1/verify", headers=_headers(), timeout=30,
                      json={"type": "signup", "email": p["email"], "token": code})
    if r.status_code >= 400 or not (r.json() if r.text else {}).get("refresh_token"):
        raise RuntimeError("That code didn't work or has expired. Check it, or send a new one.")
    st = _attach(r.json(), p["email"], p["machine_name"], p["city"])
    try:
        PENDING_FILE.unlink()
    except OSError:
        pass
    return st


def cancel_signup() -> None:
    try:
        PENDING_FILE.unlink()
    except OSError:
        pass


def connect(email: str, password: str, machine_name: str, city_name: str | None = None) -> dict:
    """Sign in once, register this computer as a machine, and remember it."""
    if not config():
        raise RuntimeError("The online dashboard isn't configured for this copy (config/cloud.json).")
    res = _auth("password", {"email": email.strip(), "password": password})
    return _attach(res, email, machine_name, city_name)


def _attach(res: dict, email: str, machine_name: str, city_name: str | None) -> dict:
    """Keep the session's refresh token (keychain) and register this computer under that account."""
    _store_session(res)
    user_id = res["user"]["id"]
    place = " ".join((city_name if city_name is not None else default_city()).split())[:80]
    st = state()
    mid = st.get("machine_id") if st.get("user_id") == user_id else None
    if not mid:
        rows = _rest("POST", "machines", json={"user_id": user_id, "name": machine_name[:80], "city": place,
                                               "platform": platform.system().lower()[:20], "app_version": APP_VERSION},
                     prefer="return=representation")
        mid = rows[0]["id"]
        _reset_cursors()                               # a new machine row starts empty online: send everything
    _save_state({"user_id": user_id, "email": email.strip(), "machine_id": mid, "machine_name": machine_name[:80],
                 "city": place,
                 "connected_at": db.now()})
    STATUS.update(connected=True, error="")
    return state()


def disconnect() -> None:
    from .mailsync import delete_secret
    st = state()
    try:
        if st.get("machine_id"):
            _rest("DELETE", "machines", params={"id": f"eq.{st['machine_id']}"})   # cascades its rows
    except Exception:
        pass
    delete_secret(KEY)
    _token.update(access="", exp=0)
    _reset_cursors()
    try:
        STATE_FILE.unlink()
    except OSError:
        pass
    STATUS.update(connected=False, last_sync=None, error="", operator=None)


def _rest(method: str, table: str, params: dict | None = None, json=None, prefer: str | None = None):
    c = config()
    h = _headers(access_token())
    if prefer:
        h["Prefer"] = prefer
    r = requests.request(method, f"{c['supabase_url']}/rest/v1/{table}", params=params, json=json, headers=h, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"{table}: {r.status_code} {r.text[:200]}")
    if not r.text:
        return None
    if method == "GET" or "return=representation" in (prefer or ""):
        return r.json()
    return None


# ----------------------------------------------------------------------------- what gets shared

SYNC_STATUSES = ("needs_decision", "to_tailor", "tailoring", "ready", "below_threshold",
                 "applied", "interviewing", "offer", "closed")


def _charts() -> dict:
    """The dashboard's charts for the website: the last 8 Sunday-to-Saturday weeks, time saved, and the role-family
    donut (counts only)."""
    try:
        from .web import donut_slices, time_saved, week_series
        today = datetime.now().astimezone().date()
        sunday = today - timedelta(days=(today.weekday() + 1) % 7)
        weeks = [week_series(sunday - timedelta(days=7 * i)) for i in range(8)]
        fams = db.fetchall("SELECT coalesce(role_family,'Other') AS fam, count(*) AS n FROM jobs "
                           "WHERE status IN ('applied','interviewing','offer','closed') GROUP BY fam ORDER BY n DESC, fam")
        saved = time_saved()
        return {"weeks": weeks, "families": donut_slices(fams),
                "time_saved": {"hours": saved["hours"], "days": saved["days"],
                               "rows": [{k: r[k] for k in ("label", "verb", "n", "per", "hours", "pct")} for r in saved["rows"]]}}
    except Exception:
        return {}


def _applied_this_week() -> int:
    """Applications since Sunday 00:00 local time (weeks run Sunday to Saturday)."""
    from .web import week_start_utc
    return db.count("jobs", "applied_at >= ?", (week_start_utc(),))


def _interview_counts() -> dict:
    """Interviews ever and offers ever (an interview that ended in a rejection still counts)."""
    try:
        from . import interviews
        c = interviews.counts()
        return {"interviews": c["interviews"], "offers": c["offers"]}
    except Exception:
        return {}


def _assessment_kpi() -> int:
    try:
        from . import assessments
        return sum(1 for a in assessments.pending() if not a["overdue"])
    except Exception:
        return 0


def _assessment_list() -> list[dict]:
    try:
        from . import assessments
        return [{"company": a["shown_company"], "role": a.get("shown_role") or "", "due_at": a["due_at"]}
                for a in assessments.pending() if not a["overdue"]][:10]
    except Exception:
        return []


def snapshot() -> dict:
    """Progress summary for the website: greeting, KPIs, pipeline, background work, 14-day chart."""
    from . import greeting, progress, worker
    from .mailsync import STATE as MAIL
    today = datetime.now().astimezone().date()
    start = datetime.combine(today, datetime.min.time()).astimezone().astimezone(timezone.utc).isoformat()
    counts = {r["status"]: r["n"] for r in db.fetchall("SELECT status, count(*) AS n FROM jobs GROUP BY status")}
    per_day = {}
    for r in db.fetchall("SELECT applied_at FROM jobs WHERE applied_at >= ?", ((datetime.now(timezone.utc) - timedelta(days=15)).isoformat(),)):
        d = datetime.fromisoformat(r["applied_at"]).astimezone().date()
        per_day[d] = per_day.get(d, 0) + 1
    snap = progress.snapshot()
    last = db.fetchone("SELECT started_at, finished_at, status, source FROM runs ORDER BY id DESC LIMIT 1")
    try:
        from . import schedule
        nxt = schedule.next_runs(1)
        next_run = nxt[0].astimezone().isoformat() if nxt else None
    except Exception:
        next_run = None
    return {
        "greeting": greeting.greet() if greeting.user_name() else {"title": "Welcome", "text": ""},
        "kpis": {
            "posted_today": db.count("jobs", "posted_at >= ? AND status != 'filtered_out'", (start,)),
            "ready": counts.get("ready", 0), "decide": counts.get("needs_decision", 0),
            "applied_today": db.count("jobs", "applied_at >= ?", (start,)),
            "applied_total": sum(counts.get(s, 0) for s in ("applied", "interviewing", "offer", "closed")),
            "interviewing": counts.get("interviewing", 0) + counts.get("offer", 0),
            **_interview_counts(),
            "rejected": counts.get("closed", 0),
            "assessments": _assessment_kpi(),
            "applied_week": _applied_this_week(),
        },
        "pipeline": {k: counts.get(k, 0) for k in ("to_screen", "needs_decision", "to_tailor", "tailoring", "ready",
                                                  "applied", "interviewing", "offer", "closed")},
        "assessments": _assessment_list(),
        **_charts(),
        "series": [{"day": (today - timedelta(days=i)).isoformat(), "n": per_day.get(today - timedelta(days=i), 0)}
                   for i in range(13, -1, -1)],
        "background": {
            "active": [{"company": a["company"], "title": a["title"], "kind": a["kind"], "stage": a["stage"], "pct": a["pct"]}
                       for a in snap["active"]],
            "queued": snap["queued_total"], "run": snap.get("run"),
            "mail": {k: MAIL.get(k) for k in ("active", "step", "found")} if MAIL.get("active") else None,
            "paused_until": worker.state.get("paused_until"),
        },
        "last_run": last, "next_run": next_run,
        "open_items": db.count("interventions", "status='open'"),
        "machine_time": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _changed_jobs(since: str) -> list[dict]:
    """Every job changed at or after the cursor (timestamps have 1-second resolution, so ">=" re-sends the
    boundary second instead of risking a skip; upserts make that harmless). All statuses: the ones that
    aren't shared are deleted online, so a job you skip doesn't stay "Ready" on your phone."""
    return db.fetchall(
        "SELECT id, company, title, location, status, match_score, salary, posted_at, applied_at, url, updated_at FROM jobs "
        "WHERE updated_at >= ? ORDER BY updated_at, id LIMIT 1000", (since,))


EMAIL_FIELDS = ("received_at", "company", "role", "event", "summary", "deadline", "job_id", "new_status", "state")


def _sync_email_updates(mid: str, uid: str) -> None:
    """New AND changed email events (undo, reassign), by last-changed time; ignored or deleted ones are removed
    online. The cursor is a timestamp, and ">=" re-sends the boundary second instead of risking a skip."""
    since = (db.kv_get("cloud_email_since") or {}).get("v") or ""
    if since.isdigit():                              # older versions stored the last event id
        since = ""
    rows = db.fetchall(f"SELECT id, updated_at, {', '.join(EMAIL_FIELDS)} FROM email_events "
                       "WHERE coalesce(updated_at, '') >= ? ORDER BY updated_at, id LIMIT 500", (since,))
    keep = [r for r in rows if r["state"] != "ignored"]
    drop = [r["id"] for r in rows if r["state"] == "ignored"]
    tomb = db.fetchall("SELECT id FROM email_events_deleted ORDER BY id LIMIT 500")
    drop += [t["id"] for t in tomb]
    if keep:
        _rest("POST", "email_updates", params={"on_conflict": "machine_id,event_id"}, prefer="resolution=merge-duplicates",
              json=[{"machine_id": mid, "user_id": uid, "event_id": r["id"], **{k: r[k] for k in EMAIL_FIELDS}} for r in keep])
    for i in range(0, len(drop), 100):
        _rest("DELETE", "email_updates", params={"machine_id": f"eq.{mid}",
                                                 "event_id": "in.(" + ",".join(str(int(x)) for x in drop[i:i + 100]) + ")"})
    if tomb:
        db.execute(f"DELETE FROM email_events_deleted WHERE id IN ({','.join('?' * len(tomb))})", [t["id"] for t in tomb])
    if rows:
        db.kv_set("cloud_email_since", rows[-1]["updated_at"] or since)


def _reset_cursors() -> None:
    for k in ("cloud_jobs_since", "cloud_email_since"):
        db.execute("DELETE FROM kv WHERE k=?", (k,))


# ----------------------------------------------------------------------------- one sync cycle

def sync_once(log=print, full: bool = True) -> dict:
    """Push the snapshot (+ changed jobs and email updates when full), then pull and run requests."""
    if not connected():
        return {"queued": 0}
    st = state()
    mid, uid = st["machine_id"], st["user_id"]
    now = db.now()
    if full:
        _rest("POST", "snapshots", params={"on_conflict": "machine_id"}, prefer="resolution=merge-duplicates",
              json={"machine_id": mid, "user_id": uid, "data": snapshot(), "updated_at": now})
        since = (db.kv_get("cloud_jobs_since") or {}).get("v") or (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        changed = _changed_jobs(since)
        jobs = [j for j in changed if j["status"] in SYNC_STATUSES]
        gone = [j["id"] for j in changed if j["status"] not in SYNC_STATUSES]
        for i in range(0, len(gone), 100):
            ids = ",".join('"' + g.replace('"', '') + '"' for g in gone[i:i + 100])
            _rest("DELETE", "jobs", params={"machine_id": f"eq.{mid}", "job_id": f"in.({ids})"})
        if changed and not jobs:
            db.kv_set("cloud_jobs_since", changed[-1]["updated_at"])
        if jobs:
            payload = [{"machine_id": mid, "user_id": uid, "job_id": j["id"], "company": j["company"], "title": j["title"],
                        "location": j["location"], "status": j["status"], "match_score": j["match_score"], "salary": j["salary"],
                        "posted_at": j["posted_at"], "applied_at": j["applied_at"], "url": j["url"], "updated_at": now}
                       for j in jobs]
            for i in range(0, len(payload), 500):
                _rest("POST", "jobs", params={"on_conflict": "machine_id,job_id"}, prefer="resolution=merge-duplicates",
                      json=payload[i:i + 500])
            db.kv_set("cloud_jobs_since", changed[-1]["updated_at"])
        _sync_email_updates(mid, uid)
        _rest("PATCH", "machines", params={"id": f"eq.{mid}"}, json={"last_seen": now, "app_version": APP_VERSION, "city": city()})
        STATUS["operator"] = operator_stats()
        if STATUS["operator"]:
            notify_operator(log)
    queued = _run_requests(mid, log)
    STATUS.update(connected=True, last_sync=now, error="")
    return {"queued": queued}


def operator_stats() -> dict | None:
    """Site-wide user counts, only for the account listed in the site's operators table (else None)."""
    try:
        c = config()
        r = requests.post(f"{c['supabase_url']}/rest/v1/rpc/operator_stats", json={},
                          headers=_headers(access_token()), timeout=30)
        d = r.json() if r.status_code == 200 and r.text else None
        return d if isinstance(d, dict) and "users" in d else None
    except Exception:   # older site without the function, offline, ...: just hide the footer line
        return None


def _rpc(name: str, args: dict):
    c = config()
    r = requests.post(f"{c['supabase_url']}/rest/v1/rpc/{name}", json=args, headers=_headers(access_token()), timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"{name}: {r.status_code} {r.text[:200]}")
    return r.json() if r.text else None


def operator_users() -> list[dict] | None:
    """Operator only: everyone's name, email, chosen city, and application counts (no companies). Else None."""
    try:
        rows = _rpc("operator_users", {})
    except Exception:
        return None
    return rows if isinstance(rows, list) else None


def _join_email(ev: dict) -> tuple[str, str] | None:
    accts, comps = ev.get("accounts") or [], ev.get("computers") or []
    if not accts and not comps:
        return None
    who = lambda x: f"{x.get('name') or '(no name)'} <{x.get('email')}>"
    lines = []
    if accts:
        lines.append(f"New account{'s' if len(accts) > 1 else ''}:")
        lines += [f"  - {who(a)}{' (opted in to updates)' if a.get('opt_in') else ''}" for a in accts]
    if comps:
        lines.append("Set up on a new computer:")
        lines += [f"  - {who(c)}: {c.get('computer')}" for c in comps]
    parts = ([f"{len(accts)} new account{'s' if len(accts) > 1 else ''}"] if accts else []) + \
            ([f"{len(comps)} new computer{'s' if len(comps) > 1 else ''}"] if comps else [])
    return "Job Autopilot: " + " and ".join(parts), "\n".join(lines) + "\n\nSent by your own Job Autopilot (you're the site operator).\n"


def notify_operator(log=print) -> int:
    """Operator only: email yourself about new sign-ups and newly connected computers since the last check."""
    from . import mailsync
    since = (db.kv_get("op_notify_since") or {}).get("v")
    if not since:                                    # first run: start from now, don't email old history
        db.kv_set("op_notify_since", db.now())
        return 0
    try:
        ev = _rpc("operator_events", {"since": since}) or {}
    except Exception:                                # site without the function: nothing to do
        return 0
    mail = _join_email(ev)
    if mail:
        try:
            if not mailsync.send_to_self(*mail):
                return 0                             # no Gmail connected: keep them for later
        except Exception as e:
            log(f"couldn't send the sign-up email: {e}")
            return 0
        log("✉️  emailed you: " + mail[0])
    if ev.get("as_of"):
        db.kv_set("op_notify_since", ev["as_of"])
    return len(ev.get("accounts") or []) + len(ev.get("computers") or [])


def _run_requests(mid: str, log) -> int:
    from . import worker
    pending = _rest("GET", "commands", params={"machine_id": f"eq.{mid}", "status": "eq.pending", "order": "created_at",
                                               "select": "id,kind,created_at"}) or []
    n = 0
    for cmd in pending:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cmd["created_at"].replace("Z", "+00:00"))
        claimed = _rest("PATCH", "commands", params={"id": f"eq.{cmd['id']}", "status": "eq.pending"},
                        json={"status": "claimed", "claimed_at": db.now()}, prefer="return=representation")
        if not claimed:
            continue                      # another sync took it
        kind = cmd["kind"]
        try:
            if kind not in ALLOWED:
                result, status = "not allowed", "failed"
            elif age > timedelta(hours=6):
                result, status = "expired: the computer was offline too long", "expired"
            else:
                worker.resume()
                if kind != "resume":
                    worker.submit(kind, "phone" if kind in ("run", "process") else None)
                result, status = "started on " + (state().get("machine_name") or "your computer"), "done"
                n += 1
                log(f"📱 request from the online dashboard: {kind}")
        except Exception as e:                       # never leave a request stuck as "claimed"
            result, status = f"failed: {e}"[:200], "failed"
        _rest("PATCH", "commands", params={"id": f"eq.{cmd['id']}"},
              json={"status": status, "result": result, "finished_at": db.now()})
    STATUS["queued_from_phone"] = STATUS.get("queued_from_phone", 0) + n
    return n


def loop(log=print) -> None:
    """Background thread: requests every 30 s, full progress sync every 60 s, while connected."""
    tick = 0
    time.sleep(20)
    while True:
        try:
            if connected():
                sync_once(log, full=(tick % 2 == 0))
        except Exception as e:  # offline, token revoked, Supabase hiccup: keep going
            STATUS["error"] = str(e)[:200]
        tick += 1
        time.sleep(30)


def default_machine_name() -> str:
    """The computer's own name ("Ava's MacBook Air" on a Mac), else its network name."""
    if platform.system() == "Darwin":
        try:
            import subprocess
            name = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=5).stdout.strip()
            if name:
                return name[:80]
        except Exception:
            pass
    return (socket.gethostname().split(".")[0] or "My computer").replace("-", " ")[:80]
