"""Local dashboard: python -m app.cli serve  ->  http://127.0.0.1:5055"""
from __future__ import annotations

import os
import subprocess
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


@app.template_filter("ago")
def ago(iso: str | None) -> str:
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso)
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


@app.template_filter("local")
def local(iso: str | None) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%b %d, %I:%M %p") if iso else "—"


@app.context_processor
def inject():
    open_n = db.fetchone("SELECT count(*) AS n FROM interventions WHERE status='open'")["n"]
    try:
        backend = llm.backend()
    except llm.LLMError:
        backend = "not configured"
    return {"open_interventions": open_n, "worker": worker.state, "status_labels": STATUS_LABELS, "backend": backend}


def _followups() -> None:
    """Turn outreach timing rules (Section XI) into reminders: day 3-5 share work, day 7-10 one ask."""
    for o in db.fetchall("SELECT o.*, j.company FROM outreach o JOIN jobs j ON j.id=o.job_id WHERE o.status IN ('sent','accepted') AND o.sent_at IS NOT NULL"):
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(o["sent_at"])).days
        who = o["person_name"] or o["target"].replace("_", " ")
        if 3 <= days <= 5:
            db.add_intervention(o["job_id"], "outreach", f"Day {days}: share one piece of real work with {who} at {o['company']}",
                                "Section XI: a short note with one artifact (e.g. a project README result). No pitch.")
        elif 7 <= days <= 10:
            db.add_intervention(o["job_id"], "outreach", f"Day {days}: one specific low-effort ask to {who} at {o['company']}",
                                "One follow-up max. A 15-minute coffee chat or one question about the team.")


