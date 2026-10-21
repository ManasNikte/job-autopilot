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
from .config import ANSWERS_FILE, DATA_DIR, PROFILE_DIR, ROOT, applicant, companies, env
from .pipeline import output_path_ok

app = Flask(__name__)
from .ext_api import bp as ext_bp  # noqa: E402  (browser extension API)
app.register_blueprint(ext_bp)
app.secret_key = "local-only"
app.config["TEMPLATES_AUTO_RELOAD"] = True   # template tweaks show up without restarting background work

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


def strf(dt, fmt: str) -> str:
    """strftime with %-d / %-I (no leading zero) on every OS; Windows' strftime rejects the '-' flag."""
    return dt.strftime(fmt.replace("%-d", str(dt.day)).replace("%-I", str(int(dt.strftime("%I")))))


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
    return {"open_interventions": open_n, "unmatched_mail": unmatched_mail, "credits": _credits(), "operator_stats": op, "app_update": upd, "app_version": VERSION, "worker": worker.state, "status_labels": STATUS_LABELS, "backend": backend}


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


@app.route("/")
def dashboard():
    _followups()
    just_updated = None
    if request.args.get("updated"):
        from . import updater
        from .version import VERSION
        try:
            notes = updater.parse_changelog((updater.ROOT / "CHANGELOG.md").read_text(), "0")
            just_updated = next((n for n in notes if n["version"] == VERSION), {"version": VERSION, "items": []})
        except OSError:
            just_updated = {"version": VERSION, "items": []}
    today = datetime.now().astimezone().date()
    start_utc = datetime.combine(today, datetime.min.time()).astimezone().astimezone(timezone.utc).isoformat()
    counts = {r["status"]: r["n"] for r in db.fetchall("SELECT status, count(*) AS n FROM jobs GROUP BY status")}
    applied_total = sum(counts.get(s, 0) for s in ("applied", "interviewing", "offer", "closed"))
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    kpis = {
        "posted_today": db.fetchone("SELECT count(*) AS n FROM jobs WHERE posted_at>=? AND status!='filtered_out'", (start_utc,))["n"],
        "ready": counts.get("ready", 0),
        "decide": counts.get("needs_decision", 0),
        "applied_week": db.fetchone("SELECT count(*) AS n FROM jobs WHERE applied_at>=?", (week_ago,))["n"],
        "applied_today": db.fetchone("SELECT count(*) AS n FROM jobs WHERE applied_at>=?", (start_utc,))["n"],
        "applied_total": applied_total,
        "interviewing": counts.get("interviewing", 0) + counts.get("offer", 0),
    }
    from . import assessments
    assess = assessments.pending()
    for a in assess:
        a["label"] = assessments.due_label(a)
    kpis["assessments"] = sum(1 for a in assess if not a["overdue"])
    # 14-day applications series (one value per local day).
    days = [today - timedelta(days=i) for i in range(13, -1, -1)]
    rows = db.fetchall("SELECT applied_at FROM jobs WHERE applied_at IS NOT NULL")
    per_day = {d: 0 for d in days}
    for r in rows:
        d = datetime.fromisoformat(r["applied_at"]).astimezone().date()
        if d in per_day:
            per_day[d] += 1
    series = [{"label": d.strftime("%b %d"), "short": d.strftime("%d"), "n": per_day[d]} for d in days]
    peak = max([s["n"] for s in series] + [1])

    funnel = [{"status": s, "label": STATUS_LABELS[s], "n": counts.get(s, 0)} for s in PIPELINE_STAGES]
    fmax = max([f["n"] for f in funnel] + [1])
    today_cond = "posted_at>=? AND status NOT IN ('filtered_out')"
    today_page = Page(db.count("jobs", today_cond, (start_utc,)), per_page=10, param="today_page", anchor="today")
    today_jobs = db.list_jobs(today_cond, (start_utc,), order="coalesce(match_score, -1) DESC, posted_at DESC",
                              limit=today_page.per_page, offset=today_page.offset)
    applied = "status IN ('applied','interviewing','offer','closed')"
    fam_rows = db.fetchall(f"SELECT coalesce(role_family,'Other') AS fam, count(*) AS n FROM jobs WHERE {applied} GROUP BY fam")
    family_page = Page(len(fam_rows), per_page=6, param="family_page", anchor="by-family")
    by_family = db.fetchall(f"SELECT coalesce(role_family,'Other') AS fam, count(*) AS n FROM jobs WHERE {applied} "
                            "GROUP BY fam ORDER BY n DESC, fam LIMIT ? OFFSET ?", (family_page.per_page, family_page.offset))
    n_companies = db.fetchone(f"SELECT count(DISTINCT company) AS n FROM jobs WHERE {applied}")["n"]
    company_page = Page(n_companies, per_page=6, param="company_page", anchor="by-company")
    by_company = db.fetchall(f"SELECT company, count(*) AS n FROM jobs WHERE {applied} GROUP BY company "
                             "ORDER BY n DESC, company COLLATE NOCASE LIMIT ? OFFSET ?", (company_page.per_page, company_page.offset))
    last_run = db.fetchone("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
    from . import greeting
    user_name = greeting.user_name()
    hello = greeting.greet(name=user_name) if user_name else None
    show_tour = bool(request.args.get("tour")) or (bool(user_name) and not db.kv_get("tour"))   # after the name box
    return render_template("dashboard.html", assess=assess, tour=tour_steps(user_name) if show_tour else None, just_updated=just_updated, hello=hello, user_name=user_name, kpis=kpis, series=series, peak=peak, funnel=funnel, fmax=fmax,
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
            log = open(DATA_DIR / "autofill.log", "a")
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
    return redirect(job.get("apply_url") or job["url"])


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
    rows = db.fetchall(f"SELECT e.*, j.company AS job_company, j.title AS job_title, j.status AS job_status FROM email_events e "
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
        flash("Connect an email account first (Settings → Email).")
        return redirect(url_for("settings_page") + "#email")
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
    db.execute("UPDATE email_events SET state='ignored' WHERE id=?", (eid,))
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
        return redirect(url_for("settings_page") + "#email")
    address = request.form.get("address", "").strip()
    pw = request.form.get("app_password", "").replace(" ", "")
    if not address or len(pw) < 16:
        flash("Enter your Gmail address and the 16-character app password.")
        return redirect(url_for("settings_page") + "#email")
    try:
        mailsync.gmail_test(address, pw)
    except Exception as e:
        flash(f"Gmail sign-in failed: {e}. Check the address, that 2-Step Verification is on, and the app password.")
        return redirect(url_for("settings_page") + "#email")
    mailsync.set_secret(f"gmail:{address}", pw)
    acc = mailsync.accounts()
    acc["gmail"] = {"address": address, "connected_at": db.now()}
    mailsync.save_accounts(acc)
    flash(f"Gmail connected ({address}), read-only. Click 'Check email now' on the Email page, or wait for the next check.")
    return redirect(url_for("settings_page") + "#email")


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
        return redirect(url_for("settings_page") + "#email")
    client_id = request.form.get("client_id", "").strip()
    address = request.form.get("address", "").strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", client_id):
        flash("Paste the Application (client) ID from your Azure app registration (36 characters).")
        return redirect(url_for("settings_page") + "#email")
    tenant = address.split("@")[-1].lower() if "@" in address else "organizations"   # your organization's directory
    try:
        mailsync.ms_start_signin(client_id, tenant)
    except Exception as e:
        flash(f"Could not start Microsoft sign-in: {e}")
    return redirect(url_for("settings_page", ms=client_id) + "#email")


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
    return redirect(url_for("settings_page") + "#email")


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
    ("MAX_SCREEN_PER_RUN", "Screens per run", "int", "How many new jobs Claude scores per run (cheap calls).", (1, 300)),
    ("MAX_TAILOR_PER_RUN", "Documents per run", "int", "How many tailored resume + cover letter packages are built per run.", (0, 100)),
    ("PARALLEL_JOBS", "Jobs at once", "int", "Screens and documents built in parallel. Higher is faster but uses your Claude limit faster. Applies after a restart.", (1, 6)),
    ("MAX_REVISIONS", "Revision rounds", "int", "How many times Claude may fix lines that fail the checks.", (0, 4)),
    ("EFFORT_TAILOR", "Resume writing effort", "choice", "Higher is slower but more careful.", ("low", "medium", "high")),
    ("EFFORT_SCREEN", "Screening effort", "choice", "Fit verdicts are short; low is usually enough.", ("low", "medium", "high")),
    ("AUTO_TAILOR_RISK", "Build documents for 'apply with risk' roles", "bool", "Off = those roles wait for your OK on Needs you.", None),
    ("SPONSOR_WEB_CHECKS", "Online sponsor lookups per run", "int", "For companies missing from your USCIS data, Claude searches public H-1B disclosure data (DOL LCAs, USCIS approvals) once per company and caches the answer for 90 days. Each lookup is one small Claude call with web search. 0 turns it off.", (0, 200)),
    ("MAIL_CHECK_MINUTES", "Check email every (minutes)", "int", "How often connected inboxes are read for application updates (Settings → Email). 0 = only when you click 'Check email now'.", (0, 1440)),
    ("MAIL_LOOKBACK_DAYS", "Email look-back on first check (days)", "int", "How far back the first check of a newly connected inbox reads.", (1, 90)),
    ("SCREEN_UNKNOWN_SPONSOR", "Screen jobs when sponsorship is unknown", "bool", "On = jobs from companies with no H-1B signal yet still go to Claude (its visa gate judges the posting); uses more screens. Off = they wait under Filtered out. Importing the USCIS H-1B data on Sources covers most companies either way.", None),
]
DEFAULTS = {"MIN_MATCH_FOR_DOCS": 7, "MIN_SALARY": 100000, "POSTED_WITHIN_HOURS": 24, "MAX_SCREEN_PER_RUN": 60, "MAX_TAILOR_PER_RUN": 15, "PARALLEL_JOBS": 3,
            "MAX_REVISIONS": 2, "EFFORT_TAILOR": "medium", "EFFORT_SCREEN": "low", "AUTO_TAILOR_RISK": "1", "SCREEN_UNKNOWN_SPONSOR": "0", "SPONSOR_WEB_CHECKS": 25,
            "MAIL_CHECK_MINUTES": 60, "MAIL_LOOKBACK_DAYS": 14}


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    from . import schedule
    from .config import CONFIG_DIR, feeds_config, save_settings
    if request.method == "POST":
        section = request.form.get("section")
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
                (CONFIG_DIR / "feeds.json").write_text(__import__("json").dumps(fc, indent=2, ensure_ascii=False))
                flash("Saved. The next run uses these settings.")
        except ValueError as e:
            flash(f"Not saved: {e}")
        return redirect(url_for("settings_page"))
    current = {k: env(k, str(DEFAULTS[k])) for k, *_ in TUNABLES}
    from . import mailsync
    ms_id = request.args.get("ms", "")
    from . import cloud, greeting
    return render_template("settings.html", user_name=greeting.user_name(), cloud_cfg=cloud.config(), cloud_state=cloud.state(),
                           cloud_status=cloud.STATUS, cloud_connected=cloud.connected(), upd_state=__import__('app.updater', fromlist=['STATE']).STATE, cloud_default_name=cloud.default_machine_name(), cloud_city=cloud.city() if cloud.connected() else cloud.default_city(),
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
    if not origin or origin.startswith("chrome-extension://"):
        return None
    from urllib.parse import urlparse
    host = urlparse(origin).netloc
    if host in (request.host, f"127.0.0.1:{request.host.split(':')[-1]}", f"localhost:{request.host.split(':')[-1]}"):
        return None
    abort(403)


@app.post("/settings/cloud")
def cloud_connect():
    from . import cloud
    if request.form.get("disconnect"):
        cloud.disconnect()
        flash("This computer is disconnected from the online dashboard, and its data there was removed.")
        return redirect(url_for("settings_page") + "#cloud")
    email, password = request.form.get("email", "").strip(), request.form.get("password", "")
    name = (request.form.get("machine_name") or cloud.default_machine_name()).strip()
    try:
        cloud.connect(email, password, name, request.form.get("city", ""))
        worker.submit("cloud")
        flash(f"Connected as {email}. Progress shows up on the online dashboard within a minute.")
    except Exception as e:
        flash(f"Couldn't connect: {e}")
    return redirect(url_for("settings_page") + "#cloud")


@app.post("/settings/cloud/wake")
def cloud_wake():
    from . import schedule
    minutes = request.form.get("minutes", "0")
    minutes = minutes if minutes in ("0", "15", "30", "60") else "0"
    schedule.set_remote_wake(int(minutes))
    flash("Saved. " + ("Your Mac will check for phone requests every " + minutes + " minutes while plugged in."
                       if minutes not in ("", "0") else "Phone requests run when your Mac is awake."))
    return redirect(url_for("settings_page") + "#cloud")


@app.post("/settings/cloud/city")
def cloud_city():
    from . import cloud
    saved = cloud.set_city(request.form.get("city", ""))
    flash(f"Saved. The site operator sees your city as {saved}." if saved else "Saved. Your city is private now.")
    return redirect(url_for("settings_page") + "#cloud")


@app.get("/users")
def users_page():
    """Site operator only: everyone using the online dashboard, with application counts (never companies)."""
    from . import cloud
    rows = cloud.operator_users() if cloud.connected() else None
    if rows is None:
        abort(404)
    for r in rows:
        r["active"] = (r.get("applied_14d") or 0) > 0
    stats = {"users": len(rows), "active": sum(r["active"] for r in rows),
             "week": sum(r.get("applied_week") or 0 for r in rows)}
    return render_template("users.html", rows=rows, stats=stats)


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
    """The walkthrough shown once to new users (app/static/tour.js). `target` is a [data-tour] element."""
    hi = f", {name.split()[0]}" if name else ""
    return [
        {"title": f"Welcome to Job Autopilot{hi} 👋",
         "text": "A one-minute tour of how it finds jobs, writes your documents, and keeps track of everything. "
                 "Skip it anytime; you can replay it from Settings."},
        {"target": '[data-tour="run"]', "title": "Run now",
         "text": "Finds new postings that match your search, has Claude screen each one against your profile, and writes "
                 "a tailored one-page resume and cover letter for the good matches. Scheduled runs do this on their own, even while you sleep."},
        {"target": '[data-tour="kpis"]', "title": "Your numbers at a glance",
         "text": "New roles today, documents ready to send, jobs waiting for your call, and what you've applied to. Click a tile to see those jobs."},
        {"target": '[data-tour="background"]', "title": "Watch the work happen",
         "text": "Live progress while jobs are screened and documents are written, with a time estimate. Nothing to do here."},
        {"target": '[data-tour="inbox"]', "title": "Needs you: your to-do list",
         "text": "The few things only you can do: decide on a borderline match, answer a missing fact, or review documents before you submit."},
        {"target": '[data-tour="jobs"]', "title": "Every job it found",
         "text": "Filter by status, see match scores, and open a job for its resume, cover letter, and why Claude rated it the way it did."},
        {"title": "Apply with the Chrome extension",
         "text": "Open a job's application page and click the Job Autopilot extension: it fills the form from your profile and attaches "
                 "your documents. You review everything and click Submit yourself. Nothing is ever sent without you."},
        {"target": '[data-tour="applied"]', "title": "Track what you sent",
         "text": "Everything you've applied to, filterable by date (today, yesterday, this week), with one click to copy all the links."},
        {"target": '[data-tour="email"]', "title": "Replies, sorted for you",
         "text": "Connect your inbox (read-only) and rejections, interviews, assessments, and offers update your jobs automatically."},
        {"target": '[data-tour="profile"]', "title": "Your profile drives everything",
         "text": "Your master prompt and form answers. Claude never invents experience, so the more accurate this is, the better your documents."},
        {"target": '[data-tour="settings"]', "title": "Make it yours",
         "text": "Run times, minimum pay, how many documents to build at once, email, the phone dashboard, and app updates."},
        {"title": "You're all set 🚀", "cta": "Let's go",
         "text": "Click Run now to start your first search. It takes a few minutes and you can watch it on this page. "
                 "Replay this tour anytime from Settings."},
    ]


@app.post("/assessments/<int:event_id>/<how>")
def assessment_mark(event_id, how):
    from . import assessments
    if how not in ("done", "skip"):
        abort(404)
    a = next((x for x in assessments.pending() if event_id in x["ids"]), None)
    if not a:
        abort(404)
    assessments.mark(a["ids"], "done" if how == "done" else "skipped")
    flash(f"Nice work on the {a['shown_company']} assessment! 🎯" if how == "done"
          else f"Okay, no more reminders for the {a['shown_company']} assessment.")
    return redirect(request.referrer or url_for("dashboard"))


@app.post("/tour/<how>")
def tour_finish(how):
    if how not in ("done", "skipped"):
        abort(404)
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
    return redirect(request.referrer or url_for("settings_page"))


@app.post("/update")
def update_apply():
    from . import updater
    res = updater.apply()
    if not res.get("ok"):
        flash("Update didn't run: " + res["message"])
        return redirect(request.referrer or url_for("dashboard"))
    if not res.get("restart"):
        flash(res["message"])
        return redirect(url_for("dashboard"))
    updater.restart_soon()
    return render_template("updating.html", version=res.get("version"))


@app.post("/settings/restart")
def restart_dashboard():
    """Restart this dashboard service (launchd brings it straight back) so 'Jobs at once' takes effect."""
    if sys.platform == "darwin":
        subprocess.Popen(["/bin/bash", "-c", f"sleep 1; launchctl kickstart -k gui/{os.getuid()}/com.jobautopilot.dashboard"])
        flash("Restarting the dashboard… refresh in a few seconds. Queued work resumes automatically.")
    else:
        flash("Restart the dashboard from your service manager (see CLAUDE_SETUP.md), or stop and start `python -m app.cli serve`.")
    return redirect(url_for("settings_page"))


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
    for f in sorted(hist.glob(f"{name}__*"), reverse=True)[:30]:
        ts = f.stem.split("__", 1)[1]
        out.append({"file": f.name, "label": strf(datetime.strptime(ts, "%Y%m%d-%H%M%S"), "%b %d, %-I:%M %p"), "size": f.stat().st_size})
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


@app.post("/profile/save")
def profile_save():
    import json as _json
    body = request.get_json(force=True)
    name = body.get("name", "")
    path = _profile_path(name)
    text = body.get("text", "")
    if name == "system_prompt" and len(text.strip()) < 2000:
        return jsonify({"ok": False, "error": "The master prompt looks almost empty; not saved (it would break every run)."}), 400
    if name == "applicant":
        try:
            _json.loads(text)
        except ValueError as e:
            return jsonify({"ok": False, "error": f"Not valid JSON: {e}"}), 400
    hist = PROFILE_DIR / "history"
    hist.mkdir(parents=True, exist_ok=True)
    if path.exists():   # keep the previous version so any edit can be undone
        (hist / f"{name}__{datetime.now():%Y%m%d-%H%M%S}{path.suffix}").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        for old in sorted(hist.glob(f"{name}__*"), reverse=True)[30:]:
            old.unlink()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
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


def serve() -> None:
    import faulthandler
    import logging
    import signal
    if hasattr(signal, "SIGUSR1"):                 # macOS / Linux only
        faulthandler.register(signal.SIGUSR1, all_threads=True)   # `kill -USR1 <pid>` writes every thread's stack to the log
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
        from . import assessments
        assessments.backfill()
        assessments.sync_todos()
    except Exception as e:
        print(f"assessments: {e}")
    n = worker.resume_queue()
    if n:
        print(f"Resumed {n} job(s) waiting for documents.")
    port = int(env("PORT", "5055") or 5055)
    print(f"Job Autopilot running at http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
