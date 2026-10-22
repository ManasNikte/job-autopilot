"""Local dashboard: python -m app.cli serve  ->  http://127.0.0.1:5055"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import sys
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, url_for

from . import db, h1b, llm, progress, worker
from .config import ANSWERS_FILE, APPLICANT_FILE, DATA_DIR, PROFILE_DIR, ROOT, applicant, companies, env
from .pipeline import output_path_ok

app = Flask(__name__)
from .ext_api import bp as ext_bp  # noqa: E402  (browser extension API)
app.register_blueprint(ext_bp)
app.secret_key = "local-only"
app.config["TEMPLATES_AUTO_RELOAD"] = True   # template tweaks show up without restarting background work

_UPDATING_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="3"><title>Updating · Job Autopilot</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;font:16px system-ui,-apple-system,sans-serif;background:#0c0f15;color:#e7eaf0}
@media (prefers-color-scheme: light){body{background:#f5f6f8;color:#1b1f27}}p{opacity:.7}</style></head>
<body><div><h1>Job Autopilot is updating…</h1><p>This page opens the new version by itself in a few seconds.</p></div></body></html>"""


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


@app.before_request
def _local_hosts_only():
    """Answer only requests addressed to this computer. A website can point its own domain at 127.0.0.1 (DNS
    rebinding) and then read pages or press buttons here; such requests carry that domain as the Host, so refuse."""
    host = (request.host or "").rsplit(":", 1)[0] if not (request.host or "").startswith("[") else (request.host or "").split("]")[0] + "]"
    if host.lower() not in LOCAL_HOSTS:
        abort(403)


@app.template_filter("safeurl")
def safeurl(url: str | None) -> str:
    """Only http(s) links from emails make it into a page (never javascript: or data:)."""
    u = (url or "").strip()
    return u if re.match(r"^https?://", u, re.I) else ""


_DB_READY = False


@app.before_request
def _ensure_db():
    """Create the tables on the first request, however the app was started (serve() also does this)."""
    global _DB_READY
    if not _DB_READY:
        db.init()
        _DB_READY = True


@app.before_request
def _hold_pages_while_updating():
    """While an update is changing files, the running (old) code must not render pages from the new templates:
    that's what showed "Internal Server Error" mid-update. Everything waits on a plain page until the restart."""
    from . import updater
    if not updater.PROGRESS.get("lock"):
        return None
    if request.path.startswith(("/update/progress", "/healthz", "/static/")):
        return None
    if request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
        return jsonify({"error": "updating"}), 503
    return _UPDATING_HTML, 503, {"Retry-After": "3", "Cache-Control": "no-store"}

STATUS_LABELS = {
    "to_screen": "Waiting for screen", "rejected": "Don't apply", "needs_decision": "Your call",
    "screening": "Screening now", "tailoring": "Building documents now", "below_threshold": "Below your threshold",
    "to_tailor": "Queued for documents", "ready": "Ready to submit", "applied": "Applied",
    "interviewing": "Interviewing", "offer": "Offer", "closed": "Closed", "error": "Error",
    "filtered_out": "Filtered out", "new": "New",
}
PIPELINE_STAGES = ["to_screen", "screening", "needs_decision", "to_tailor", "tailoring", "ready", "applied", "interviewing", "offer"]


def _credits() -> dict:
    """Author credit for the header and footer (config/credits.json)."""
    try:
        from .config import CONFIG_DIR, load_json
        return load_json(CONFIG_DIR / "credits.json")
    except (OSError, ValueError):
        return {}


from .fmt import strf  # noqa: E402  (Windows-safe %-d / %-I)


app.add_template_filter(strf, "strf")


@app.template_filter("firsturl")
def firsturl(text: str | None) -> str:
    m = re.search(r"https?://[^\s<>\"')]+", text or "")
    return m.group(0) if m else ""


def _parse_dt(iso: str | None) -> datetime | None:
    """ISO text → aware datetime (naive values are treated as UTC); None for empty or unparseable input."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@app.template_filter("ago")
def ago(iso: str | None) -> str:
    dt = _parse_dt(iso)
    if not dt:
        return "—"
    s = max(0, (datetime.now(timezone.utc) - dt).total_seconds())   # clocks drift; never "-3m ago"
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


@app.template_filter("mdinline")
def mdinline(text: str | None):
    """Escape, then allow **bold** and `code` (release notes)."""
    from markupsafe import Markup, escape
    t = str(escape(text or ""))
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    return Markup(t)


@app.template_filter("local")
def local(iso: str | None) -> str:
    dt = _parse_dt(iso)
    return dt.astimezone().strftime("%b %d, %I:%M %p") if dt else "—"


def nav_index(operator: bool = False) -> list[dict]:
    """Everything the sidebar search can find: pages, sub-pages, settings pages, and individual settings.
    t = title, p = where it lives, u = link, k = extra words people might type."""
    S = lambda sec, anchor="": url_for("settings_page", section=sec) + (f"#{anchor}" if anchor else "")
    items = [
        ("Dashboard", "Overview", url_for("dashboard"), "home overview kpi numbers greeting maps chart"),
        ("How it works", "Overview", url_for("how_it_works"), "help explain guide steps what does it do faq questions"),
        ("Needs you", "Overview", url_for("inbox"), "to-do todo inbox decisions referral missing facts submit review"),
        ("Background", "Overview", url_for("background"), "progress queue runs activity log working screening"),
        ("All jobs", "Jobs", url_for("jobs"), "jobs postings roles search list"),
        ("Ready to submit", "Jobs", url_for("jobs", status="ready"), "ready apply documents resume cover letter"),
        ("In progress", "Jobs", url_for("jobs", status="queued"), "queued screening tailoring waiting"),
        ("Below threshold", "Jobs", url_for("jobs", status="below_threshold"), "low match score held"),
        ("Don't apply", "Jobs", url_for("jobs", status="rejected"), "rejected skipped not a fit"),
        ("Filtered out", "Jobs", url_for("jobs", status="filtered_out"), "rules prefilter dropped"),
        ("Applied", "Jobs", url_for("jobs", status="applied_all"), "submitted applications copy links date today yesterday week"),
        ("Assessments", "Jobs", url_for("assessments_page"), "online assessment oa test hackerrank codesignal due deadline reminder"),
        ("Interviews and offers", "Jobs", url_for("interviews_page"), "interview recruiter call onsite offer rejected after interview"),
        ("Email", "Inbox & network", url_for("email_page"), "inbox replies messages"),
        ("Email: assessments", "Email", url_for("email_page", event="assessment"), "oa test"),
        ("Email: interviews", "Email", url_for("email_page", event="interview"), "recruiter call schedule"),
        ("Email: offers", "Email", url_for("email_page", event="offer"), "offer letter"),
        ("Email: rejections", "Email", url_for("email_page", event="rejection"), "rejected declined not moving forward"),
        ("Email: not matched", "Email", url_for("email_page", state="unmatched"), "unmatched match to a job"),
        ("Outreach", "Inbox & network", url_for("outreach"), "linkedin notes messages connect"),
        ("People", "Inbox & network", url_for("people"), "contacts referrals networking spreadsheet xlsx"),
        ("Profile: master prompt", "Setup", url_for("profile_page"), "profile resume rules background experience prompt"),
        ("Profile: confirmed facts", "Setup", url_for("profile_page", f="answers"), "answers facts"),
        ("Profile: form presets", "Setup", url_for("profile_page", f="applicant"), "autofill phone address linkedin eeo applicant email"),
        ("Resume template", "Setup › Profile", url_for("resume_template_page"), "resume layout template ats classic compact projects experience format look overlap"),
        ("Resume rules in my prompt", "Setup › Profile", url_for("resume_template_page") + "#rules", "upgrade prompt rules achievements empty sections fit one page"),
        ("Sources", "Setup", url_for("sources_page"), "feeds boards greenhouse lever ashby workday uscis h-1b import csv"),
        ("Run times", "Settings › Schedule", S("schedule", "schedule"), "schedule daily time when runs start 8 am"),
        ("Lid closed / asleep", "Settings › Schedule", S("schedule", "wake"), "wake sleep pmset overnight laptop"),
        ("Job feeds", "Settings › Pipeline and limits", S("pipeline", "set-feeds"), "jobright simplify hacker news who is hiring"),
        ("H-1B filter", "Settings › Pipeline and limits", S("pipeline", "set-h1b"), "sponsor sponsorship visa h1b"),
        ("Restart dashboard", "Settings › Pipeline and limits", S("pipeline"), "restart reload"),
        ("Gmail", "Settings › Email inboxes", S("email"), "app password google imap connect inbox"),
        ("Outlook / Microsoft 365", "Settings › Email inboxes", S("email"), "microsoft azure office exchange university"),
        ("Apple Mail", "Settings › Email inboxes", S("email"), "mac mail app"),
        ("Online dashboard account", "Settings › Online dashboard", S("online"), "phone mobile sign up password create account connect computer site link"),
        ("Your city", "Settings › Online dashboard", S("online"), "location city operator"),
        ("Wake for phone requests", "Settings › Online dashboard", S("online"), "every 30 minutes remote wake"),
        ("App updates", "Settings › App updates", S("updates"), "version update check new release changelog"),
        ("Guided tour", "Settings › Getting started", S("start", "tour"), "tour walkthrough intro help onboarding"),
        ("Your name", "Settings › Getting started", S("start", "profile"), "name greeting"),
    ]
    items += [(t, "Settings", url_for("settings_page", section=k), h) for k, t, h in SETTINGS_SECTIONS]
    items += [(label, "Settings › Pipeline and limits", S("pipeline", f"set-{key}"), f"{key.lower().replace('_', ' ')} {help_}")
              for key, label, _, help_, _ in TUNABLES]
    if operator:
        items.append(("Users", "Site", url_for("users_page"), "operator users active people location"))
    return [{"t": t, "p": p, "u": u, "k": k.lower()} for t, p, u, k in items]


@app.context_processor
def inject():
    open_n = db.fetchone("SELECT count(*) AS n FROM interventions WHERE status='open'")["n"]
    try:
        backend = llm.backend()
    except llm.LLMError:
        backend = "not configured"
    try:
        unmatched_mail = db.count("email_events", "state='unmatched'")
    except Exception:
        unmatched_mail = 0
    from . import cloud, updater
    from .version import VERSION
    op = cloud.STATUS.get("operator") if request.endpoint == "dashboard" else None
    upd = updater.STATE if updater.update_available() else None
    ext_request = None
    try:
        from .ext_api import paired_extensions
        pend = json.loads((db.kv_get("ext_pending") or {}).get("v") or "null")
        if pend and pend.get("id") not in paired_extensions():
            ext_request = pend
    except Exception:
        ext_request = None
    return {"ext_request": ext_request, "open_interventions": open_n, "unmatched_mail": unmatched_mail, "credits": _credits(), "operator_stats": op, "operator_nav": bool(cloud.STATUS.get("operator")), "settings_nav": SETTINGS_SECTIONS, "nav_index": nav_index(bool(cloud.STATUS.get("operator"))), "app_update": upd, "app_version": VERSION, "worker": worker.state, "status_labels": STATUS_LABELS, "backend": backend}


def _followups() -> None:
    """Turn outreach timing rules (Section XI) into reminders: day 3-5 share work, day 7-10 one ask."""
    for o in db.fetchall("SELECT o.*, j.company FROM outreach o JOIN jobs j ON j.id=o.job_id WHERE o.status IN ('sent','accepted') AND o.sent_at IS NOT NULL"):
        sent = _parse_dt(o["sent_at"])
        if not sent:
            continue
        days = (datetime.now(timezone.utc) - sent).days
        who = o["person_name"] or o["target"].replace("_", " ")
        if 3 <= days <= 5:
            db.add_intervention(o["job_id"], "outreach", f"Follow up: share one piece of real work with {who} at {o['company']}",
                                "Section XI: a short note with one artifact (e.g. a project README result). No pitch.")
        elif 7 <= days <= 10:
            db.add_intervention(o["job_id"], "outreach", f"Follow up: one specific low-effort ask to {who} at {o['company']}",
                                "One follow-up max. A 15-minute coffee chat or one question about the team.")


def _profile_email() -> str:
    """The email in your form presets (profile/applicant.json), unless it's still the placeholder."""
    try:
        e = (json.loads(APPLICANT_FILE.read_text(encoding="utf-8")).get("email") or "").strip()
    except (OSError, ValueError):
        return ""
    return "" if e.endswith("@example.com") else e


def week_series(sunday=None) -> dict:
    """Applications per local day for one Sunday-to-Saturday week (default: this week)."""
    today = datetime.now().astimezone().date()
    sunday = sunday or today - timedelta(days=(today.weekday() + 1) % 7)
    days = [sunday + timedelta(days=i) for i in range(7)]
    per_day = {d: 0 for d in days}
    prev = 0
    lo = datetime.combine(sunday - timedelta(days=7), datetime.min.time()).astimezone().astimezone(timezone.utc).isoformat()
    hi = datetime.combine(sunday + timedelta(days=7), datetime.min.time()).astimezone().astimezone(timezone.utc).isoformat()
    for r in db.fetchall("SELECT applied_at FROM jobs WHERE applied_at >= ? AND applied_at < ?", (lo, hi)):
        dt = _parse_dt(r["applied_at"])
        if not dt:
            continue
        d = dt.astimezone().date()
        if d in per_day:
            per_day[d] += 1
        elif d < sunday:
            prev += 1
    out = [{"date": d.isoformat(), "dow": d.strftime("%a"), "label": strf(d, "%a %b %-d"), "day": d.day, "n": per_day[d],
            "today": d == today, "future": d > today} for d in days]
    total = sum(x["n"] for x in out)
    past = [x for x in out if not x["future"]] or out
    return {"start": sunday.isoformat(), "title": f"{strf(days[0], '%b %-d')} – {strf(days[-1], '%b %-d')}",
            "current": sunday <= today <= days[-1], "days": out, "total": total, "prev_total": prev,
            "avg": round(total / len(past), 1), "peak": max([x["n"] for x in out] + [1]),
            "best": max(out, key=lambda x: x["n"]) if total else None}


def week_options(limit: int = 26) -> list[dict]:
    """Weeks to pick from: every week since the first application (newest first), at most `limit`."""
    today = datetime.now().astimezone().date()
    this_sun = today - timedelta(days=(today.weekday() + 1) % 7)
    first = db.fetchone("SELECT min(applied_at) AS a FROM jobs WHERE applied_at IS NOT NULL")["a"]
    first_d = (_parse_dt(first) or datetime.now(timezone.utc)).astimezone().date()
    first_sun = first_d - timedelta(days=(first_d.weekday() + 1) % 7)
    weeks, sun = [], this_sun
    while sun >= first_sun and len(weeks) < limit:
        weeks.append({"start": sun.isoformat(), "label": ("This week" if sun == this_sun else "Last week" if sun == this_sun - timedelta(days=7)
                      else f"Week of {strf(sun, '%b %-d')}")})
        sun -= timedelta(days=7)
    return weeks or [{"start": this_sun.isoformat(), "label": "This week"}]


