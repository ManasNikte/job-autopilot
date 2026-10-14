"""Local API for the Job Autopilot browser extension (extension/ folder).

Only the extension's background worker may call it: requests must carry the X-Autopilot header and
must not come from a web page origin. Because no CORS headers are ever sent, scripts on other
websites cannot read these responses even though the server listens on 127.0.0.1.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from flask import Blueprint, abort, jsonify, request, send_file

from . import db, form_fill
from .pipeline import output_path_ok

bp = Blueprint("ext", __name__, url_prefix="/api/ext")
VERSION = "1.0.0"


@bp.before_request
def only_the_extension():
    origin = request.headers.get("Origin", "")
    if request.headers.get("X-Autopilot") != "1" or (origin and not origin.startswith("chrome-extension://")):
        abort(403)


STATUS_TEXT = {
    "to_screen": "Waiting to be scored", "screening": "Scoring the match…", "rejected": "Not a fit",
    "needs_decision": "Your call (apply with risk)", "to_tailor": "Queued for documents",
    "tailoring": "Building resume and cover letter…", "ready": "Resume and cover letter ready",
    "applied": "Applied", "interviewing": "Interviewing", "offer": "Offer", "closed": "Closed", "error": "Error",
    "filtered_out": "Filtered out",
}


def _job_brief(j: dict) -> dict:
    from . import progress
    pct = next((a["pct"] for a in progress.snapshot()["active"] if a["job_id"] == j["id"]), None)
    return {"id": j["id"], "company": j["company"], "title": j["title"], "status": j["status"],
            "status_text": STATUS_TEXT.get(j["status"], j["status"]), "progress": pct,
            "verdict": j.get("verdict"), "match_score": j.get("match_score"),
            "has_documents": bool(j.get("resume_pdf")), "final_call": j.get("final_call")}


def match_job(url: str, title: str = "", strict: bool = False) -> dict | None:
    """Best guess at which tracked job an application page belongs to (strict: id/URL evidence only)."""
    u = urlparse(url)
    host, path, q = u.netloc.lower(), u.path.lower(), parse_qs(u.query)
    candidates = db.query_jobs("status IN ('ready','applied','tailoring','to_tailor')", order="updated_at DESC", limit=300)
    ids_in_url = set(re.findall(r"\d{5,}", url)) | set(q.get("gh_jid", []))
    uuids_in_url = set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", url.lower()))
    for j in candidates:                       # 1. the posting's own id appears in the URL
        native = j["id"].split(":")[-1].lower()
        if native in ids_in_url or native in uuids_in_url:
            return j
    for j in candidates:                       # 2. same page as the stored posting or apply link
        for link in (j.get("apply_url"), j.get("url")):
            lu = urlparse(link or "")
            if lu.netloc.lower() == host and lu.path.lower().rstrip("/") and path.startswith(lu.path.lower().rstrip("/")):
                return j
    if strict:
        return None
    last = db.kv_get("last_opened_job")        # 3. the job you just opened from the dashboard
    if last and datetime.now(timezone.utc) - datetime.fromisoformat(last["at"]) < timedelta(minutes=45):
        j = db.get_job(last["v"])
        if j and _company_hint(j["company"], host, title):
            return j
    company_hits = [j for j in candidates if j["status"] == "ready" and _company_hint(j["company"], host, title)]
    if len(company_hits) == 1:                 # 4. exactly one ready job at this company
        return company_hits[0]
    if last and datetime.now(timezone.utc) - datetime.fromisoformat(last["at"]) < timedelta(minutes=45):
        return db.get_job(last["v"])
    return None


def _company_hint(company: str, host: str, title: str) -> bool:
    slug = re.sub(r"[^a-z0-9]", "", company.lower())
    return bool(slug) and (slug in re.sub(r"[^a-z0-9]", "", host) or slug in re.sub(r"[^a-z0-9]", "", title.lower()))


@bp.get("/ping")
def ping():
    return jsonify({"ok": True, "version": VERSION})


@bp.get("/jobs")
def jobs():
    m = match_job(request.args.get("url", ""), request.args.get("title", ""))
    ready = db.query_jobs("status IN ('ready','tailoring','to_tailor')", order="updated_at DESC", limit=100)
    return jsonify({"match": _job_brief(m) if m else None, "ready": [_job_brief(j) for j in ready]})


@bp.get("/job")
def job_info():
    j = db.get_job(request.args.get("id", "")) or abort(404)
    return jsonify(_job_brief(j))


@bp.post("/add_job")
def add_job():
    """'Add this job in one click': store the page as a job, score it, and build documents if it passes."""
    import hashlib

    from . import worker
    body = request.get_json(force=True)
    url = (body.get("url") or "").split("#")[0]
    existing = match_job(url, body.get("title", ""), strict=True)
    if existing and existing.get("status") not in ("filtered_out",):
        return jsonify(_job_brief(existing))
    jid = "ext:" + hashlib.sha1(url.encode()).hexdigest()[:12]
    if not db.get_job(jid):
        db.upsert_job({
            "id": jid, "company": (body.get("company") or "Unknown company")[:120],
            "title": (body.get("title") or "Job")[:200], "location": body.get("location") or "",
            "url": url, "apply_url": url, "ats": "web", "posted_at": db.now(),
            "description": (body.get("description") or "")[:30000], "role_family": None,
            "status": "to_screen", "status_reason": "added from the browser extension",
        })
    else:
        db.update_job(jid, status="to_screen")
    db.kv_set("last_opened_job", jid)
    worker.submit("screen", jid)
    return jsonify(_job_brief(db.get_job(jid)))


@bp.post("/plan")
def plan():
    body = request.get_json(force=True)
    job = db.get_job(body.get("job_id", "")) or abort(404)
    fields = body.get("fields") or []
    if not fields:
        return jsonify({"answers": []})
    try:
        answers = form_fill.plan(job, fields, body.get("url", ""))
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"answers": answers})


@bp.get("/file")
def file():
    job = db.get_job(request.args.get("job_id", "")) or abort(404)
    path = job.get("cover_pdf" if request.args.get("kind") == "cover" else "resume_pdf")
    if not path or not output_path_ok(path) or not Path(path).exists():
        abort(404)
    return send_file(path, mimetype="application/pdf", download_name=Path(path).name)


@bp.post("/learn")
def learn():
    body = request.get_json(force=True)
    job = db.get_job(body.get("job_id", "")) or abort(404)
    fields = body.get("fields") or []
    learned = form_fill.learn(job, [(None, f) for f in fields], body.get("filled") or {},
                              {"vals": body.get("values") or {}}, bool(body.get("submitted")))
    return jsonify({"learned": learned})


@bp.post("/applied")
def applied():
    body = request.get_json(force=True)
    job = db.get_job(body.get("job_id", "")) or abort(404)
    if job["status"] not in ("applied", "interviewing", "offer"):
        db.update_job(job["id"], status="applied", applied_at=db.now())
        db.resolve_interventions(job["id"], ("submit",), "submitted (confirmation page seen by extension)")
    return jsonify({"ok": True})
