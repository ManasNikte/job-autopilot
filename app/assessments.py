"""Online assessments from your inbox: what's pending, exactly when each is due, and daily reminders.

One list feeds the dashboard KPI, the "Assessments to finish" card, the greeting, Needs you, the phone
dashboard, and desktop notifications. It includes assessments from companies you applied to outside this app
(emails that didn't match a job), keeps the exact due time (e.g. "8:41 PM EDT"), and treats an assessment as
finished when you mark it done, or when a later email from that company (interview, offer, rejection) shows the
process moved on.
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db
from .h1b import norm as company_norm

TZ = {"PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
      "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
      "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
      "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
      "UTC": "UTC", "GMT": "UTC"}
_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?(?:\s*\(?\s*(PT|PST|PDT|MT|MST|MDT|CT|CST|CDT|ET|EST|EDT|UTC|GMT)\b)?",
                   re.I)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?", re.I)


def parse_due(deadline: str | None, *texts: str, received_at: str | None = None) -> str | None:
    """Best due moment as an ISO UTC timestamp. Uses an ISO deadline if Claude gave one with a time; otherwise the
    date (from `deadline`, or the first month-day in the texts) plus the first clock time with a time zone found
    in the texts ("by 6:00am PT", "at 8:41 PM EDT"). No time stated: end of that day on this computer."""
    d: date | None = None
    if deadline:
        try:
            dt = datetime.fromisoformat(deadline.strip().replace("Z", "+00:00"))
            if "T" in deadline and dt.tzinfo:
                return dt.astimezone(timezone.utc).isoformat(timespec="minutes")
            d = dt.date()
        except ValueError:
            d = None
    blob = " ".join(t for t in texts if t)
    if d is None:
        m = _DATE.search(blob)
        if m:
            base = datetime.fromisoformat(received_at).date() if received_at else date.today()
            year = int(m.group(3)) if m.group(3) else base.year
            try:
                d = date(year, _MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
                if not m.group(3) and d < base - timedelta(days=30):      # "Jan 5" written in December
                    d = d.replace(year=year + 1)
            except ValueError:
                d = None
    if d is None:
        return None
    t, zone = None, None
    for m in _TIME.finditer(blob):
        h, mi, ap, tz = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower(), m.group(4)
        if 1 <= h <= 12 and mi < 60:
            t = time(h % 12 + (12 if ap == "p" else 0), mi)
            zone = ZoneInfo(TZ[tz.upper()]) if tz else None
            break
    if t is None:
        return datetime.combine(d, time(23, 59)).astimezone().astimezone(timezone.utc).isoformat(timespec="minutes")
    local = datetime.combine(d, t, tzinfo=zone) if zone else datetime.combine(d, t).astimezone()
    return local.astimezone(timezone.utc).isoformat(timespec="minutes")


def backfill() -> int:
    """Give older assessment emails a due time (runs at startup; cheap)."""
    n = 0
    for r in db.fetchall("SELECT id, deadline, summary, snippet, received_at FROM email_events "
                         "WHERE event='assessment' AND due_at IS NULL"):
        due = parse_due(r["deadline"], r["summary"] or "", r["snippet"] or "", received_at=r["received_at"])
        if due:
            db.execute("UPDATE email_events SET due_at=? WHERE id=?", (due, r["id"]))
            n += 1
    return n


def pending(now: datetime | None = None) -> list[dict]:
    """Assessments still to do, soonest first, one per company (the latest email wins). Overdue ones stay for a
    day so you can mark them, then drop off."""
    now = now or datetime.now(timezone.utc)
    rows = db.fetchall(
        "SELECT e.id, e.company, e.role, e.link, e.deadline, e.due_at, e.received_at, e.summary, e.job_id, e.state, "
        "coalesce(j.company, e.company) AS shown_company, coalesce(j.title, e.role) AS shown_role "
        "FROM email_events e LEFT JOIN jobs j ON j.id = e.job_id "
        "WHERE e.event='assessment' AND e.state NOT IN ('ignored','undone') AND e.done_at IS NULL "
        "ORDER BY e.received_at")
    later = {}
    for r in db.fetchall("SELECT company, received_at FROM email_events WHERE event IN ('interview','offer','rejection') "
                         "AND state NOT IN ('ignored','undone')"):
        k = company_norm(r["company"] or "")
        later[k] = max(later.get(k, ""), r["received_at"] or "")
    by_company: dict[str, dict] = {}
    for r in rows:
        k = company_norm(r["shown_company"] or r["company"] or "") or f"#{r['id']}"
        if later.get(k, "") > (r["received_at"] or ""):
            continue                                    # they've since invited you to interview, offered, or declined
        by_company[k] = {**r, "ids": by_company.get(k, {}).get("ids", []) + [r["id"]]}
    out = []
    for a in by_company.values():
        due = datetime.fromisoformat(a["due_at"]) if a.get("due_at") else None
        if due and due < now - timedelta(days=1):
            continue
        left = (due - now).total_seconds() if due else None
        out.append({**a, "due": due, "overdue": bool(due and due < now), "seconds_left": left,
                    "urgent": left is not None and left < 36 * 3600})
    out.sort(key=lambda a: (a["due"] is None, a["due"] or now))
    return out


def due_label(a: dict, now: datetime | None = None) -> str:
    """'due Sun Oct 25, 8:41 PM (in 2 days)' in this computer's time zone."""
    if not a.get("due"):
        return "no due date in the email"
    local = a["due"].astimezone()
    now_local = (now or datetime.now(timezone.utc)).astimezone()
    days = (local.date() - now_local.date()).days
    when = local.strftime("%a %b %-d, %-I:%M %p")
    if a["overdue"]:
        return f"was due {when}"
    left = a["seconds_left"]
    rel = (f"in {int(left // 3600)}h {int(left % 3600 // 60)}m" if left < 6 * 3600 else
           "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days")
    return f"due {when} ({rel})"