# Minutes a person would spend on each step by hand (conservative estimates, shown on the dashboard).
MANUAL_MINUTES = {"screen": 5, "docs": 45, "form": 10, "email": 2}


def time_saved() -> dict:
    """Hours the app saved versus doing it by hand: reading each JD to judge fit, writing tailored documents,
    filling application forms, and sorting replies."""
    n = {"screen": db.count("jobs", "match_score IS NOT NULL"),
         "docs": db.count("jobs", "tailor_json IS NOT NULL"),
         "form": db.count("jobs", "applied_at IS NOT NULL"),
         "email": db.count("email_events", "state != 'ignored'") if db.fetchone(
             "SELECT 1 FROM sqlite_master WHERE type='table' AND name='email_events'") else 0}
    labels = {"screen": ("Job descriptions read and judged for fit", "screened"),
              "docs": ("Tailored resumes and cover letters", "written"),
              "form": ("Application forms filled", "filled"),
              "email": ("Application emails sorted", "sorted")}
    rows = [{"key": k, "label": labels[k][0], "verb": labels[k][1], "n": n[k], "per": MANUAL_MINUTES[k],
             "hours": round(n[k] * MANUAL_MINUTES[k] / 60, 1)} for k in ("screen", "docs", "form", "email")]
    total_h = sum(r["hours"] for r in rows)
    peak = max([r["hours"] for r in rows] + [0.1])
    for r in rows:
        r["pct"] = round(r["hours"] / peak * 100, 1)
    return {"hours": round(total_h), "days": round(total_h / 8, 1), "rows": rows}


def donut_slices(rows: list[dict], top: int = 6, slots: dict | None = None) -> dict:
    """SVG donut arcs by role family: the six largest families plus Other (colors in fixed order). With `slots`
    (family -> color slot, from the Applied donut), every donut colors a family the same way and folds families
    that aren't among them into Other, so the three donuts can be read side by side."""
    import math
    rows = [dict(r) for r in rows if r["n"]]
    if slots is not None:
        main = [r for r in rows if r["fam"] in slots and slots[r["fam"]] != "other"]
        rest = [r for r in rows if r not in main]
    else:
        main, rest = rows[:top], rows[top:]
    other = sum(r["n"] for r in rest) + sum(r["n"] for r in main if r["fam"] == "Other")
    main = [r for r in main if r["fam"] != "Other"]
    if other:
        main.append({"fam": "Other", "n": other})
    total = sum(r["n"] for r in main)
    if not total:
        return {"total": 0, "slices": []}
    R, r_in, cx, cy, gap = 80, 52, 90, 90, 0.025       # gap: radians of surface between slices
    out, a = [], -math.pi / 2
    for i, row in enumerate(main):
        sweep = row["n"] / total * 2 * math.pi
        a0, a1 = a + (gap / 2 if len(main) > 1 else 0), a + sweep - (gap / 2 if len(main) > 1 else 0)
        if len(main) == 1:
            a1 = a0 + 2 * math.pi - 1e-4
        large = 1 if a1 - a0 > math.pi else 0
        pt = lambda rad, ang: (cx + rad * math.cos(ang), cy + rad * math.sin(ang))
        (x0, y0), (x1, y1), (x2, y2), (x3, y3) = pt(R, a0), pt(R, a1), pt(r_in, a1), pt(r_in, a0)
        d = (f"M{x0:.2f},{y0:.2f} A{R},{R} 0 {large} 1 {x1:.2f},{y1:.2f} L{x2:.2f},{y2:.2f} "
             f"A{r_in},{r_in} 0 {large} 0 {x3:.2f},{y3:.2f} Z")
        slot = "other" if row["fam"] == "Other" else (slots or {}).get(row["fam"], str(i + 1))
        out.append({"fam": row["fam"], "n": row["n"], "pct": round(row["n"] / total * 100), "d": d, "slot": slot})
        a += sweep
    return {"total": total, "slices": out}


def active_series(slots: dict, days: int = 60, today=None) -> dict:
    """Active applications per role family for each day: applied by then and not yet rejected (status_changed_at
    of a rejection is the day it closed). Starts at your first application, at most `days` back. Families keep
    their donut colors; the rest fold into Other."""
    today = today or datetime.now().astimezone().date()
    rows = db.fetchall("SELECT coalesce(role_family,'Other') AS fam, status, applied_at, status_changed_at FROM jobs "
                       "WHERE applied_at IS NOT NULL AND status IN ('applied','interviewing','offer','closed')")
    spans = []
    for r in rows:
        start = _parse_dt(r["applied_at"])
        if not start:
            continue
        end = _parse_dt(r["status_changed_at"]) if r["status"] == "closed" else None
        fam = r["fam"] if r["fam"] in slots and slots[r["fam"]] != "other" else "Other"
        spans.append((fam, start.astimezone().date(), end.astimezone().date() if end else None))
    if not spans:
        return {"days": [], "series": []}
    first = max(min(sp[1] for sp in spans), today - timedelta(days=days - 1))
    dates = [first + timedelta(days=i) for i in range((today - first).days + 1)]
    fams = [f for f in slots if slots[f] != "other"] + ["Other"]
    series = []
    for fam in fams:
        mine = [sp for sp in spans if sp[0] == fam]
        if not mine:
            continue
        pts = [sum(1 for _, a, z in mine if a <= d and (z is None or z > d)) for d in dates]
        series.append({"fam": fam, "slot": slots.get(fam, "other"), "points": pts, "now": pts[-1]})
    series.sort(key=lambda x: -x["now"])
    return {"days": [{"iso": d.isoformat(), "label": strf(datetime.combine(d, datetime.min.time()), "%b %-d"),
                      "dow": strf(datetime.combine(d, datetime.min.time()), "%a")} for d in dates],
            "series": series, "total": sum(x["now"] for x in series)}


@app.get("/api/week")
def week_json():
    try:
        sunday = datetime.strptime(request.args.get("start", ""), "%Y-%m-%d").date()
    except ValueError:
        sunday = None
    if sunday:
        sunday -= timedelta(days=(sunday.weekday() + 1) % 7)       # any date → its Sunday
    return jsonify(week_series(sunday))


def week_start_utc(today=None) -> str:
    """This calendar week's start, Sunday 00:00 on this computer's clock (weeks run Sunday to Saturday), in UTC."""
    today = today or datetime.now().astimezone().date()
    sunday = today - timedelta(days=(today.weekday() + 1) % 7)     # weekday(): Monday 0 … Sunday 6
    return datetime.combine(sunday, datetime.min.time()).astimezone().astimezone(timezone.utc).isoformat()