@app.route("/")
def dashboard():
    _followups()
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
        "applied_total": applied_total,
        "interviewing": counts.get("interviewing", 0) + counts.get("offer", 0),
    }
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
    return render_template("dashboard.html", kpis=kpis, series=series, peak=peak, funnel=funnel, fmax=fmax,
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


@app.route("/jobs")
def jobs():
    tab = request.args.get("status", "")
    q = request.args.get("q", "").strip()
    source = request.args.get("source", "")
    startup = request.args.get("startup") == "1"
    min_match = request.args.get("min_match", "")
    sort = request.args.get("sort") or ("applied" if tab in ("applied", "applied_all") else "posted")
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
    cond = " AND ".join(where + filters) or "1=1"
    page = Page(db.count("jobs", cond, tuple(params + fparams)))
    rows = db.list_jobs(cond, tuple(params + fparams), order=JOB_SORTS.get(sort, JOB_SORTS["posted"])[1],
                        limit=page.per_page, offset=page.offset)
    # tab counts honour the search/filters, so you see where your matches are
    fcond = " AND ".join(filters) or "1=1"
    counts = {k: db.count("jobs", f"({c}) AND {fcond}", tuple(fparams)) for k, _, c in JOB_TABS}
    return render_template("jobs.html", jobs=rows, status=tab, q=q, source=source, startup=startup, min_match=min_match,
                           sort=sort, sorts=JOB_SORTS, tabs=JOB_TABS, counts=counts, page=page)


@app.route("/jobs/<path:job_id>")
def job_detail(job_id):
    job = db.get_job(job_id) or abort(404)
    ivs = db.fetchall("SELECT * FROM interventions WHERE job_id=? ORDER BY status='open' DESC, id DESC", (job_id,))
    outreach = db.fetchall("SELECT * FROM outreach WHERE job_id=? ORDER BY target", (job_id,))
    from . import contacts
    return render_template("job.html", job=job, ivs=ivs, outreach=outreach, applicant=applicant(), h1b=h1b.lookup(job["company"]),
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
        db.update_job(job_id, status="to_screen")
        worker.submit("screen", job_id)
    elif act == "regenerate":
        db.update_job(job_id, status="to_tailor")
        worker.submit("tailor", job_id)
        flash("Regenerating documents in the background.")
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
        "WHERE i.status='open' ORDER BY CASE i.kind WHEN 'error' THEN 0 WHEN 'referral' THEN 1 WHEN 'decision' THEN 2 WHEN 'missing_info' THEN 3 "
        "WHEN 'quality' THEN 4 WHEN 'submit' THEN 5 WHEN 'outreach' THEN 6 ELSE 7 END, i.id DESC")
    from . import contacts as _c
    for i in ivs:
        if i["kind"] == "referral":
            ask = db.fetchone("SELECT a.*, c.name, c.phone, c.linkedin, c.email FROM referral_asks a JOIN contacts c ON c.id=a.contact_id "
                              "WHERE a.job_id=? AND ? LIKE 'Ask ' || c.name || ' (%'", (i["job_id"], i["title"]))
            i["ask"] = ask
    groups: dict[str, list] = {}
    for i in ivs:
        groups.setdefault(i["kind"], []).append(i)
    return render_template("inbox.html", groups=groups)


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
    else:
        db.execute("UPDATE interventions SET status=?, resolved_at=? WHERE id=?",
                   ("dismissed" if act == "dismiss" else "resolved", db.now(), iid))
    return redirect(request.referrer or url_for("inbox"))


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
        try:
            n = h1b.import_csv(request.files["csv"].read())
            flash(f"Imported {n:,} employers from the USCIS file.")
        except ValueError as e:
            flash(f"Import failed: {e}")
        return redirect(url_for("sources_page"))
    status = {r["key"]: r for r in db.fetchall("SELECT * FROM source_status")}
    rows = []
    for c in companies():
        key = f"{c['ats']}:{c['slug']}"
        rows.append({**c, "status": status.get(key), "h1b_data": h1b.lookup(c["name"])})
    n_h1b = db.fetchone("SELECT count(*) AS n FROM h1b_employers")["n"]
    feeds = db.fetchall("SELECT * FROM feed_status ORDER BY feed")
    boards = db.fetchall("SELECT * FROM discovered_boards ORDER BY found_at DESC")
    runs = db.fetchall("SELECT * FROM runs ORDER BY id DESC LIMIT 15")
    return render_template("sources.html", rows=rows, n_h1b=n_h1b, runs=runs, feeds=feeds, boards=boards)


@app.post("/boards/<path:key>")
def toggle_board(key):
    db.execute("UPDATE discovered_boards SET enabled = 1 - enabled WHERE key=?", (key,))
    return redirect(url_for("sources_page"))


SOURCE_LABELS = {"greenhouse": "Company board", "lever": "Company board", "ashby": "Company board", "discovered": "Discovered board",
                 "jobright": "Jobright", "simplify": "Simplify", "hn": "HN startups", "web": "Added by you"}


@app.template_filter("smslink")
def smslink(phone: str | None, body: str = "") -> str:
    """Opens Messages with the text filled in (Mac and iPhone)."""
    from urllib.parse import quote
    digits = "".join(ch for ch in (phone or "") if ch.isdigit() or ch == "+")
    return f"sms:{digits}&body={quote(body or '')}" if digits else ""


@app.template_filter("source_label")
def source_label(s):
    return SOURCE_LABELS.get(s or "", s or "—")


@app.post("/run")
def run_now():
    kind = request.form.get("kind", "run")
    source = request.form.get("source", "manual")
    worker.submit(kind, source if kind in ("run", "process") else None)
    flash("Pipeline started in the background. Watch the activity log.")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/progress")
def progress_json():
    snap = progress.snapshot()
    pu = worker.state.get("paused_until")
    snap["paused_until"] = datetime.fromtimestamp(pu).strftime("%I:%M %p").lstrip("0") if pu else None
    return jsonify(snap)


def _schedule_info() -> dict:
    """What launchd and pmset say about the daily runs and lid-closed wakes."""
    import plistlib
    from datetime import date
    info = {"installed": False, "times": [], "next": None, "wake": False, "wake_next": None}
    plist = Path.home() / "Library/LaunchAgents/com.jobautopilot.daily.plist"
    if plist.exists():
        try:
            d = plistlib.loads(plist.read_bytes())
            times = sorted((x["Hour"], x.get("Minute", 0)) for x in d.get("StartCalendarInterval", []))
            info["installed"] = True
            info["times"] = [datetime(2000, 1, 1, h, m).strftime("%-I:%M %p") for h, m in times]
            now = datetime.now()
            upcoming = [datetime.combine(date.today() + timedelta(days=dd), datetime.min.time()).replace(hour=h, minute=m)
                        for dd in (0, 1) for h, m in times]
            nxt = next((t for t in upcoming if t > now), None)
            info["next"] = nxt.strftime("%a %-I:%M %p") if nxt else None
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
    ("POSTED_WITHIN_HOURS", "Look back (hours)", "int", "Only postings newer than this are fetched each run.", (1, 336)),
    ("MAX_SCREEN_PER_RUN", "Screens per run", "int", "How many new jobs Claude scores per run (cheap calls).", (1, 300)),
    ("MAX_TAILOR_PER_RUN", "Documents per run", "int", "How many tailored resume + cover letter packages are built per run.", (0, 100)),
    ("PARALLEL_JOBS", "Jobs at once", "int", "Screens and documents built in parallel. Higher is faster but uses your Claude limit faster. Applies after a restart.", (1, 6)),
    ("MAX_REVISIONS", "Revision rounds", "int", "How many times Claude may fix lines that fail the checks.", (0, 4)),
    ("EFFORT_TAILOR", "Resume writing effort", "choice", "Higher is slower but more careful.", ("low", "medium", "high")),
    ("EFFORT_SCREEN", "Screening effort", "choice", "Fit verdicts are short; low is usually enough.", ("low", "medium", "high")),
    ("AUTO_TAILOR_RISK", "Build documents for 'apply with risk' roles", "bool", "Off = those roles wait for your OK on Needs you.", None),
]
DEFAULTS = {"MIN_MATCH_FOR_DOCS": 7, "POSTED_WITHIN_HOURS": 24, "MAX_SCREEN_PER_RUN": 60, "MAX_TAILOR_PER_RUN": 15, "PARALLEL_JOBS": 3,
            "MAX_REVISIONS": 2, "EFFORT_TAILOR": "medium", "EFFORT_SCREEN": "low", "AUTO_TAILOR_RISK": "1"}


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
    return render_template("settings.html", sched=schedule.status(), tunables=TUNABLES, current=current,
                           feeds=feeds_config(), ampm=_ampm)