def short_when(a: dict) -> str:
    return a["due"].astimezone().strftime("%a %-I:%M %p") if a.get("due") else "no date"


def mark(ids: list[int], how: str = "done") -> None:
    """You finished it (or decided not to): stop reminding, close the Needs you item."""
    for i in ids:
        db.execute("UPDATE email_events SET done_at=?, done_how=? WHERE id=?", (db.now(), how, i))
        ev = db.fetchone("SELECT job_id, company FROM email_events WHERE id=?", (i,))
        if ev and ev["job_id"]:
            db.resolve_interventions(ev["job_id"], ("assessment",), "assessment done" if how == "done" else "not doing it")
        elif ev:
            db.execute("UPDATE interventions SET status='done', resolved_at=? WHERE job_id IS NULL AND kind='assessment' "
                       "AND title LIKE ? AND status='open'", (db.now(), f"Online assessment from {ev['company']} ·%"))


def sync_todos() -> None:
    """Make sure every pending assessment, matched to a job or not, is on Needs you with its due time."""
    for a in pending():
        when = f"due {a['due'].astimezone().strftime('%a %b %-d, %-I:%M %p')}" if a.get("due") else "no due date"
        title = f"Online assessment from {a['shown_company']} · {when}"
        detail = f"{a.get('summary') or ''}\n{a.get('link') or ''}".strip()
        if a.get("job_id"):
            db.execute("UPDATE interventions SET title=? WHERE job_id=? AND kind='assessment' AND status='open'",
                       (title, a["job_id"]))
            if not db.fetchone("SELECT 1 FROM interventions WHERE job_id=? AND kind='assessment' AND status='open'", (a["job_id"],)):
                db.add_intervention(a["job_id"], "assessment", title, detail)
        else:
            db.execute("UPDATE interventions SET title=? WHERE job_id IS NULL AND kind='assessment' AND status='open' "
                       "AND title LIKE ?", (title, f"Online assessment from {a['shown_company']} ·%"))
            if not db.fetchone("SELECT 1 FROM interventions WHERE job_id IS NULL AND kind='assessment' AND status='open' "
                               "AND title LIKE ?", (f"Online assessment from {a['shown_company']} ·%",)):
                db.add_intervention(None, "assessment", title, detail)


# ----------------------------------------------------------------------------- reminders

def _notify(title: str, text: str) -> bool:
    """A desktop notification (macOS). Elsewhere the dashboard banner does the reminding."""
    if sys.platform != "darwin":
        return False
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(text)}" with title "{esc(title)}" sound name "Glass"'],
                       capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def reminder_text(items: list[dict]) -> str:
    if len(items) == 1:
        a = items[0]
        return f"{a['shown_company']} assessment {due_label(a)}."
    first = items[0]
    return f"{len(items)} assessments to finish. Next: {first['shown_company']}, {due_label(first)}."


def remind_tick(now: datetime | None = None, notify=_notify) -> list[str]:
    """Called every few minutes. Once a day from 9 AM (local), and once when one is due within 3 hours."""
    now = now or datetime.now(timezone.utc)
    items = [a for a in pending(now) if not a["overdue"]]
    if not items:
        return []
    sent = []
    local = now.astimezone()
    day_key = f"assess_daily:{local:%Y-%m-%d}"
    if local.hour >= 9 and not db.kv_get(day_key):
        db.kv_set(day_key, "1")
        if notify("Assessments to finish", reminder_text(items)):
            sent.append("daily")
    for a in items:
        if a["seconds_left"] is not None and a["seconds_left"] < 3 * 3600:
            key = f"assess_soon:{a['id']}"
            if not db.kv_get(key):
                db.kv_set(key, "1")
                if notify(f"{a['shown_company']} assessment due soon", due_label(a, now).capitalize() + "."):
                    sent.append(f"soon:{a['id']}")
    return sent