@app.route("/")
def dashboard():
    _followups()
    just_updated = None
    if request.args.get("updated"):
        from . import updater
        from .version import VERSION
        try:
            if db.kv_get("updated_from") is None:              # updated by an older version of the updater
                prev = updater.version_before_update()
                if prev:
                    db.kv_set("updated_from", prev)
            since = (db.kv_get("updated_from") or {}).get("v") or ""
            notes = updater.parse_changelog((updater.ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), "0")
            mine = [n for n in notes if updater._vkey(n["version"]) <= updater._vkey(VERSION)
                    and (not since or updater._vkey(n["version"]) > updater._vkey(since))] or \
                   [n for n in notes if n["version"] == VERSION]
            just_updated = {"version": VERSION, "since": since, "sections": mine,
                            "items": [i for n in mine for i in n["items"]]}
            aside = (db.kv_get("update_set_aside") or {}).get("v")
            if aside:
                try:
                    just_updated["set_aside"] = json.loads(aside)
                except ValueError:
                    pass
                db.kv_set("update_set_aside", "")
        except OSError:
            just_updated = {"version": VERSION, "items": []}
    today = datetime.now().astimezone().date()
    start_utc = datetime.combine(today, datetime.min.time()).astimezone().astimezone(timezone.utc).isoformat()
    counts = {r["status"]: r["n"] for r in db.fetchall("SELECT status, count(*) AS n FROM jobs GROUP BY status")}
    applied_total = sum(counts.get(s, 0) for s in ("applied", "interviewing", "offer", "closed"))
    kpis = {
        "posted_today": db.fetchone("SELECT count(*) AS n FROM jobs WHERE posted_at>=? AND status!='filtered_out'", (start_utc,))["n"],
        "ready": counts.get("ready", 0),
        "decide": counts.get("needs_decision", 0),
        "applied_week": db.fetchone("SELECT count(*) AS n FROM jobs WHERE applied_at>=?", (week_start_utc(),))["n"],
        "applied_today": db.fetchone("SELECT count(*) AS n FROM jobs WHERE applied_at>=?", (start_utc,))["n"],
        "applied_total": applied_total,
        "interviewing": counts.get("interviewing", 0) + counts.get("offer", 0),
    }
    from . import interviews
    kpis.update({f"iv_{k}": v for k, v in interviews.counts().items()})
    from . import assessments
    assess = assessments.pending()
    for a in assess:
        a["label"] = assessments.due_label(a)
    kpis["assessments"] = len(assess)          # the same list the card below shows (overdue ones stay a day)
    week = week_series()
    weeks = week_options()
    saved = time_saved()
    tone = {"to_screen": 1, "screening": 1, "needs_decision": 2, "to_tailor": 2, "tailoring": 2, "ready": 3,
            "applied": 4, "interviewing": 5, "offer": 5}            # ordinal shade: early stages light, outcomes dark
    funnel = [{"status": s, "label": STATUS_LABELS[s], "n": counts.get(s, 0), "tone": tone.get(s, 3)} for s in PIPELINE_STAGES]
    fmax = max([f["n"] for f in funnel] + [1])
    ftotal = sum(f["n"] for f in funnel) or 1
    today_cond = "posted_at>=? AND status NOT IN ('filtered_out')"
    today_page = Page(db.count("jobs", today_cond, (start_utc,)), per_page=10, param="today_page", anchor="today")
    today_jobs = db.list_jobs(today_cond, (start_utc,), order="coalesce(match_score, -1) DESC, posted_at DESC",
                              limit=today_page.per_page, offset=today_page.offset)
    applied = "status IN ('applied','interviewing','offer','closed')"
    fam_rows = db.fetchall(f"SELECT coalesce(role_family,'Other') AS fam, count(*) AS n FROM jobs WHERE {applied} "
                           "GROUP BY fam ORDER BY n DESC, fam")
    family_page = Page(len(fam_rows), per_page=6, param="family_page", anchor="by-family")
    by_family = fam_rows[family_page.offset:family_page.offset + family_page.per_page]
    donut = donut_slices(fam_rows)
    slots = {sl["fam"]: sl["slot"] for sl in donut["slices"]}
    rej_rows = db.fetchall("SELECT coalesce(role_family,'Other') AS fam, count(*) AS n FROM jobs WHERE status='closed' "
                           "GROUP BY fam ORDER BY n DESC, fam")
    oa_rows = db.fetchall("SELECT coalesce(j.role_family,'Other') AS fam, count(DISTINCT j.id) AS n FROM email_events e "
                          "JOIN jobs j ON j.id = e.job_id WHERE e.event='assessment' AND e.state NOT IN ('ignored','undone') "
                          "GROUP BY fam ORDER BY n DESC, fam")
    oa_outside = db.fetchone("SELECT count(DISTINCT lower(coalesce(company,''))) AS n FROM email_events WHERE event='assessment' "
                             "AND job_id IS NULL AND state NOT IN ('ignored','undone')")["n"]
    active = active_series(slots)
    donuts = [
        {"id": "by-family", "title": "Applied by role family", "word": "applied", **donut, "link": url_for("jobs", status="applied_all")},
        {"id": "rejected-family", "title": "Rejected by role family", "word": "rejected", **donut_slices(rej_rows, slots=slots),
         "empty": "No rejections yet.", "link": url_for("email_page", event="rejection")},
        {"id": "assess-family", "title": "Assessments by role family", "word": "assessments", **donut_slices(oa_rows, slots=slots),
         "empty": "No assessments yet.", "link": url_for("assessments_page"),
         "note": f"Plus {oa_outside} from companies you applied to outside the app." if oa_outside else ""},
    ]
    n_companies = db.fetchone(f"SELECT count(DISTINCT company) AS n FROM jobs WHERE {applied}")["n"]
    company_page = Page(n_companies, per_page=6, param="company_page", anchor="by-company")
    by_company = db.fetchall(f"SELECT company, count(*) AS n FROM jobs WHERE {applied} GROUP BY company "
                             "ORDER BY n DESC, company COLLATE NOCASE LIMIT ? OFFSET ?", (company_page.per_page, company_page.offset))
    last_run = db.fetchone("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
    from . import greeting
    user_name = greeting.user_name()
    hello = greeting.greet(name=user_name) if user_name else None
    show_tour = bool(request.args.get("tour")) or (bool(user_name) and not db.kv_get("tour"))   # after the name box
    new_tour = [] if show_tour else whats_new_steps((db.kv_get("updated_from") or {}).get("v"))
    from . import cloud
    cloud_prompt = (bool(cloud.config()) and not cloud.connected() and not db.kv_get("cloud_prompt") and bool(user_name)
                    and not show_tour)
    return render_template("dashboard.html", cloud_prompt=cloud_prompt, assess=assess, tour=tour_steps(user_name) if show_tour else (new_tour or None), tour_kind="" if show_tour else "new", just_updated=just_updated, hello=hello, user_name=user_name, kpis=kpis, week=week, weeks=weeks, saved=saved, donut=donut, donuts=donuts, active=active, funnel=funnel, fmax=fmax, ftotal=ftotal,
                           today_jobs=today_jobs, by_family=by_family, by_company=by_company, last_run=last_run, counts=counts,
                           today_page=today_page, family_page=family_page, company_page=company_page,
                           applied_total=sum(r["n"] for r in fam_rows))


class Page:
    """Server-side pagination: page/per_page from the query string, links that keep the other filters."""

    def __init__(self, total: int, per_page: int = 50, param: str = "page", anchor: str = ""):
        self.total, self.per_page, self.param, self.anchor = total, per_page, param, anchor
        self.pages = max(1, -(-total // per_page))
        try:
            self.page = min(self.pages, max(1, int(request.args.get(param, 1))))
        except ValueError:
            self.page = 1
        self.offset = (self.page - 1) * per_page
        self.first = self.offset + 1 if total else 0
        self.last = min(total, self.offset + per_page)

    def url(self, page: int) -> str:
        args = request.args.to_dict()
        args[self.param] = page
        return url_for(request.endpoint, **(request.view_args or {}), **args) + (f"#{self.anchor}" if self.anchor else "")

    def window(self) -> list:
        """1 … 4 5 [6] 7 8 … 20"""
        keep = {1, self.pages, *range(self.page - 2, self.page + 3)}
        out, prev = [], 0
        for n in sorted(k for k in keep if 1 <= k <= self.pages):
            if n - prev > 1:
                out.append(None)
            out.append(n)
            prev = n
        return out


JOB_TABS = [  # (key, label, SQL condition)
    ("", "All", "status!='filtered_out'"),
    ("ready", "Ready to submit", "status='ready'"),
    ("queued", "In progress", "status IN ('to_screen','screening','to_tailor','tailoring','needs_decision')"),
    ("applied_all", "Applied", "status IN ('applied','interviewing','offer','closed')"),
    ("below_threshold", "Below threshold", "status='below_threshold'"),
    ("rejected", "Don't apply", "status='rejected'"),
    ("filtered_out", "Filtered out", "status='filtered_out'"),
]
JOB_SORTS = {
    "posted": ("Newest posted", "posted_at DESC"),
    "match": ("Best match", "coalesce(match_score, -1) DESC, posted_at DESC"),
    "company": ("Company A–Z", "company COLLATE NOCASE ASC, posted_at DESC"),
    "applied": ("Recently applied", "applied_at DESC"),
    "updated": ("Recently updated", "updated_at DESC"),
}


def _job_filters(args) -> dict:
    """Shared by the Jobs list and 'Copy links': tab, search, source, startup, match, and applied-date filters."""
    tab = args.get("status", "")
    q = args.get("q", "").strip()
    source = args.get("source", "")
    startup = args.get("startup") == "1"
    min_match = args.get("min_match", "")
    where, params = [], []
    tabs = {k: cond for k, _, cond in JOB_TABS}
    if tab in tabs:
        where.append(tabs[tab])
    elif tab:                                   # any single status (e.g. links from the dashboard funnel)
        where.append("status=?")
        params.append(tab)
    filters, fparams = [], []
    if source == "companies":
        filters.append("source IN ('greenhouse','lever','ashby')")
    elif source:
        filters.append("source=?")
        fparams.append(source)
    if startup:
        filters.append("startup=1")
    if min_match.isdigit():
        filters.append("match_score >= ?")
        fparams.append(int(min_match))
    if q:
        filters.append("(company LIKE ? OR title LIKE ? OR location LIKE ?)")
        fparams += [f"%{q}%"] * 3
    # Applied date (your local days): a quick range, or any From / To dates
    when, d_from, d_to = args.get("applied", ""), args.get("from", ""), args.get("to", "")
    today = datetime.now().astimezone().date()
    quick = {"today": (today, today), "yesterday": (today - timedelta(days=1),) * 2,
             "7d": (today - timedelta(days=6), today), "30d": (today - timedelta(days=29), today)}
    lo = hi = None
    if when in quick:
        lo, hi = quick[when]
    elif d_from or d_to:
        try:
            lo = date.fromisoformat(d_from) if d_from else None
            hi = date.fromisoformat(d_to) if d_to else None
        except ValueError:
            lo = hi = None
        if lo and hi and lo > hi:
            lo, hi = hi, lo
        when = "custom" if (lo or hi) else ""
    utc = lambda d: datetime.combine(d, datetime.min.time()).astimezone().astimezone(timezone.utc).isoformat()
    if lo:
        filters.append("applied_at >= ?")
        fparams.append(utc(lo))
    if hi:
        filters.append("applied_at < ?")
        fparams.append(utc(hi + timedelta(days=1)))
    return {"tab": tab, "q": q, "source": source, "startup": startup, "min_match": min_match, "where": where, "params": params,
            "filters": filters, "fparams": fparams, "applied": when, "from": lo.isoformat() if lo else "",
            "to": hi.isoformat() if hi else "", "cond": " AND ".join(where + filters) or "1=1"}


@app.route("/jobs")
def jobs():
    f = _job_filters(request.args)
    tab = f["tab"]
    sort = request.args.get("sort") or ("applied" if tab in ("applied", "applied_all") or f["applied"] else "posted")
    allp = tuple(f["params"] + f["fparams"])
    page = Page(db.count("jobs", f["cond"], allp))
    rows = db.list_jobs(f["cond"], allp, order=JOB_SORTS.get(sort, JOB_SORTS["posted"])[1],
                        limit=page.per_page, offset=page.offset)
    # tab counts honour the search/filters, so you see where your matches are
    fcond = " AND ".join(f["filters"]) or "1=1"
    counts = {k: db.count("jobs", f"({c}) AND {fcond}", tuple(f["fparams"])) for k, _, c in JOB_TABS}
    show_dates = tab in ("applied_all", "applied", "interviewing", "offer", "closed") or bool(f["applied"])
    return render_template("jobs.html", jobs=rows, status=tab, q=f["q"], source=f["source"], startup=f["startup"],
                           min_match=f["min_match"], sort=sort, sorts=JOB_SORTS, tabs=JOB_TABS, counts=counts, page=page,
                           applied=f["applied"], d_from=f["from"], d_to=f["to"], show_dates=show_dates,
                           today_iso=datetime.now().astimezone().date().isoformat())


@app.route("/jobs/links")
def job_links():
    """Application links for EVERY job matching the current filters (not just the visible page)."""
    f = _job_filters(request.args)
    rows = db.fetchall(f"SELECT company, title, coalesce(nullif(apply_url, ''), url) AS link FROM jobs WHERE {f['cond']} "
                       "ORDER BY coalesce(applied_at, posted_at) DESC", tuple(f["params"] + f["fparams"]))
    rows = [r for r in rows if r["link"]]
    return jsonify({"count": len(rows), "links": rows})


@app.route("/jobs/<path:job_id>")
def job_detail(job_id):
    job = db.get_job(job_id) or abort(404)
    ivs = db.fetchall("SELECT * FROM interventions WHERE job_id=? ORDER BY status='open' DESC, id DESC", (job_id,))
    outreach = db.fetchall("SELECT * FROM outreach WHERE job_id=? ORDER BY target", (job_id,))
    from . import contacts
    from .llm import prompt_version
    current_prompt = prompt_version()
    return render_template("job.html", job=job, current_prompt=current_prompt, ivs=ivs, outreach=outreach, applicant=applicant(), h1b=h1b.lookup(job["company"]),
                           asks=contacts.asks_for_job(job_id), known=contacts.contacts_for_company(job["company"], referable_only=False),
                           stages=contacts.STAGES)


@app.post("/jobs/<path:job_id>/action")
def job_action(job_id):
    job = db.get_job(job_id) or abort(404)
    act = request.form["action"]
    if act == "applied":
        db.update_job(job_id, status="applied", applied_at=db.now())
        db.resolve_interventions(job_id, ("submit",), "submitted")
        flash(f"Marked applied: {job['company']}. LinkedIn outreach is ready on this page.")
    elif act in ("interviewing", "offer", "closed"):
        db.update_job(job_id, status=act)
        if act == "closed":
            db.resolve_interventions(job_id, ("submit", "decision", "missing_info", "quality", "review", "outreach"), "closed")
    elif act == "build_anyway":     # below your match threshold -> build this one anyway
        db.update_job(job_id, status="to_tailor", status_reason="build anyway")
        worker.submit("tailor", job_id)
        flash("Building documents for this job anyway.")
    elif act == "approve":          # APPLY WITH RISK -> build anyway
        db.update_job(job_id, status="to_tailor", status_reason="approved by you")
        db.resolve_interventions(job_id, ("decision",), "approved")
        worker.submit("tailor", job_id)
        flash("Building documents in the background.")
    elif act == "proceed":          # DON'T APPLY -> proceed anyway
        db.update_job(job_id, status="to_tailor", status_reason="proceed anyway")
        db.resolve_interventions(job_id, ("decision",), "proceed anyway")
        worker.submit("tailor", job_id)
        flash("Proceeding anyway; the verdict stays on record.")
    elif act == "skip":
        from .pipeline import skip_job
        removed = skip_job(job_id)
        flash(f"Skipped {job['company']} · {job['title']}: marked Don't apply"
              + (f" and deleted its {removed} document file(s)." if removed else "."))
    elif act == "rescreen":
        worker.resume()
        db.update_job(job_id, status="to_screen")
        worker.submit("screen", job_id)
    elif act == "fix_docs":
        worker.resume()
        worker.submit("refit", job_id)
        flash("Fixing the documents in the background: tighter spacing within your rules first, then a small Claude trim only if needed.")
    elif act == "regenerate" and job["status"] in ("applied", "interviewing", "offer", "closed", "tailoring"):
        flash("This job is already " + STATUS_LABELS.get(job["status"], job["status"]).lower()
              + ", so its documents stay as they are (rebuilding would reset its status).")
    elif act == "regenerate":
        from .llm import prompt_version
        db.update_job(job_id, status="to_tailor")
        worker.submit("tailor", job_id)
        pv = prompt_version()
        flash("Regenerating documents in the background with your master prompt"
              + (f" saved {datetime.fromisoformat(pv):%b %d, %I:%M %p}" if pv else "") + ". The links update when the new PDFs are ready.")
    elif act == "autofill":
        import importlib.util
        if importlib.util.find_spec("playwright") is None:
            flash("Autofill needs Playwright: run `.venv/bin/pip install playwright && .venv/bin/python -m playwright install chromium` in the job-autopilot folder.")
        elif not job.get("resume_pdf"):
            flash("No resume PDF for this job yet; build the documents first.")
        else:
            log = open(DATA_DIR / "autofill.log", "a", encoding="utf-8")
            log.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M} {job['company']} | {job['title']}\n")
            log.flush()
            subprocess.Popen([sys.executable, "-m", "app.autofill", job_id], cwd=ROOT, stdout=log, stderr=log)
            flash("A Chromium window is opening. It uploads your resume and cover letter, then Claude reads every question and fills answers from your profile (about a minute); fields that need you are outlined in orange. "
                  "Answer the remaining questions, press Submit yourself, then click \"I submitted it\" here. "
                  "If nothing opens within ~10 seconds, see data/autofill.log.")
    elif act == "toggle_board":
        pass
    elif act == "notes":
        db.update_job(job_id, notes=request.form.get("notes", ""))
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/jobs/<path:job_id>/open")
def open_application(job_id):
    """Remember which job you are applying to (the extension uses it to match the page), then go there."""
    job = db.get_job(job_id) or abort(404)
    db.kv_set("last_opened_job", job_id)
    target = job.get("apply_url") or job.get("url")
    if not target:
        flash("This job has no application link saved. Search the company's careers page for it.")
        return redirect(url_for("job_detail", job_id=job_id))
    return redirect(target)


@app.route("/inbox")
def inbox():
    _followups()
    ivs = db.fetchall(
        "SELECT i.*, j.company, j.title AS job_title, j.match_score, j.verdict FROM interventions i LEFT JOIN jobs j ON j.id=i.job_id "
        "WHERE i.status='open' ORDER BY CASE i.kind WHEN 'offer' THEN -3 WHEN 'interview' THEN -2 WHEN 'assessment' THEN -1 "
        "WHEN 'error' THEN 0 WHEN 'referral' THEN 1 WHEN 'decision' THEN 2 WHEN 'missing_info' THEN 3 "
        "WHEN 'quality' THEN 4 WHEN 'submit' THEN 5 WHEN 'outreach' THEN 6 ELSE 7 END, i.id DESC")
    for i in ivs:
        if i["kind"] == "referral":
            ask = db.fetchone("SELECT a.*, c.name, c.phone, c.linkedin, c.email FROM referral_asks a JOIN contacts c ON c.id=a.contact_id "
                              "WHERE a.job_id=? AND ? LIKE 'Ask ' || c.name || ' (%'", (i["job_id"], i["title"]))
            i["ask"] = ask
        if i["kind"] in ("assessment", "interview", "offer"):
            i["email_id"] = _email_for(i)
    groups: dict[str, list] = {}
    for i in ivs:
        groups.setdefault(i["kind"], []).append(i)
    return render_template("inbox.html", groups=groups)


def _email_for(iv: dict) -> int | None:
    """The email behind a Needs-you item: the job's latest email of that kind, else (no job) the company named in it."""
    if iv.get("job_id"):
        r = db.fetchone("SELECT id FROM email_events WHERE job_id=? AND event=? AND state != 'ignored' ORDER BY received_at DESC LIMIT 1",
                        (iv["job_id"], iv["kind"]))
        if r:
            return r["id"]
    m = re.match(r"^(?:Online assessment|Interview invite|Offer) from (.+?)(?: · .*)?$", iv.get("title") or "")
    if m:
        r = db.fetchone("SELECT id FROM email_events WHERE company=? AND event=? AND state != 'ignored' ORDER BY received_at DESC LIMIT 1",
                        (m.group(1), iv["kind"]))
        return r["id"] if r else None
    return None


@app.post("/inbox/<int:iid>")
def inbox_action(iid):
    iv = db.fetchone("SELECT * FROM interventions WHERE id=?", (iid,)) or abort(404)
    act = request.form["action"]
    if act == "answer":
        answer = request.form.get("answer", "").strip()
        if answer:
            with open(ANSWERS_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n- Q: {iv['title']}\n  A (confirmed {datetime.now():%b %d, %Y}): {answer}\n")
            db.execute("UPDATE interventions SET status='resolved', resolution=?, resolved_at=? WHERE id=?", (answer, db.now(), iid))
            # Same question asked for other jobs is answered too.
            db.execute("UPDATE interventions SET status='resolved', resolution=?, resolved_at=? WHERE kind='missing_info' AND title=? AND status='open'",
                       (answer, db.now(), iv["title"]))
            if iv["job_id"] and request.form.get("rebuild"):
                worker.submit("tailor", iv["job_id"])
            flash("Saved to profile/answers.md. Every future run uses it.")
    elif act == "fix" and iv["job_id"]:
        worker.resume()
        worker.submit("refit", iv["job_id"])
        flash("Fixing in the background: tighter spacing within your rules first, then a small Claude trim only if needed.")
    elif act == "retry" and iv["job_id"]:
        worker.resume()
        job = db.get_job(iv["job_id"])
        if iv["title"].startswith("Tailoring"):
            db.update_job(iv["job_id"], status="to_tailor")
            worker.submit("tailor", iv["job_id"])
        else:
            db.update_job(iv["job_id"], status="to_screen")
            worker.submit("screen", iv["job_id"])
        flash(f"Retrying {job['company'] if job else 'the job'} in the background. The card clears itself when it works.")
    else:
        db.execute("UPDATE interventions SET status=?, resolved_at=? WHERE id=?",
                   ("dismissed" if act == "dismiss" else "resolved", db.now(), iid))
    return redirect(request.referrer or url_for("inbox"))


# ----------------------------------------------------------------------------- email status tracking

EMAIL_EVENTS = {"rejection": "Rejected", "interview": "Interview", "assessment": "Assessment", "offer": "Offer",
                "confirmation": "Received", "other": "Other"}


@app.route("/email")
def email_page():
    from . import mailsync
    mailsync.init()
    ev, state, q = request.args.get("event", ""), request.args.get("state", ""), request.args.get("q", "").strip()
    focus = request.args.get("id", type=int)
    # every column is qualified: the query joins jobs, which has company/title/status columns too
    where, params = ["e.state != 'ignored'"], []
    if focus:
        where.append("e.id=?")
        params.append(focus)
    if ev:
        where.append("e.event=?")
        params.append(ev)
    if state:
        where.append("e.state=?")
        params.append(state)
    if q:
        where.append("(e.company LIKE ? OR e.role LIKE ? OR e.subject LIKE ? OR e.sender LIKE ? OR j.company LIKE ? OR j.title LIKE ?)")
        params += [f"%{q}%"] * 6
    cond = " AND ".join(where)
    page = Page(db.fetchone(f"SELECT count(*) AS n FROM email_events e LEFT JOIN jobs j ON j.id=e.job_id WHERE {cond}", tuple(params))["n"])
    rows = db.fetchall(f"SELECT e.*, j.company AS job_company, j.title AS job_title, j.status AS job_status, "
                       "(SELECT 1 FROM email_events x WHERE x.msg_id = e.msg_id || '#rejected') AS marked_rejected, "
                       "(SELECT 1 FROM email_events x WHERE x.msg_id = e.msg_id || '#offer') AS marked_offer FROM email_events e "
                       f"LEFT JOIN jobs j ON j.id=e.job_id WHERE {cond} ORDER BY e.received_at DESC LIMIT ? OFFSET ?",
                       (*params, page.per_page, page.offset))
    counts = {r["event"]: r["n"] for r in db.fetchall("SELECT event, count(*) AS n FROM email_events WHERE state != 'ignored' GROUP BY event")}
    unmatched = db.count("email_events", "state='unmatched'")
    jobs_for_assign = db.fetchall("SELECT id, company, title FROM jobs WHERE status IN ('ready','applied','interviewing','offer','closed') "
                                  "ORDER BY coalesce(applied_at, updated_at) DESC LIMIT 400") if unmatched else []
    last = db.kv_get("mail_last_sync")
    return render_template("email.html", focus=focus, rows=rows, page=page, counts=counts, unmatched=unmatched, ev=ev, state=state, q=q,
                           events=EMAIL_EVENTS, accounts=mailsync.accounts(), jobs_for_assign=jobs_for_assign,
                           last=json.loads(last["v"]) if last else None, last_at=last["at"] if last else None)


@app.post("/email/check")
def email_check():
    from . import mailsync
    if not mailsync.accounts():
        flash("Connect an email account first (Settings → Email inboxes).")
        return redirect(url_for("settings_page", section="email"))
    worker.resume()
    worker.submit("mail")
    flash("Checking your inboxes in the background. Updates appear here and on each job.")
    return redirect(request.referrer or url_for("email_page"))


@app.post("/email/<int:eid>/undo")
def email_undo(eid):
    from . import mailsync
    flash("Undone: the job is back to its previous status." if mailsync.undo(eid) else "Nothing to undo for that email.")
    return redirect(request.referrer or url_for("email_page"))


@app.post("/email/<int:eid>/assign")
def email_assign(eid):
    from . import mailsync
    job_id = request.form.get("job_id", "")
    flash("Matched and applied." if mailsync.assign(eid, job_id) else "Pick a job from the list.")
    return redirect(request.referrer or url_for("email_page"))


@app.post("/email/<int:eid>/ignore")
def email_ignore(eid):
    from . import mailsync
    mailsync.reclassify(eid, "not_application")
    return redirect(request.referrer or url_for("email_page"))


@app.post("/extension/<how>")
def extension_pairing(how):
    """Approve (or refuse) the Chrome extension that asked to connect."""
    from .ext_api import paired_extensions
    pend = json.loads((db.kv_get("ext_pending") or {}).get("v") or "null") or {}
    ext = request.form.get("id", "")
    if not ext or ext != pend.get("id") or how not in ("approve", "dismiss"):
        abort(400)
    if how == "approve":
        ids = paired_extensions() | {ext}
        db.kv_set("ext_paired", json.dumps(sorted(ids)))
        flash("Extension approved. Reopen it on the application page.")
    else:
        flash("Okay, that extension stays blocked.")
    db.kv_set("ext_pending", "null")
    return redirect(request.referrer or url_for("dashboard"))


@app.post("/email/rematch")
def email_rematch():
    """Try every unmatched email again from its whole text, in the background."""
    from . import mailsync
    def run():
        try:
            mailsync.rematch_unmatched(lambda m: print(m, flush=True))
        except Exception as e:
            print(f"re-matching: {e}", flush=True)
    threading.Thread(target=run, daemon=True, name="rematch").start()
    flash("Matching unmatched emails from their full text in the background. Refresh in a minute to see what's left.")
    return redirect(request.referrer or url_for("email_page", state="unmatched"))


@app.post("/email/<int:eid>/type")
def email_reclassify(eid):
    """Fix type: what this email really is. The job's status and Needs you follow."""
    from . import mailsync
    kind = request.form.get("event", "")
    row = mailsync.reclassify(eid, kind)
    if not row:
        flash("Pick what the email is from the list.")
    elif kind == "not_application":
        flash("Hidden: not about a job. Any change it made to the job was reversed.")
    elif row.get("rejected"):
        flash(f"Kept as {'an interview' if row.get('event') == 'interview' else 'an assessment'} and flagged Rejected; the job is marked rejected.")
    elif row.get("offer"):
        flash("Kept as an interview and flagged Offer 🎉; the job is marked as an offer.")
    else:
        label = EMAIL_EVENTS.get(kind, kind)
        flash(f"Now marked as {label.lower()}" + (f"; {row.get('company') or 'the job'} is updated." if row.get("state") == "applied" else "."))
    return redirect(request.referrer or url_for("email_page"))


@app.post("/settings/email/gmail")
def email_gmail_save():
    """Save a Gmail address + app password (the password goes to the OS keychain, never to project files)."""
    from . import mailsync
    if request.form.get("disconnect"):
        acc = mailsync.accounts()
        g = acc.pop("gmail", None)
        if g:
            mailsync.delete_secret(f"gmail:{g['address']}")
        mailsync.save_accounts(acc)
        flash("Gmail disconnected and its app password removed from the keychain.")
        return redirect(url_for("settings_page", section="email"))
    address = request.form.get("address", "").strip()
    pw = request.form.get("app_password", "").replace(" ", "")
    if not address or len(pw) < 16:
        flash("Enter your Gmail address and the 16-character app password.")
        return redirect(url_for("settings_page", section="email"))
    try:
        mailsync.gmail_test(address, pw)
    except Exception as e:
        flash(f"Gmail sign-in failed: {e}. Check the address, that 2-Step Verification is on, and the app password.")
        return redirect(url_for("settings_page", section="email"))
    mailsync.set_secret(f"gmail:{address}", pw)
    acc = mailsync.accounts()
    acc["gmail"] = {"address": address, "connected_at": db.now()}
    mailsync.save_accounts(acc)
    flash(f"Gmail connected ({address}), read-only. Click 'Check email now' on the Email page, or wait for the next check.")
    return redirect(url_for("settings_page", section="email"))


@app.post("/settings/email/microsoft")
def email_ms_start():
    from . import mailsync
    if request.form.get("disconnect"):
        acc = mailsync.accounts()
        acc.pop("microsoft", None)
        mailsync.save_accounts(acc)
        try:
            mailsync.MS_CACHE.unlink()
        except OSError:
            pass
        flash("Microsoft account disconnected and its sign-in removed.")
        return redirect(url_for("settings_page", section="email"))
    client_id = request.form.get("client_id", "").strip()
    address = request.form.get("address", "").strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", client_id):
        flash("Paste the Application (client) ID from your Azure app registration (36 characters).")
        return redirect(url_for("settings_page", section="email"))
    tenant = address.split("@")[-1].lower() if "@" in address else "organizations"   # your organization's directory
    try:
        mailsync.ms_start_signin(client_id, tenant)
    except Exception as e:
        flash(f"Could not start Microsoft sign-in: {e}")
    return redirect(url_for("settings_page", section="email", ms=client_id))


@app.route("/settings/email/applemail/accounts")
def email_applemail_accounts():
    from . import mailsync
    try:
        return jsonify({"ok": True, "accounts": mailsync.applemail_accounts()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.post("/settings/email/applemail")
def email_applemail_save():
    from . import mailsync
    acc = mailsync.accounts()
    if request.form.get("disconnect"):
        acc.pop("applemail", None)
        mailsync.save_accounts(acc)
        flash("Apple Mail disconnected.")
    else:
        name, address = request.form.get("name", "").strip(), request.form.get("address", "").strip()
        if not name:
            flash("Pick an account from the list.")
        else:
            acc["applemail"] = {"name": name, "address": address or name, "connected_at": db.now()}
            mailsync.save_accounts(acc)
            flash(f"Apple Mail connected ({address or name}), read-only. Click 'Check email now' on the Email page.")
    return redirect(url_for("settings_page", section="email"))


@app.route("/settings/email/microsoft/status")
def email_ms_status():
    from . import mailsync
    return jsonify(mailsync.ms_signin_state(request.args.get("client_id", "")))


@app.route("/outreach")
def outreach():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    where, params = ["1=1"], []
    if status:
        where.append("o.status=?")
        params.append(status)
    if q:
        where.append("(j.company LIKE ? OR j.title LIKE ? OR o.person_name LIKE ?)")
        params += [f"%{q}%"] * 3
    cond = " AND ".join(where)
    base = "FROM outreach o JOIN jobs j ON j.id=o.job_id WHERE " + cond
    total = db.fetchone("SELECT count(*) n " + base, tuple(params))["n"]
    page = Page(total, per_page=30)
    rows = db.fetchall(
        "SELECT o.*, j.company, j.title, j.status AS job_status " + base +
        " ORDER BY CASE o.status WHEN 'drafted' THEN 0 WHEN 'sent' THEN 1 WHEN 'accepted' THEN 2 ELSE 3 END, o.created_at DESC"
        " LIMIT ? OFFSET ?", (*params, page.per_page, page.offset))
    counts = {r["status"]: r["n"] for r in db.fetchall("SELECT status, count(*) n FROM outreach GROUP BY status")}
    return render_template("outreach.html", rows=rows, page=page, q=q, status=status, counts=counts)


@app.post("/outreach/<int:oid>")
def outreach_action(oid):
    o = db.fetchone("SELECT * FROM outreach WHERE id=?", (oid,)) or abort(404)
    status = request.form["status"]
    sent_at = o["sent_at"] or (db.now() if status == "sent" else None)
    db.execute("UPDATE outreach SET status=?, person_name=?, profile_url=?, message=?, sent_at=?, updated_at=? WHERE id=?",
               (status, request.form.get("person_name") or o["person_name"], request.form.get("profile_url") or o["profile_url"],
                request.form.get("message") or o["message"], sent_at, db.now(), oid))
    return redirect(request.referrer or url_for("outreach"))


@app.route("/sources", methods=["GET", "POST"])
def sources_page():
    if request.method == "POST" and request.files.get("csv"):
        for f in request.files.getlist("csv"):
            try:
                n = h1b.import_csv(f.read())
                flash(f"{f.filename}: imported {n:,} employers.")
            except (ValueError, UnicodeDecodeError) as e:
                flash(f"{f.filename}: import failed: {e}")
        return redirect(url_for("sources_page"))
    status = {r["key"]: r for r in db.fetchall("SELECT * FROM source_status")}
    rows = []
    for c in companies():
        if c.get("directory"):
            continue                      # thousands of boards: summarized in the directory card instead
        key = f"{c['ats']}:{c['slug']}"
        rows.append({**c, "status": status.get(key), "h1b_data": h1b.lookup(c["name"])})
    directory = {
        "by_ats": db.fetchall("SELECT ats, count(*) AS n, sum(enabled) AS on_ FROM discovered_boards WHERE source='directory' GROUP BY ats ORDER BY n DESC"),
        "errors": db.fetchone("SELECT count(*) AS n FROM source_status s JOIN discovered_boards b ON b.key=s.key "
                              "WHERE b.source='directory' AND s.ok=0")["n"],
        "refreshed": db.kv_get("directory_refreshed"),
        "sponsors": db.fetchone("SELECT count(*) AS n FROM sponsors")["n"],
    }
    n_h1b = db.fetchone("SELECT count(*) AS n FROM h1b_employers")["n"]
    feeds = db.fetchall("SELECT * FROM feed_status ORDER BY feed")
    boards = db.fetchall("SELECT * FROM discovered_boards WHERE source != 'directory' ORDER BY found_at DESC")
    runs = db.fetchall("SELECT * FROM runs ORDER BY id DESC LIMIT 15")
    return render_template("sources.html", rows=rows, n_h1b=n_h1b, runs=runs, feeds=feeds, boards=boards, directory=directory)


@app.post("/boards/<path:key>")
def toggle_board(key):
    db.execute("UPDATE discovered_boards SET enabled = 1 - enabled WHERE key=?", (key,))
    return redirect(url_for("sources_page"))


SOURCE_LABELS = {"greenhouse": "Company board", "lever": "Company board", "ashby": "Company board", "discovered": "Discovered board",
                 "jobright": "Jobright", "simplify": "Simplify", "hn": "HN startups", "web": "Added by you",
                 "workday": "Company board", "smartrecruiters": "Company board", "directory": "Board directory"}


@app.template_filter("smslink")
def smslink(phone: str | None, body: str = "") -> str:
    """Opens Messages with the text filled in (Mac and iPhone)."""
    from urllib.parse import quote
    digits = "".join(ch for ch in (phone or "") if ch.isdigit() or ch == "+")
    return f"sms:{digits}&body={quote(body or '')}" if digits else ""


@app.template_filter("source_label")
def source_label(s):
    return SOURCE_LABELS.get(s or "", s or "—")


@app.post("/resume")
def resume_now():
    flash("Resumed. If Claude is still at its limit, work pauses again on its own." if worker.resume() else "Nothing was paused.")
    return redirect(request.referrer or url_for("dashboard"))


@app.post("/run")
def run_now():
    kind = request.form.get("kind", "run")
    source = request.form.get("source", "manual")
    resumed = worker.resume()          # you clicked: try now, even if an earlier usage-limit pause is pending
    worker.submit(kind, source if kind in ("run", "process") else None)
    flash(("Resumed after the usage-limit pause. " if resumed else "") + "Pipeline started in the background. Watch the activity log.")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/api/map")
def map_json():
    """Pins for the dashboard maps. set = ready | today | all; show = applied,interviewing,rejected (any mix)."""
    from . import geo
    which = request.args.get("set", "ready")
    start_utc = datetime.combine(datetime.now().astimezone().date(), datetime.min.time()).astimezone().astimezone(timezone.utc).isoformat()
    # show = any of applied, interviewing (incl. offers), rejected; each chip on the map toggles one
    groups = {"applied": ["applied"], "interviewing": ["interviewing", "offer"], "rejected": ["closed"], "assessment": []}
    show = [g for g in request.args.get("show", "applied,interviewing").split(",") if g in groups]
    if request.args.get("rejected") == "1" and "rejected" not in show:      # older dashboards
        show.append("rejected")
    statuses = ["ready"] if which == "ready" else [st for g in show for st in groups[g]]
    with_assess = which != "ready" and "assessment" in show
    if not statuses and not with_assess:
        return jsonify({"pins": [], "total": 0, "unmapped": 0, "pending": 0})
    jobs = []
    if statuses:
        where = f"status IN ({','.join('?' * len(statuses))})"
        params: tuple = tuple(statuses)
        if which == "today":   # applied today, or moved forward / rejected today
            where += " AND (applied_at >= ? OR (status != 'applied' AND coalesce(status_changed_at, updated_at) >= ?))"
            params += (start_utc, start_utc)
        jobs = db.fetchall(f"SELECT id, company, title, location, status FROM jobs WHERE {where}", params)
    if with_assess:            # pending assessments are "now", so they show for Today and All time alike
        from . import assessments
        by_id = {j["id"]: j for j in jobs}
        for a in assessments.pending():
            note = assessments.due_label(a).replace(" (", ", ").rstrip(")")      # "due Sun Oct 25, 8:41 PM, in 5 days"
            if a.get("job_id"):
                j = by_id.get(a["job_id"]) or db.fetchone("SELECT id, company, title, location, status FROM jobs WHERE id=?", (a["job_id"],))
                if j:
                    j = {**j, "status": "assessment", "note": note}
                    by_id[j["id"]] = j
                    continue
            by_id[f"email:{a['id']}"] = {"id": f"email:{a['id']}", "company": a["shown_company"], "title": a.get("shown_role") or "Online assessment",
                                         "location": "", "status": "assessment", "note": note, "company_fallback": True}
        jobs = list(by_id.values())
    pending = geo.warm(jobs)
    pins, unmapped = geo.pins(jobs)
    return jsonify({"pins": pins, "total": len(jobs), "unmapped": len(unmapped), "unmapped_jobs": unmapped[:40], "pending": pending})


@app.route("/progress")
def progress_json():
    snap = progress.snapshot()
    from . import mailsync
    snap["mail"] = {k: v for k, v in mailsync.STATE.items()}
    if snap["mail"].get("active"):
        snap["mail"]["elapsed"] = int(__import__("time").time() - snap["mail"].get("started", 0))
    pu = worker.state.get("paused_until")
    snap["paused_until"] = datetime.fromtimestamp(pu).strftime("%I:%M %p").lstrip("0") if pu else None
    return jsonify(snap)


def _schedule_info() -> dict:
    """What launchd and pmset say about the daily runs and lid-closed wakes."""
    import plistlib
    from datetime import date
    info = {"installed": False, "times": [], "next": None, "wake": False, "wake_next": None, "mac": sys.platform == "darwin"}
    plist = Path.home() / "Library/LaunchAgents/com.jobautopilot.daily.plist"
    if not info["mac"]:                    # Windows / Linux: the dashboard's built-in clock starts the runs
        from . import schedule
        info.update(installed=True, times=[_ampm(t) for t in schedule.load_times()])
        nxt = schedule.next_runs(1)
        info["next"] = strf(nxt[0], "%a %-I:%M %p") if nxt else None
        return info
    if plist.exists():
        try:
            d = plistlib.loads(plist.read_bytes())
            times = sorted((x["Hour"], x.get("Minute", 0)) for x in d.get("StartCalendarInterval", []))
            info["installed"] = True
            info["times"] = [strf(datetime(2000, 1, 1, h, m), "%-I:%M %p") for h, m in times]
            now = datetime.now()
            upcoming = [datetime.combine(date.today() + timedelta(days=dd), datetime.min.time()).replace(hour=h, minute=m)
                        for dd in (0, 1) for h, m in times]
            nxt = next((t for t in upcoming if t > now), None)
            info["next"] = strf(nxt, "%a %-I:%M %p") if nxt else None
        except Exception:
            pass
    try:
        sched = subprocess.run(["pmset", "-g", "sched"], capture_output=True, text=True, timeout=5).stdout
        wakes = [ln for ln in sched.splitlines() if "jobautopilot" in ln]
        info["wake"] = bool(wakes)
        if wakes:
            info["wake_next"] = wakes[0].split("wake at ")[-1].split(" by")[0]
    except Exception:
        pass
    return info


@app.route("/background")
def background():
    runs_page = Page(db.count("runs"), per_page=8, param="runs_page", anchor="recent-runs")
    runs = db.fetchall("SELECT id, started_at, finished_at, status, source, stats_json FROM runs ORDER BY id DESC LIMIT ? OFFSET ?",
                       (runs_page.per_page, runs_page.offset))
    for r in runs:
        try:
            st = __import__("json").loads(r["stats_json"] or "{}")
        except ValueError:
            st = {}
        f = st.get("fetch") or {}
        r["summary"] = {"fetched": f.get("fetched"), "new": (f.get("new") or 0) + ((f.get("feeds") or {}).get("new") or 0),
                        "screened": st.get("screened"), "docs": st.get("queued_for_documents", st.get("tailored")),
                        "error": st.get("error")}
        if r["started_at"] and r["finished_at"]:
            secs = (datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds()
            r["duration"] = f"{int(secs // 60)}m {int(secs % 60)}s"
    sched_log = ""
    p = DATA_DIR / "scheduled.log"
    if p.exists():
        sched_log = "\n".join(p.read_text(errors="replace").splitlines()[-12:])
    return render_template("background.html", runs=runs, runs_page=runs_page, schedule=_schedule_info(), sched_log=sched_log)


# ----------------------------------------------------------------------------- people (referrals)

PERSON_FIELDS = ("name", "company", "title", "connection", "phone", "email", "linkedin", "stage", "notes", "last_contacted")


@app.route("/people", methods=["GET", "POST"])
def people():
    from . import contacts
    if request.method == "POST" and request.files.get("xlsx"):
        try:
            res = contacts.import_xlsx(request.files["xlsx"].read())
            flash(f"Imported: {res['added']} new, {res['updated']} updated."
                  + (f" Cleaned up: {'; '.join(res['changes'][:6])}{'…' if len(res['changes']) > 6 else ''}" if res["changes"] else ""))
            _requeue_referrals()
        except Exception as e:
            flash(f"Import failed: {e}")
        return redirect(url_for("people"))
    q, stage, company = request.args.get("q", "").strip(), request.args.get("stage", ""), request.args.get("company", "")
    where, params = ["1=1"], []
    if q:
        where.append("(name LIKE ? OR company LIKE ? OR title LIKE ? OR connection LIKE ? OR notes LIKE ? OR phone LIKE ?)")
        params += [f"%{q}%"] * 6
    if stage:
        where.append("stage=?")
        params.append(stage)
    if company:
        where.append("company=?")
        params.append(company)
    cond = " AND ".join(where)
    page = Page(db.count("contacts", cond, tuple(params)), per_page=25)
    order = "CASE stage WHEN 'will_refer' THEN 0 WHEN 'warm' THEN 1 WHEN 'unknown' THEN 2 WHEN 'no_reply' THEN 3 WHEN 'student' THEN 4 ELSE 5 END, name COLLATE NOCASE"
    rows = db.fetchall(f"SELECT * FROM contacts WHERE {cond} ORDER BY {order} LIMIT ? OFFSET ?", (*params, page.per_page, page.offset))
    for r in rows:
        r["links"] = json.loads(r.get("job_links") or "[]")
        r["open_jobs"] = _jobs_at(r["company"]) if r.get("company") else []
    stage_counts = {x["stage"]: x["n"] for x in db.fetchall("SELECT stage, count(*) n FROM contacts GROUP BY stage")}
    companies = [x["company"] for x in db.fetchall("SELECT DISTINCT company FROM contacts WHERE company IS NOT NULL ORDER BY company COLLATE NOCASE")]
    return render_template("people.html", rows=rows, page=page, q=q, stage=stage, company=company,
                           stages=contacts.STAGES, stage_counts=stage_counts, companies=companies)


def _jobs_at(company: str) -> list[dict]:
    from .h1b import norm
    key = norm(company)
    if not key:
        return []
    out = []
    for j in db.fetchall("SELECT id, company, title, status FROM jobs WHERE status IN ('ready','tailoring','to_tailor','applied','below_threshold')"):
        k = norm(j["company"])
        if k == key or k.startswith(key + " ") or key.startswith(k + " "):
            out.append(j)
    return out


def _requeue_referrals() -> None:
    """New or edited contacts: draft referral texts for jobs at their companies that are ready to submit."""
    from . import contacts
    for j in db.fetchall("SELECT id, company FROM jobs WHERE status='ready'"):
        if contacts.contacts_for_company(j["company"]):
            worker.submit("referrals", j["id"])


@app.route("/people/new", methods=["GET", "POST"])
@app.route("/people/<int:pid>/edit", methods=["GET", "POST"])
def person_form(pid: int | None = None):
    from . import contacts
    from .h1b import norm
    person = db.fetchone("SELECT * FROM contacts WHERE id=?", (pid,)) if pid else {}
    if pid and not person:
        abort(404)
    if request.method == "POST":
        rec = {k: (request.form.get(k) or "").strip() or None for k in PERSON_FIELDS}
        if not rec["name"]:
            flash("A name is required.")
            return render_template("person_form.html", person={**person, **rec}, stages=contacts.STAGES)
        company, _ = contacts.clean_company(rec["company"])
        rec["company"] = company
        rec["company_key"] = norm(company) if company else None
        rec["phone"] = contacts.clean_phone(rec["phone"]) if rec["phone"] else None
        rec["stage"] = rec["stage"] if rec["stage"] in contacts.STAGES else "unknown"
        links = [u.strip() for u in (request.form.get("job_links") or "").splitlines() if u.strip()]
        rec["job_links"] = json.dumps(links)
        if pid:
            db.execute(f"UPDATE contacts SET {','.join(f'{k}=?' for k in rec)}, updated_at=? WHERE id=?", (*rec.values(), db.now(), pid))
            flash(f"Saved {rec['name']}.")
        else:
            db.execute(f"INSERT INTO contacts ({','.join(rec)}, source, created_at, updated_at) VALUES ({','.join('?' * len(rec))}, 'manual', ?, ?)",
                       (*rec.values(), db.now(), db.now()))
            flash(f"Added {rec['name']}." + (f" Referral texts will be drafted for ready jobs at {company}." if company else ""))
        _requeue_referrals()
        return redirect(url_for("people", q=rec["name"]))
    if person:
        person = dict(person)
        person["job_links_text"] = "\n".join(json.loads(person.get("job_links") or "[]"))
    return render_template("person_form.html", person=person, stages=contacts.STAGES)


@app.post("/people/<int:pid>/delete")
def person_delete(pid: int):
    p = db.fetchone("SELECT name FROM contacts WHERE id=?", (pid,)) or abort(404)
    for a in db.fetchall("SELECT job_id FROM referral_asks WHERE contact_id=?", (pid,)):
        db.execute("UPDATE interventions SET status='dismissed', resolved_at=? WHERE job_id=? AND kind='referral' AND title LIKE ? AND status='open'",
                   (db.now(), a["job_id"], f"Ask {p['name']} (%"))
    db.execute("DELETE FROM referral_asks WHERE contact_id=?", (pid,))
    db.execute("DELETE FROM contacts WHERE id=?", (pid,))
    flash(f"Deleted {p['name']}.")
    return redirect(request.referrer or url_for("people"))


@app.post("/referrals/<int:rid>")
def referral_action(rid: int):
    a = db.fetchone("SELECT a.*, c.name FROM referral_asks a JOIN contacts c ON c.id=a.contact_id WHERE a.id=?", (rid,)) or abort(404)
    status = request.form.get("status", "")
    if status in ("asked", "referred", "declined", "skipped", "drafted"):
        db.execute("UPDATE referral_asks SET status=?, updated_at=?, asked_at=COALESCE(asked_at, CASE WHEN ?='asked' THEN ? END) WHERE id=?",
                   (status, db.now(), status, db.now(), rid))
        if status != "drafted":
            db.execute("UPDATE interventions SET status='resolved', resolution=?, resolved_at=? WHERE job_id=? AND kind='referral' "
                       "AND title LIKE ? AND status='open'", (f"referral {status}", db.now(), a["job_id"], f"Ask {a['name']} (%"))
            if status == "asked":
                db.execute("UPDATE contacts SET last_contacted=?, updated_at=? WHERE id=?", (date.today().isoformat(), db.now(), a["contact_id"]))
            if status == "referred":
                db.execute("UPDATE contacts SET stage='will_refer' WHERE id=?", (a["contact_id"],))
    if request.form.get("message"):
        db.execute("UPDATE referral_asks SET message=?, updated_at=? WHERE id=?", (request.form["message"], db.now(), rid))
    flash({"asked": f"Marked as asked. Give {a['name'].split()[0]} a day or two before you submit, if you can.",
           "referred": f"Great: {a['name'].split()[0]} referred you. Submit the application now.",
           "declined": "Noted.", "skipped": "Skipped this referral."}.get(status, "Saved."))
    return redirect(request.referrer or url_for("inbox"))


@app.post("/jobs/<path:job_id>/referrals")
def job_referrals(job_id):
    db.get_job(job_id) or abort(404)
    worker.submit("referrals", job_id)
    flash("Drafting referral texts in the background; refresh in a few seconds.")
    return redirect(url_for("job_detail", job_id=job_id))


# ----------------------------------------------------------------------------- settings

TUNABLES = [
    ("MIN_MATCH_FOR_DOCS", "Minimum match to build documents", "int", "Resumes and cover letters are built only for jobs Claude scores at or above this (out of 10). Lower-scored jobs wait under 'Below your threshold', where you can still build one by hand.", (1, 10)),
    # key, label, kind, help, choices/range
    ("MIN_SALARY", "Minimum pay (USD per year)", "int", "Jobs whose listed pay tops out below this are skipped before Claude sees them (hourly pay counts as 2,080 hours a year). Jobs that list no pay are kept. 0 turns it off.", (0, 500000)),
    ("POSTED_WITHIN_HOURS", "Look back (hours)", "int", "Only postings newer than this are fetched each run.", (1, 336)),
    ("FIRST_RUN_CATCHUP", "Catch up on the first run of the day", "bool", "The day's first run looks back a day further, and at least to the last run, so postings that went up overnight or while the computer was off aren't missed (up to 7 days).", None),
    ("MAX_SCREEN_PER_RUN", "Screens per batch", "int", "How many new jobs Claude scores per batch. Batches keep going on their own until none are left.", (1, 300)),
    ("USAGE_PAUSE_AT", "Pause at Claude usage (%)", "int", "Background work pauses when your Claude session or weekly limit reaches this, and resumes after it resets. 100 turns the check off.", (50, 100)),
    ("MAX_TAILOR_PER_RUN", "Documents per run", "int", "How many tailored resume + cover letter packages are built per run.", (0, 100)),
    ("PARALLEL_JOBS", "Jobs at once", "int", "Screens and documents built in parallel. Higher is faster but uses your Claude limit faster. Applies after a restart.", (1, 6)),
    ("MAX_REVISIONS", "Revision rounds", "int", "How many times Claude may fix lines that fail the checks.", (0, 4)),
    ("EFFORT_TAILOR", "Resume writing effort", "choice", "Higher is slower but more careful.", ("low", "medium", "high")),
    ("EFFORT_SCREEN", "Screening effort", "choice", "Fit verdicts are short; low is usually enough.", ("low", "medium", "high")),
    ("AUTO_TAILOR_RISK", "Build documents for 'apply with risk' roles", "bool", "Off = those roles wait for your OK on Needs you.", None),
    ("SPONSOR_WEB_CHECKS", "Online sponsor lookups per run", "int", "For companies missing from your USCIS data, Claude searches public H-1B disclosure data (DOL LCAs, USCIS approvals) once per company and caches the answer for 90 days. Each lookup is one small Claude call with web search. 0 turns it off.", (0, 200)),
    ("MAIL_CHECK_MINUTES", "Check email every (minutes)", "int", "How often connected inboxes are read for application updates (Settings → Email inboxes). 0 = only when you click 'Check email now'.", (0, 1440)),
    ("MAIL_LOOKBACK_DAYS", "Email look-back on first check (days)", "int", "How far back the first check of a newly connected inbox reads.", (1, 90)),
    ("SCREEN_UNKNOWN_SPONSOR", "Screen jobs when sponsorship is unknown", "bool", "On = jobs from companies with no H-1B signal yet still go to Claude (its visa gate judges the posting); uses more screens. Off = they wait under Filtered out. Importing the USCIS H-1B data on Sources covers most companies either way.", None),
]
DEFAULTS = {"USAGE_PAUSE_AT": 95, "MIN_MATCH_FOR_DOCS": 7, "MIN_SALARY": 100000, "POSTED_WITHIN_HOURS": 24, "FIRST_RUN_CATCHUP": "1", "MAX_SCREEN_PER_RUN": 60, "MAX_TAILOR_PER_RUN": 15, "PARALLEL_JOBS": 3,
            "MAX_REVISIONS": 2, "EFFORT_TAILOR": "medium", "EFFORT_SCREEN": "low", "AUTO_TAILOR_RISK": "1", "SCREEN_UNKNOWN_SPONSOR": "0", "SPONSOR_WEB_CHECKS": 25,
            "MAIL_CHECK_MINUTES": 60, "MAIL_LOOKBACK_DAYS": 14}


SETTINGS_SECTIONS = [   # (key, title, one-line description): each is its own page under /settings/<key>
    ("schedule", "Schedule", "When runs start, and waking the computer for them with the lid closed."),
    ("pipeline", "Pipeline and limits", "Match threshold, pay floor, batch sizes, Claude usage limit, job feeds, and the H-1B filter."),
    ("email", "Email inboxes", "Read application replies from Gmail, Outlook / Microsoft 365, or Apple Mail (read-only)."),
    ("online", "Online dashboard", "Check progress from your phone and start runs from anywhere."),
    ("updates", "App updates", "Your version, and one-click updates from GitHub."),
    ("start", "Getting started", "The guided tour, your name, and your profile files."),
]


@app.route("/settings", methods=["GET", "POST"])
def settings_root():
    """/settings opens the first section (307 keeps a POST from older pages a POST)."""
    args = request.args.to_dict()
    sec = args.pop("section", "schedule")
    if sec not in {k for k, _, _ in SETTINGS_SECTIONS}:
        sec = "schedule"
    return redirect(url_for("settings_page", section=sec, **args), code=307)


@app.route("/settings/<section>", methods=["GET", "POST"])
def settings_page(section="schedule"):
    if section not in {k for k, _, _ in SETTINGS_SECTIONS}:
        abort(404)
    from . import schedule
    from .config import CONFIG_DIR, feeds_config, save_settings
    if request.method == "POST":
        section = request.form.get("section") or section
        try:
            if section == "schedule":
                times = schedule.save_times([t for t in request.form.getlist("time") if t.strip()])
                info = schedule.status()
                msg = f"Saved. Runs every day at {', '.join(_ampm(t) for t in times)}."
                if info["wake_installed"] and info["wake_follows_settings"]:
                    msg += " Lid-closed wake-ups moved to 2 minutes before each."
                flash(msg)
            elif section == "pipeline":
                updates = {}
                for key, _, kind, _, rng in TUNABLES:
                    if kind == "bool":
                        updates[key] = "1" if request.form.get(key) else "0"
                    elif kind == "int":
                        v = int(request.form.get(key) or DEFAULTS[key])
                        updates[key] = max(rng[0], min(rng[1], v))
                    else:
                        v = request.form.get(key, DEFAULTS[key])
                        updates[key] = v if v in rng else DEFAULTS[key]
                save_settings(updates)
                from .pipeline import hold_below_threshold, release_above_threshold
                held, released = hold_below_threshold(), release_above_threshold()
                for j in db.fetchall("SELECT id FROM jobs WHERE status='to_tailor'"):
                    worker.submit("tailor", j["id"])
                if held or released:
                    flash(f"Match threshold is now {updates['MIN_MATCH_FOR_DOCS']}/10: {held} queued job(s) held back, {released} released to build.")
                fc = feeds_config()
                fc["require_h1b_signal"] = bool(request.form.get("require_h1b_signal"))
                for feed in ("jobright", "simplify", "hn_who_is_hiring"):
                    fc.setdefault(feed, {})["enabled"] = bool(request.form.get(f"feed_{feed}"))
                (CONFIG_DIR / "feeds.json").write_text(__import__("json").dumps(fc, indent=2, ensure_ascii=False), encoding="utf-8")
                flash("Saved. The next run uses these settings.")
        except ValueError as e:
            flash(f"Not saved: {e}")
        return redirect(url_for("settings_page", section=section if section in ("schedule", "pipeline") else "schedule"))
    current = {k: env(k, str(DEFAULTS[k])) for k, *_ in TUNABLES}
    from . import mailsync
    ms_id = request.args.get("ms", "")
    from . import cloud, greeting
    title, help_text = next((t, h) for k, t, h in SETTINGS_SECTIONS if k == section)
    return render_template("settings.html", section=section, section_title=title, section_help=help_text,
                           settings_sections=SETTINGS_SECTIONS, user_name=greeting.user_name(), cloud_cfg=cloud.config(), cloud_state=cloud.state(),
                           cloud_status=cloud.STATUS, cloud_connected=cloud.connected(), upd_state=__import__('app.updater', fromlist=['STATE']).STATE, cloud_default_name=cloud.default_machine_name(), cloud_pending=cloud.pending_signup(), cloud_email=_profile_email(), cloud_city=cloud.city() if cloud.connected() else cloud.default_city(),
                           remote_wake=schedule.remote_wake(), mail_accounts=mailsync.accounts(), ms_client=ms_id, is_mac=sys.platform == "darwin",
                           ms_state=mailsync.ms_signin_state(ms_id) if ms_id else None, sched=schedule.status(), tunables=TUNABLES, current=current,
                           feeds=feeds_config(), ampm=_ampm)


def _ampm(t: str) -> str:
    h, m = (int(x) for x in t.split(":"))
    return strf(datetime(2000, 1, 1, h, m), "%-I:%M %p")


# ----------------------------------------------------------------------------- online dashboard (phone)

@app.before_request
def _same_origin_posts():
    """Block cross-site form posts: a web page you visit must not be able to press buttons on this local
    dashboard. Browsers send Origin on POSTs; only this dashboard and the Chrome extension may post."""
    if request.method != "POST":
        return None
    origin = request.headers.get("Origin") or ""
    if not origin:
        return None
    if origin.startswith("chrome-extension://"):
        if request.path.startswith("/api/ext/"):
            return None                          # the extension API checks that it's the extension you approved
        abort(403)
    from urllib.parse import urlparse
    host = urlparse(origin).netloc
    if host in (request.host, f"127.0.0.1:{request.host.split(':')[-1]}", f"localhost:{request.host.split(':')[-1]}"):
        return None
    abort(403)


@app.post("/settings/cloud")
def cloud_connect():
    from . import cloud
    if request.form.get("disconnect"):
        force = request.form.get("force") == "1"
        try:
            cloud.disconnect(force=force)
        except RuntimeError as e:
            flash(f"{e} (Or disconnect anyway: its data stays online until you delete your account on the site.)")
            return redirect(url_for("settings_page", section="online"))
        flash("This computer is disconnected from the online dashboard" +
              (" (its data online couldn't be deleted; delete your account on the site to remove it)." if force
               else ", and its data there was removed."))
        return redirect(url_for("settings_page", section="online"))
    email, password = request.form.get("email", "").strip(), request.form.get("password", "")
    name = (request.form.get("machine_name") or cloud.default_machine_name()).strip()
    try:
        cloud.connect(email, password, name, request.form.get("city", ""))
        worker.submit("cloud")
        flash(f"Connected as {email}. Progress shows up on the online dashboard within a minute.")
    except Exception as e:
        flash(f"Couldn't connect: {e}")
    return redirect(url_for("settings_page", section="online"))


@app.post("/settings/cloud/signup")
def cloud_signup():
    """Create the online-dashboard account from here (the password goes to Supabase and isn't stored)."""
    from . import cloud
    f = request.form
    if not f.get("privacy"):
        flash("Please read and accept the privacy note first.")
        return redirect(url_for("settings_page", section="online"))
    if f.get("password", "") != f.get("password2", ""):
        flash("The passwords don't match.")
        return redirect(url_for("settings_page", section="online"))
    try:
        p = cloud.signup(f.get("name", ""), f.get("email", ""), f.get("password", ""),
                         (f.get("machine_name") or cloud.default_machine_name()).strip(), f.get("city", ""), bool(f.get("optin")))
        flash(f"We emailed a 6-digit code to {p['email']}. Enter it below to finish.")
    except Exception as e:
        flash(f"Couldn't create the account: {e}")
    return redirect(url_for("settings_page", section="online"))


@app.post("/settings/cloud/verify")
def cloud_verify():
    from . import cloud
    try:
        st = cloud.verify(request.form.get("code", ""))
        worker.submit("cloud")
        cfg = cloud.config()
        flash(f"🎉 You're all set, {st['email']}. This computer is connected. From now on just sign in at "
              f"{cfg.get('site_url') or 'the online dashboard'} on your phone.")
    except Exception as e:
        flash(str(e))
    return redirect(url_for("settings_page", section="online"))


@app.post("/settings/cloud/resend")
def cloud_resend():
    from . import cloud
    try:
        cloud.resend_code()
        flash("Sent a new code. Check your inbox (and spam).")
    except Exception as e:
        flash(str(e))
    return redirect(url_for("settings_page", section="online"))


@app.post("/settings/cloud/cancel")
def cloud_cancel():
    from . import cloud
    cloud.cancel_signup()
    return redirect(url_for("settings_page", section="online"))


@app.post("/cloud/prompt/dismiss")
def cloud_prompt_dismiss():
    db.kv_set("cloud_prompt", "dismissed")
    return redirect(url_for("dashboard"))


@app.post("/settings/cloud/wake")
def cloud_wake():
    from . import schedule
    minutes = request.form.get("minutes", "0")
    minutes = minutes if minutes in ("0", "15", "30", "60") else "0"
    schedule.set_remote_wake(int(minutes))
    flash("Saved. " + ("Your Mac will check for phone requests every " + minutes + " minutes while plugged in."
                       if minutes not in ("", "0") else "Phone requests run when your Mac is awake."))
    return redirect(url_for("settings_page", section="online"))


@app.post("/settings/cloud/city")
def cloud_city():
    from . import cloud
    saved = cloud.set_city(request.form.get("city", ""))
    flash(f"Saved. The site operator sees your city as {saved}." if saved else "Saved. Your city is private now.")
    return redirect(url_for("settings_page", section="online"))


PIPELINE_LABELS = [("to_screen", "Waiting for screen"), ("needs_decision", "Their call"), ("to_tailor", "Queued for documents"),
                   ("tailoring", "Building documents"), ("ready", "Ready to submit"), ("applied", "Applied"),
                   ("interviewing", "Interviewing"), ("offer", "Offer"), ("closed", "Rejected")]


def _num(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _vtuple(v: str | None) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def _as_iso(v) -> str | None:
    """A time from a computer's snapshot as ISO text: some send a Unix timestamp (the usage-limit pause)."""
    if v in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(float(v), timezone.utc).isoformat(timespec="minutes")
    except (TypeError, ValueError):
        return str(v)


def user_detail(r: dict) -> dict:
    """Everything the operator may see for one person, totalled across their computers (counts only)."""
    from .version import VERSION
    machines = [m for m in (r.get("machines") or []) if isinstance(m, dict)]
    k, pipe, fams, days = {}, {}, {}, {}
    for m in machines:
        for key, v in (m.get("kpis") or {}).items():
            k[key] = k.get(key, 0) + _num(v)
        for key, v in (m.get("pipeline") or {}).items():
            pipe[key] = pipe.get(key, 0) + _num(v)
        for f in m.get("families") or []:
            fam = f.get("fam") or "Other"
            fams[fam] = fams.get(fam, 0) + _num(f.get("n"))
        for e in m.get("series") or []:
            if e.get("day"):
                days[e["day"]] = days.get(e["day"], 0) + _num(e.get("n"))
        m["outdated"] = bool(m.get("app_version")) and _vtuple(m.get("app_version")) < _vtuple(VERSION)
    fam_total = sum(fams.values())
    series = [{"day": d, "n": n} for d, n in sorted(days.items())][-14:]
    peak = max([e["n"] for e in series] + [1])
    for e in series:
        e["h"] = round(e["n"] / peak * 100)
        try:
            e["label"] = strf(datetime.fromisoformat(e["day"]), "%a %b %-d")
        except ValueError:
            e["label"] = e["day"]
    runs = [m["last_run"] for m in machines if isinstance(m.get("last_run"), dict) and m["last_run"].get("started_at")]
    nexts = sorted(m["next_run"] for m in machines if m.get("next_run"))
    hours = sum(float(m.get("time_saved_hours") or 0) for m in machines)
    return {
        "tiles": [("Applied today", k.get("applied_today", 0)), ("This week", r.get("applied_week") or 0),
                  ("Last 14 days", r.get("applied_14d") or 0), ("All time", r.get("applied_total") or 0),
                  ("Ready to submit", k.get("ready", 0)), ("Waiting on their call", k.get("decide", 0)),
                  ("Interviews (ever)", k.get("interviews", k.get("interviewing", 0))), ("Offers", k.get("offers", 0)),
                  ("Rejected", k.get("rejected", 0)),
                  ("Assessments to finish", k.get("assessments", 0)),
                  ("Items in Needs you", sum(_num(m.get("open_items")) for m in machines)),
                  ("New roles today", k.get("posted_today", 0)), ("Hours saved", round(hours))],
        "pipeline": [(label, pipe.get(key, 0)) for key, label in PIPELINE_LABELS],
        "pipe_max": max(list(pipe.values()) + [1]),
        "families": [{"fam": f, "n": n, "pct": round(n / fam_total * 100) if fam_total else 0}
                     for f, n in sorted(fams.items(), key=lambda x: (-x[1], x[0]))],
        "series": series,
        "last_run": max(runs, key=lambda x: x["started_at"]) if runs else None,
        "next_run": nexts[0] if nexts else None,
        "queued": sum(_num(m.get("queued")) for m in machines),
        "paused_until": _as_iso(next((m["paused_until"] for m in machines if m.get("paused_until")), None)),
        "machines": machines,
    }


@app.get("/users")
def users_page():
    """Site operator only: everyone using the online dashboard, with application counts (never companies)."""
    from . import cloud
    rows = cloud.operator_users() if cloud.connected() else None
    if rows is None:
        abort(404)
    for r in rows:
        # This week = Sunday to Saturday on each person's computer (its own count), not the last 7 days. Computers
        # on versions too old to send it keep the database's 7-day figure.
        weeks = [(m.get("kpis") or {}).get("applied_week") for m in (r.get("machines") or []) if isinstance(m, dict)]
        if any(w is not None for w in weeks):
            r["applied_week"] = sum(int(w or 0) for w in weeks)
        r["active"] = (r.get("applied_14d") or 0) > 0
        r["detail"] = d = user_detail(r)
        r["display"] = r.get("name") or (r.get("email") or "?").split("@")[0]
        r["initials"] = "".join(p[0] for p in r["display"].split()[:2]).upper() or "?"
        r["status"] = "active" if r["active"] else ("inactive" if r.get("computers") else "setup")
        versions = [m.get("app_version") for m in d["machines"] if m.get("app_version")]
        r["version"] = max(versions, key=_vtuple) if versions else None
        r["outdated"] = any(m.get("outdated") for m in d["machines"])
        old_v = [m.get("app_version") for m in d["machines"] if m.get("outdated") and m.get("app_version")]
        r["old_version"] = min(old_v, key=_vtuple) if old_v else None
    order = {"active": 0, "inactive": 1, "setup": 2}
    rows.sort(key=lambda r: (order[r["status"]], -(r.get("applied_week") or 0), -(r.get("applied_total") or 0)))
    stats = {"users": len(rows), "active": sum(r["active"] for r in rows),
             "setup": sum(r["status"] == "setup" for r in rows), "outdated": sum(r["outdated"] for r in rows),
             "week": sum(r.get("applied_week") or 0 for r in rows), "total": sum(r.get("applied_total") or 0 for r in rows)}
    nudge = [r for r in rows if r["status"] == "setup" or r["outdated"]]
    from .version import VERSION
    return render_template("users.html", rows=rows, stats=stats, nudge=nudge, latest=VERSION)


@app.post("/cloud/poll")
def cloud_poll():
    """Called by the wake-up job on this Mac (localhost only): sync now and report queued work."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403)
    from . import cloud
    try:
        return jsonify(cloud.sync_once(full=True))
    except Exception as e:
        return jsonify({"queued": 0, "error": str(e)[:200]})


@app.post("/settings/name")
def set_user_name():
    """Your name for greetings (saved to profile/applicant.json, which stays on your computer)."""
    from . import greeting
    name = re.sub(r"\s+", " ", request.form.get("name", "")).strip()[:60]
    if name:
        greeting.set_name(name)
        flash(f"Nice to meet you, {name.split()[0]}!")
    return redirect(request.referrer or url_for("dashboard"))


# ----------------------------------------------------------------------------- first-run tour

def tour_steps(name: str | None) -> list[dict]:
    """The walkthrough shown once to new users (app/static/tour.js). It visits every page: `page` is where a step
    lives (the tour moves there on its own) and `target` is a [data-tour] element on it; no target = a centered card."""
    hi = f", {name.split()[0]}" if name else ""
    home, inbox, jobs = url_for("dashboard"), url_for("inbox"), url_for("jobs")
    S = lambda sec: url_for("settings_page", section=sec)
    steps = [
        (home, None, f"Welcome to Job Autopilot{hi} 👋",
         "A short walk through every page: how it finds jobs, writes your documents, and keeps track of everything. "
         "It moves from page to page on its own. Skip anytime; you can replay it from Settings."),
        (home, "run", "Run now",
         "Finds new postings that match your search, has Claude screen each one against your profile, and writes a tailored "
         "one-page resume and cover letter for the good matches. Scheduled runs do this on their own, even while you sleep."),
        (home, "kpis", "Your numbers at a glance",
         "New roles today, documents ready to send, jobs waiting for your call, what you've applied to, interviews, and "
         "assessments to finish. Click a tile to see those jobs."),
        (home, "background", "Watch the work happen",
         "Live progress while jobs are screened and documents are written. If you get close to your Claude usage limit, "
         "background work pauses on its own and picks up after the reset."),
        (home, "week", "Your week, day by day",
         "Applications for each day from Sunday to Saturday. Use the arrows or the menu to look at earlier weeks."),
        (home, "saved", "Time saved",
         "An estimate of the hours you didn't spend reading job descriptions, writing documents, filling forms, and sorting emails."),
        (home, "maps", "Where your applications are",
         "One map shows jobs ready to submit; the other shows what you've applied to, with interviews, rejections, and "
         "assessments. Click a pin to see its jobs."),
        (inbox, "needs", "Needs you: your to-do list",
         "The few things only you can do: ask for a referral, decide on a borderline match, answer a fact only you know, "
         "or review documents before you submit. Answer once and it's remembered."),
        (jobs, "job-tabs", "Every job it found",
         "Tabs sort jobs by where they are: ready to submit, in progress, below your threshold, or skipped and why. "
         "Open a job for its resume, cover letter, match score, and Claude's reasons."),
        (jobs, "job-filters", "Find anything",
         "Search by company or title, and filter by source, match score, or when you applied."),
        (jobs, None, "Apply with the Chrome extension",
         "Open a job's application page and click the Job Autopilot extension: it fills the form from your profile and attaches "
         "your documents. You review everything and click Submit yourself. Nothing is ever sent without you."),
        (url_for("jobs", status="applied_all"), "job-tabs", "Track what you sent",
         "Everything you've applied to, filterable by date (today, yesterday, this week), with one click to copy all the links."),
        (url_for("assessments_page"), "assess", "Online assessments",
         "Assessments from your inbox, with their exact due times, a reminder every morning, and another 3 hours before each one is due."),
        (url_for("email_page"), "email-tabs", "Replies, sorted for you",
         "Once your inbox is connected (read-only), rejections, interviews, assessments, and offers update your jobs on their own."),
        (url_for("people"), "people", "People and referrals",
         "Add people you know. When a job is at their company, Needs you suggests asking them for a referral, with the message already written."),
        (url_for("profile_page"), "profile-files", "Your profile drives everything",
         "Your master prompt (who you are and how to write about you), confirmed facts, and form presets. Claude never "
         "invents experience, so the more accurate this is, the better your documents. Earlier versions are kept."),
        (url_for("resume_template_page"), "templates", "Pick a resume template",
         "Four ATS-friendly layouts. Preview each one with your own resume, then pick the one that suits your profile."),
        (url_for("sources_page"), "sources", "Where jobs come from",
         "About 4,000 company job boards and job feeds, plus the USCIS H-1B sponsor data that decides which companies are worth a look."),
        (S("schedule"), "schedule", "When it runs",
         "Set your daily run times. The computer can wake itself up for a run, so it works overnight."),
        (S("pipeline"), "pipeline", "How picky it is",
         "Your match threshold for building documents, minimum pay, how many jobs to handle per run, and how much Claude effort to use."),
        (S("email"), "inboxes", "Connect your email",
         "Gmail, Outlook, or Apple Mail, read-only. This is what keeps your jobs and assessments up to date."),
        (S("online"), "online", "Check in from your phone",
         "The optional online dashboard shows your progress on any phone or tablet and can start a run on this computer."),
        ("", "search", "Search the app",
         "Press ⌘K (Ctrl+K on Windows) to jump to any page or a single setting, such as \"minimum pay\" or \"Gmail\"."),
        (url_for("how_it_works"), "how", "The whole picture",
         "This page explains the six steps in plain words, with answers to common questions. It's always in the sidebar."),
    ]
    out = [{"page": page, "title": title, "text": text, **({"target": f'[data-tour="{t}"]'} if t else {})}
           for page, t, title, text in steps]
    out.append({"page": home, "title": "You're all set 🚀", "cta": "Let's go",
                "text": "Click Run now to start your first search. It takes a few minutes and you can watch it here. "
                        "Replay this tour anytime from Settings → Getting started."})
    return out


# What's new, shown as a walkthrough right after an update: steps for every version since the one you came from.
# (page, data-tour target or None, title, text). Add steps here with each release that adds something visible.
WHATS_NEW = {
    "3.11": [("/profile/resume-template", "templates", "Pick a resume template",
              "Four ATS-friendly layouts. Preview each one with your own resume and pick the one that fits your profile. "
              "Empty sections are now left off, and long rows wrap instead of overlapping."),
             ("/profile/resume-template", "rules", "Optional: new resume rules for your prompt",
              "One click adds rules that help resumes fit any profile. If your prompt already works for you, skip it. "
              "Your current prompt is saved first, so you can go back anytime."),
             ("/how-it-works", "how", "How it works",
              "The whole system in six plain steps, with answers to common questions.")],
    "3.11.1": [("/settings/start", "replay", "A walkthrough of every page",
                "The intro now visits every page. Replay it here anytime.")],
    "3.11.3": [("/email", None, "Forward job emails to your inbox",
                "An inbox the app can't read, like a school account? Forward job emails from it to your connected inbox, "
                "and the app reads the original message inside.")],
    "3.11.4": [("/email", "fix-type", "Fix type",
                "If Claude labels an email wrong, pick what it really is. The job's status and Needs you follow. "
                "Assessments get a Done button."),
               ("/", "donuts", "Applied, rejected, and assessments by role family",
                "Three donuts in one row, with each family in the same color on all three.")],
    "3.12": [("/", "active-lines", "Active applications by role family",
              "One line per family: applications you've sent that haven't been rejected yet, day by day. "
              "Hover or tap for every family's count that day."),
             (None, None, "Updates install together",
              "When several updates are waiting, one click installs all of them, then this tour shows what's new.")],
    "3.15.2": [(None, None, "Approve your Chrome extension",
                "For safety, only the Chrome extension you approve can use Job Autopilot now. The next time you click "
                "it, a banner at the top asks you to approve it; compare the ID with chrome://extensions.")],
    "3.15": [("/interviews", "interviews", "Interviews and offers",
              "Every interview you've had stays here, even after a rejection (flagged Rejected). Offers are flagged and "
              "listed on top. On the Email page, Fix type on an interview keeps it when you mark it Rejected or Offer.")],
    "3.14": [(None, None, "Smoother updates",
              "Updates now run in the background with a progress bar, then open the new version by themselves. "
              "No more error page while the app restarts.")],
    "3.13": [("", "help-chat", "Ask the Help chat",
              "Questions about the app, or something not working? Ask here. It answers from the latest guides and "
              "checks your app's current state to tell you what's going on.")],
}


def whats_new_steps(since: str | None) -> list[dict]:
    """The tour after an update: every WHATS_NEW step for versions newer than `since`, up to this one."""
    from .updater import _vkey
    from .version import VERSION
    if not since:
        return []
    versions = sorted((v for v in WHATS_NEW if _vkey(since) < _vkey(v) <= _vkey(VERSION)), key=_vkey)
    steps = [{"page": page if page is not None else "", "title": title, "text": text, **({"target": f'[data-tour="{t}"]'} if t else {})}
             for v in versions for page, t, title, text in WHATS_NEW[v]]
    if not steps:
        return []
    home = url_for("dashboard")
    return ([{"page": home, "title": f"What's new in v{VERSION} 🎉",
              "text": f"You updated from v{since}. Here's a quick look at what changed, page by page. Skip anytime."}]
            + steps
            + [{"page": home, "title": "That's everything new", "cta": "Done",
                "text": "The full notes are in the banner at the top of the dashboard."}])


@app.get("/tour/steps")
def tour_steps_api():
    """The steps for tour.js when the walkthrough continues on another page."""
    if request.args.get("kind") == "new":
        return jsonify(whats_new_steps((db.kv_get("updated_from") or {}).get("v")))
    from . import greeting
    return jsonify(tour_steps(greeting.user_name()))


@app.post("/api/help")
def help_chat():
    """The Help chat: one question (plus the last few turns) in, one answer out. Nothing is stored."""
    from . import helpchat, llm
    body = request.get_json(silent=True) or {}
    q = str(body.get("q", "")).strip()
    if not q:
        return jsonify({"error": "Ask a question first."}), 400
    history = body.get("history") if isinstance(body.get("history"), list) else []
    try:
        return jsonify({"answer": helpchat.answer(q, history)})
    except llm.BackendUnavailable as e:
        msg = str(e)
        if "sign in" in msg.lower() or "logged in" in msg.lower():
            msg = ("I can't reach Claude: its sign-in has expired. Open a terminal, run `claude`, type `/login`, and sign in "
                   "with your Claude account. Then ask me again.")
        return jsonify({"error": msg[:400]}), 503
    except Exception as e:
        return jsonify({"error": f"Something went wrong answering that: {str(e)[:200]}"}), 500


@app.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html")


@app.route("/interviews")
def interviews_page():
    """Every interview you've had, kept even after a rejection (flagged), with offers on top."""
    from . import interviews
    items = interviews.history()
    return render_template("interviews.html", items=items, offers=[i for i in items if i["offer"]],
                           counts=interviews.counts(items))


@app.route("/assessments")
def assessments_page():
    from . import assessments
    assess = assessments.pending()
    for a in assess:
        a["label"] = assessments.due_label(a)
    # Everything that isn't still to do: finished, dismissed, or where the company has since rejected you (flagged,
    # so an assessment you took before a rejection still shows) or moved you on to an interview or offer.
    open_ids = {i for a in assess for i in a["ids"]}
    done = []
    for d in db.fetchall(
            "SELECT e.id, e.received_at, coalesce(j.company, e.company) AS company, coalesce(j.title, e.role) AS role, "
            "e.due_at, e.done_at, e.done_how, j.status AS job_status, "
            "(SELECT max(x.received_at) FROM email_events x WHERE x.event='rejection' AND x.state NOT IN ('ignored','undone') "
            " AND (x.job_id = e.job_id OR lower(x.company) = lower(e.company))) AS rejected_at, "
            "(SELECT max(x.received_at) FROM email_events x WHERE x.event IN ('interview','offer') AND x.state NOT IN ('ignored','undone') "
            " AND (x.job_id = e.job_id OR lower(x.company) = lower(e.company))) AS advanced_at "
            "FROM email_events e LEFT JOIN jobs j ON j.id = e.job_id "
            "WHERE e.event='assessment' AND e.state NOT IN ('ignored') ORDER BY e.received_at DESC LIMIT 100"):
        if d["id"] in open_ids:
            continue
        rejected = d["job_status"] == "closed" or bool(d["rejected_at"] and d["rejected_at"] >= d["received_at"])
        advanced = bool(d["advanced_at"] and d["advanced_at"] >= d["received_at"]) and not rejected
        if not (d["done_at"] or rejected or advanced):
            continue
        done.append({**d, "rejected": rejected, "advanced": advanced})
    return render_template("assessments.html", assess=assess, done=done)


@app.post("/assessments/<int:event_id>/<how>")
def assessment_mark(event_id, how):
    from . import assessments
    if how not in ("done", "skip"):
        abort(404)
    a = next((x for x in assessments.pending() if event_id in x["ids"]), None)
    if not a:                                    # overdue or already moved on: mark that email itself
        ev = db.fetchone("SELECT e.id, coalesce(j.company, e.company) AS shown_company FROM email_events e "
                         "LEFT JOIN jobs j ON j.id = e.job_id WHERE e.id=? AND e.event='assessment'", (event_id,))
        if not ev:
            abort(404)
        a = {"ids": [ev["id"]], "shown_company": ev["shown_company"]}
    assessments.mark(a["ids"], "done" if how == "done" else "skipped")
    flash(f"Nice work on the {a['shown_company']} assessment! 🎯" if how == "done"
          else f"Okay, no more reminders for the {a['shown_company']} assessment.")
    return redirect(request.referrer or url_for("dashboard"))


@app.post("/tour/<how>")
def tour_finish(how):
    if how not in ("done", "skipped"):
        abort(404)
    if request.args.get("kind") == "new":           # the what's-new tour: shown once per update
        db.kv_set("updated_from", "")
        return ("", 204)
    db.kv_set("tour", how)
    return ("", 204)


# ----------------------------------------------------------------------------- app updates

@app.get("/healthz")
def healthz():
    from .version import VERSION
    return jsonify({"ok": True, "version": VERSION})


@app.post("/update/check")
def update_check():
    from . import updater
    st = updater.check(force=True)
    if st.get("error"):
        flash("Couldn't check for updates: " + st["error"])
    elif st.get("behind"):
        flash(f"An update is available ({'v' + st['remote_version'] if st.get('remote_version') else str(st['behind']) + ' new change(s)'}).")
    else:
        flash("You're on the latest version.")
    return redirect(request.referrer or url_for("settings_page", section="updates"))


@app.post("/update")
def update_apply():
    """Start the update in the background and show the progress page; it opens the new version when it's ready."""
    from . import updater
    from .version import VERSION
    res = updater.start()
    if not res.get("ok"):
        flash("Update didn't run: " + res["message"])
        return redirect(request.referrer or url_for("dashboard"))
    return render_template("updating.html", version=res.get("target"), current=VERSION)


@app.get("/update/progress")
def update_progress():
    from . import updater
    from .version import VERSION
    p = updater.PROGRESS
    return jsonify({"stage": p.get("stage") or "", "pct": p.get("pct") or 0, "message": p.get("message") or "",
                    "error": p.get("error") or "", "target": p.get("target"), "version": VERSION},
                   ), 200, {"Cache-Control": "no-store"}


@app.post("/settings/restart")
def restart_dashboard():
    """Restart this dashboard service (launchd brings it straight back) so 'Jobs at once' takes effect."""
    if sys.platform == "darwin":
        subprocess.Popen(["/bin/bash", "-c", f"sleep 1; launchctl kickstart -k gui/{os.getuid()}/com.jobautopilot.dashboard"])
    else:
        from . import updater
        updater.restart_soon(1.0)                # Windows: through Task Scheduler; Linux: in place (systemd keeps the PID)
    flash("Restarting the dashboard… refresh in a few seconds. Queued work resumes automatically.")
    return redirect(url_for("settings_page", section="pipeline"))


# ----------------------------------------------------------------------------- profile editor

PROFILE_FILES = {
    "system_prompt": ("Master prompt", "profile/system_prompt.md",
                      "Your full profile and every resume / cover letter rule. Claude reads it fresh on every call, so the next screen or document uses your edits."),
    "answers": ("Confirmed facts", "profile/answers.md",
                "Facts you confirmed on Needs you. Appended to the master prompt on every call; fold them into the master prompt when convenient."),
    "applicant": ("Form presets", "profile/applicant.json",
                  "Contact details and fixed answers (work authorization, EEO) the autofill uses. Must stay valid JSON."),
    "adapter": ("App adapter (advanced)", "prompts/app_adapter.md",
                "How the app asks Claude for JSON (question budget, bullet lengths, patch revisions). Change only if you know why."),
}


def _profile_path(name: str) -> Path:
    if name not in PROFILE_FILES:
        abort(404)
    rel = PROFILE_FILES[name][1]
    return PROFILE_DIR / rel.split("/", 1)[1] if rel.startswith("profile/") else Path(ROOT) / rel


def _versions(name: str) -> list[dict]:
    hist = PROFILE_DIR / "history"
    out = []
    for f in sorted(hist.glob(f"{name}__*"), key=lambda f: f.stat().st_mtime, reverse=True)[:30]:
        ts = f.stem.split("__", 1)[1][:15]          # "20261022-021807" (a "-2" suffix means a second save that second)
        try:
            label = strf(datetime.strptime(ts, "%Y%m%d-%H%M%S"), "%b %d, %-I:%M:%S %p")
        except ValueError:
            continue
        out.append({"file": f.name, "label": label, "size": f.stat().st_size})
    return out


@app.route("/profile")
def profile_page():
    name = request.args.get("f", "system_prompt")
    path = _profile_path(name)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    restore = request.args.get("version")
    if restore:
        vp = PROFILE_DIR / "history" / Path(restore).name
        if vp.exists() and vp.name.startswith(name + "__"):
            text = vp.read_text(encoding="utf-8")
    modified = strf(datetime.fromtimestamp(path.stat().st_mtime), "%b %d, %-I:%M %p") if path.exists() else "never"
    return render_template("profile.html", files=PROFILE_FILES, name=name, text=text, modified=modified,
                           versions=_versions(name), restoring=restore)


def _save_profile_file(name: str, text: str) -> None:
    """Write a profile file, keeping the previous version in profile/history (any change can be undone)."""
    path = _profile_path(name)
    hist = PROFILE_DIR / "history"
    hist.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp, n = f"{datetime.now():%Y%m%d-%H%M%S}", 1
        backup = hist / f"{name}__{stamp}{path.suffix}"
        while backup.exists():                       # two saves in one second must not overwrite a backup
            n += 1
            backup = hist / f"{name}__{stamp}-{n}{path.suffix}"
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        for old in sorted(hist.glob(f"{name}__*"), reverse=True)[30:]:
            old.unlink()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _latest_resume_data() -> tuple[dict, str]:
    """The newest tailored resume (your real content), else the built-in sample."""
    row = db.fetchone("SELECT company, tailor_json FROM jobs WHERE tailor_json IS NOT NULL ORDER BY updated_at DESC LIMIT 1")
    if row:
        try:
            return json.loads(row["tailor_json"]), f"your latest tailored resume ({row['company']})"
        except ValueError:
            pass
    return json.loads((ROOT / "tests/fixtures/sample_tailor.json").read_text(encoding="utf-8")), "the built-in sample"


@app.route("/profile/resume-template")
def resume_template_page():
    from . import resume_rules
    from .pipeline import resume_template
    from .render import pdfgen
    prompt_path = _profile_path("system_prompt")
    prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    _, source = _latest_resume_data()
    return render_template("resume_template.html", templates=pdfgen.TEMPLATES, current=resume_template(), source=source,
                           rules_have=resume_rules.status(prompt), rules_version=resume_rules.RULES_VERSION)


@app.post("/profile/resume-template")
def resume_template_save():
    from .render import pdfgen
    t = request.form.get("template", "")
    if t not in pdfgen.TEMPLATES:
        abort(400)
    path = _profile_path("applicant")
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data["resume_template"] = t
    _save_profile_file("applicant", json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    flash(f"Resume template set to {pdfgen.TEMPLATES[t]['label']}. New resumes use it; use Regenerate on a job to redo one.")
    return redirect(url_for("resume_template_page"))


@app.get("/profile/resume-preview")
def resume_preview():
    """A real PDF of your latest tailored resume drawn with the chosen template (nothing is saved to a job)."""
    from .render import pdfgen
    t = request.args.get("t", "")
    if not t:
        from .pipeline import resume_template
        t = resume_template()                       # no template named: show the one you use
    if t not in pdfgen.TEMPLATES:
        abort(404)
    data, _ = _latest_resume_data()
    data["_template"] = t
    fits = [L for L in pdfgen.LAYOUTS if pdfgen.resume_fits(data, L)[0]]
    out = DATA_DIR / "previews"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"resume-{t}.pdf"
    pdfgen.build_resume(data, str(path), fits[-1] if fits else pdfgen.LAYOUTS[0], f"Preview — {pdfgen.TEMPLATES[t]['label']}")
    return send_file(path, mimetype="application/pdf")


@app.post("/profile/resume-rules")
def resume_rules_apply():
    """Add (or refresh) the app's resume rules in the master prompt. The previous prompt stays in history."""
    from . import resume_rules
    path = _profile_path("system_prompt")
    if not path.exists():
        flash("No master prompt yet; set it up on Profile first.")
        return redirect(url_for("resume_template_page"))
    before = path.read_text(encoding="utf-8")
    had = resume_rules.status(before)
    _save_profile_file("system_prompt", resume_rules.apply(before))
    flash(("Updated the resume rules in your prompt." if had else "Added the resume rules to your prompt.")
          + " Your previous prompt is saved; restore it from Profile → Master prompt → Earlier versions anytime.")
    return redirect(url_for("resume_template_page"))


@app.post("/profile/save")
def profile_save():
    import json as _json
    body = request.get_json(force=True)
    name = body.get("name", "")
    _profile_path(name)                  # rejects unknown file names
    text = body.get("text", "")
    if name == "system_prompt" and len(text.strip()) < 2000:
        return jsonify({"ok": False, "error": "The master prompt looks almost empty; not saved (it would break every run)."}), 400
    if name == "applicant":
        try:
            _json.loads(text)
        except ValueError as e:
            return jsonify({"ok": False, "error": f"Not valid JSON: {e}"}), 400
    _save_profile_file(name, text)   # keeps the previous version so any edit can be undone
    return jsonify({"ok": True, "saved": strf(datetime.now(), "%-I:%M:%S %p"), "chars": len(text)})


@app.route("/activity")
def activity():
    return jsonify({"state": worker.state, "log": list(worker.log_lines)[-120:]})


@app.route("/file")
def file():
    p = request.args.get("p", "")
    if not output_path_ok(p) or not Path(p).exists():
        abort(404)
    return send_file(p, as_attachment=request.args.get("dl") == "1")


class _QuietPolling:
    """Drop access-log lines for the endpoints the pages poll every few seconds (they flooded the output)."""

    def filter(self, record) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in ("GET /activity", "GET /progress", "GET /static/", "GET /favicon"))


def _release_inherited_socket(port: int) -> None:
    """After an in-place restart (os.execv), the old server's listening socket is still open in this process:
    werkzeug marks it inheritable and leaves its number in WERKZEUG_SERVER_FD. Binding the port again would then
    fail with "Address already in use" and the app would never come back. Close it first (only if it really is
    a socket on our port, never some other file that happens to have that number)."""
    import socket
    fd = os.environ.pop("WERKZEUG_SERVER_FD", None)
    if not fd or not fd.isdigit():
        return
    try:
        s = socket.socket(fileno=int(fd))
    except OSError:
        return
    try:
        if s.type == socket.SOCK_STREAM and s.getsockname()[1] == port:
            s.close()
            return
    except OSError:
        pass
    s.detach()


def _wait_for_port(port: int, seconds: float = 20) -> bool:
    """Right after a restart the previous copy can still hold the port for a moment. Wait for it to free up."""
    import socket
    import time as _t
    end = _t.time() + seconds
    while True:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            if _t.time() > end:
                return False
            _t.sleep(0.5)
        finally:
            s.close()


def serve() -> None:
    import faulthandler
    import logging
    import signal
    if hasattr(signal, "SIGUSR1"):                 # macOS / Linux only
        faulthandler.register(signal.SIGUSR1, all_threads=True)   # `kill -USR1 <pid>` writes every thread's stack to the log
    _release_inherited_socket(int(env("PORT", "5055") or 5055))
    logging.getLogger("werkzeug").addFilter(_QuietPolling())
    db.init()
    db.recover_interrupted()
    progress.reset()
    worker.start()
    from . import updater
    threading.Thread(target=updater.loop, name="updater", daemon=True).start()
    if not db.kv_get("tour") and db.count("runs"):               # existing installs skip the first-run tour
        db.kv_set("tour", "existing")
    try:
        from . import h1b
        h1b.renormalize()                      # once after a norm() change ("&" vs "and" in employer names)
    except Exception as e:
        print(f"h1b names: {e}")
    try:
        from . import assessments
        assessments.backfill()
        assessments.sync_todos()
    except Exception as e:
        print(f"assessments: {e}")
    n = worker.resume_queue()
    if n:
        print(f"Resumed {n} job(s) waiting for documents.")
    port = int(env("PORT", "5055") or 5055)
    if not _wait_for_port(port):
        print(f"Port {port} is still busy; exiting so the service manager starts the dashboard again shortly.", flush=True)
        os._exit(1)
    print(f"Job Autopilot running at http://127.0.0.1:{port}", flush=True)
    try:
        app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
    except (OSError, SystemExit) as e:
        # The web server couldn't start (or stopped); background threads would keep a dead dashboard "running",
        # so exit for real and let launchd / Task Scheduler / systemd bring it back.
        print(f"The dashboard's web server stopped ({e}); exiting so it restarts.", flush=True)
        os._exit(1)
