"""Interviews you've had, kept for good: an interview that later turns into a rejection or an offer stays an
interview, with a Rejected or Offer flag. Built from interview and offer emails (matched to a job or not) and from
jobs you marked Interviewing or Offer yourself. Feeds the dashboard tile, the Interviews page, and the phone
dashboard's counts.
"""
from __future__ import annotations

from . import db
from .h1b import norm as company_norm

LIVE = "state NOT IN ('ignored','undone')"


def _key(job_id: str | None, company: str | None) -> str:
    return f"job:{job_id}" if job_id else f"co:{company_norm(company or '')}"


def history() -> list[dict]:
    """One entry per interview process (a job, or a company for emails that didn't match a job), newest first."""
    items: dict[str, dict] = {}
    for e in db.fetchall(
            "SELECT e.id, e.job_id, e.received_at, e.event, e.link, e.summary, coalesce(j.company, e.company) AS company, "
            "coalesce(j.title, e.role) AS role, j.status AS job_status FROM email_events e LEFT JOIN jobs j ON j.id = e.job_id "
            f"WHERE e.event IN ('interview','offer') AND e.{LIVE} ORDER BY e.received_at"):
        k = _key(e["job_id"], e["company"])
        it = items.setdefault(k, {"key": k, "job_id": e["job_id"], "company": e["company"], "role": e["role"] or "",
                                  "first_at": e["received_at"], "job_status": e["job_status"], "email_id": e["id"],
                                  "offer": False, "offer_at": None, "offer_email_id": None, "rejected": False,
                                  "rejected_at": None, "emails": 0, "link": ""})
        it["emails"] += 1
        it["role"] = it["role"] or (e["role"] or "")
        if e["event"] == "offer":
            it.update(offer=True, offer_at=it["offer_at"] or e["received_at"], offer_email_id=it["offer_email_id"] or e["id"])
        elif e["link"] and not it["link"]:
            it["link"] = e["link"]
    for j in db.fetchall("SELECT id, company, title, status, status_changed_at, applied_at FROM jobs "
                         "WHERE status IN ('interviewing','offer')"):
        k = _key(j["id"], j["company"])
        if k not in items:                       # marked by hand, no email
            items[k] = {"key": k, "job_id": j["id"], "company": j["company"], "role": j["title"], "job_status": j["status"],
                        "first_at": j["status_changed_at"] or j["applied_at"], "email_id": None, "offer": False,
                        "offer_at": None, "offer_email_id": None, "rejected": False, "rejected_at": None, "emails": 0, "link": ""}
    for k in [k for k in items if k.startswith("co:")]:       # an unmatched email for a job you also have matched
        co = items[k]
        target = next((x for kk, x in items.items() if kk.startswith("job:") and company_norm(x["company"] or "") == k[3:]), None)
        if target:
            target["emails"] += co["emails"]
            target["first_at"] = min(filter(None, [target["first_at"], co["first_at"]]), default=None)
            target["link"] = target["link"] or co["link"]
            if co["offer"]:
                target.update(offer=True, offer_at=target["offer_at"] or co["offer_at"],
                              offer_email_id=target["offer_email_id"] or co["offer_email_id"])
            del items[k]
    for it in items.values():
        if it["job_status"] == "offer" and not it["offer"]:
            it["offer"] = True
        if it["job_status"] == "closed":
            it["rejected"] = True
    for r in db.fetchall("SELECT job_id, company, received_at FROM email_events WHERE event='rejection' AND " + LIVE):
        co = company_norm(r["company"] or "")
        hit = items.get(_key(r["job_id"], None)) if r["job_id"] else None
        hits = [hit] if hit else [x for x in items.values() if co and company_norm(x["company"] or "") == co
                                  and (not r["job_id"] or not x["job_id"])]
        for it in hits:
            if (r["received_at"] or "") >= (it["first_at"] or ""):
                it["rejected"] = True
                it["rejected_at"] = max(it["rejected_at"] or "", r["received_at"] or "")
    out = list(items.values())
    for it in out:
        it["state"] = "offer" if it["offer"] else "rejected" if it["rejected"] else "open"
    out.sort(key=lambda x: x["first_at"] or "", reverse=True)
    return out


def counts(items: list[dict] | None = None) -> dict:
    items = history() if items is None else items
    return {"interviews": len(items), "offers": sum(1 for i in items if i["offer"]),
            "rejected": sum(1 for i in items if i["rejected"] and not i["offer"]),
            "open": sum(1 for i in items if i["state"] == "open")}