def _ampm(t: str) -> str:
    h, m = (int(x) for x in t.split(":"))
    return datetime(2000, 1, 1, h, m).strftime("%-I:%M %p")


@app.post("/settings/restart")
def restart_dashboard():
    """Restart this dashboard service (launchd brings it straight back) so 'Jobs at once' takes effect."""
    subprocess.Popen(["/bin/bash", "-c", f"sleep 1; launchctl kickstart -k gui/{os.getuid()}/com.jobautopilot.dashboard"])
    flash("Restarting the dashboard… refresh in a few seconds. Queued work resumes automatically.")
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
        out.append({"file": f.name, "label": datetime.strptime(ts, "%Y%m%d-%H%M%S").strftime("%b %d, %-I:%M %p"), "size": f.stat().st_size})
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
    modified = datetime.fromtimestamp(path.stat().st_mtime).strftime("%b %d, %-I:%M %p") if path.exists() else "never"
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
    return jsonify({"ok": True, "saved": datetime.now().strftime("%-I:%M:%S %p"), "chars": len(text)})


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
    faulthandler.register(signal.SIGUSR1, all_threads=True)   # `kill -USR1 <pid>` writes every thread's stack to the log
    logging.getLogger("werkzeug").addFilter(_QuietPolling())
    db.init()
    db.recover_interrupted()
    progress.reset()
    worker.start()
    n = worker.resume_queue()
    if n:
        print(f"Resumed {n} job(s) waiting for documents.")
    port = int(env("PORT", "5055") or 5055)
    print(f"Job Autopilot running at http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
