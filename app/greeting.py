"""The one-line greeting above the dashboard KPIs: your name, the time of day on your computer, and your progress.

The name comes from profile/applicant.json ("preferred_name", else the first word of "name"), else from the
master prompt's "Name on all documents:" line. Set it on the Settings page. It lives in profile/ (gitignored).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from . import db
from .config import APPLICANT_FILE, SYSTEM_PROMPT_FILE

PLACEHOLDER = re.compile(r"^\[?\s*your\s+name\s*\]?$", re.I)


def user_name() -> str:
    """First name to greet, or '' if the app doesn't know it yet."""
    try:
        a = json.loads(APPLICANT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        a = {}
    for v in (a.get("preferred_name"), a.get("name")):
        v = (v or "").strip()
        if v and not PLACEHOLDER.match(v):
            return v.split()[0]
    try:
        m = re.search(r"Name on all documents[^:\n]*:\s*([^\n(]+)", SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")[:20000])
        if m and "[" not in m.group(1):
            return m.group(1).strip().split()[0]
    except OSError:
        pass
    return ""


def set_name(name: str) -> None:
    try:
        a = json.loads(APPLICANT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        a = {}
    a["preferred_name"] = name.strip()
    if not a.get("name") or PLACEHOLDER.match(a.get("name", "")):
        a["name"] = name.strip()
    APPLICANT_FILE.write_text(json.dumps(a, indent=2, ensure_ascii=False), encoding="utf-8")


def stats(today: date | None = None) -> dict:
    """Progress numbers in your local days."""
    today = today or datetime.now().astimezone().date()
    per_day: dict[date, int] = {}
    for r in db.fetchall("SELECT applied_at FROM jobs WHERE applied_at IS NOT NULL"):
        d = datetime.fromisoformat(r["applied_at"]).astimezone().date()
        per_day[d] = per_day.get(d, 0) + 1
    streak, d = 0, today if per_day.get(today) else today - timedelta(days=1)
    while per_day.get(d):
        streak += 1
        d -= timedelta(days=1)
    week = sum(n for d, n in per_day.items() if today - timedelta(days=6) <= d <= today)
    prev_week = sum(n for d, n in per_day.items() if today - timedelta(days=13) <= d <= today - timedelta(days=7))
    past_best = max([n for d, n in per_day.items() if d < today] or [0])
    since = (datetime.combine(today - timedelta(days=2), datetime.min.time()).astimezone()).isoformat()
    # recent offers / interviews / assessments whose Needs-you item you haven't closed yet
    fresh = {r["event"]: r for r in db.fetchall(
        "SELECT e.event, coalesce(j.company, e.company) AS company FROM email_events e JOIN jobs j ON j.id=e.job_id "
        "WHERE e.state='applied' AND e.event IN ('offer','interview','assessment') AND e.received_at >= ? "
        "AND EXISTS (SELECT 1 FROM interventions i WHERE i.job_id=e.job_id AND i.kind=e.event AND i.status='open') "
        "ORDER BY e.received_at", (since,))}
    return {
        "today": per_day.get(today, 0), "week": week, "prev_week": prev_week, "total": sum(per_day.values()),
        "streak": streak, "past_best": past_best, "ready": db.count("jobs", "status='ready'"),
        "interviewing": db.count("jobs", "status IN ('interviewing','offer')"),
        "offer": fresh.get("offer"), "interview": fresh.get("interview"), "assessment": fresh.get("assessment"),
        "assessments": _pending_assessments(),
    }


def _pending_assessments() -> list[dict]:
    try:
        from . import assessments
        return [a for a in assessments.pending() if not a["overdue"]]
    except Exception:
        return []


def _assessment_line(items: list[dict]) -> str:
    from .assessments import due_label, short_when
    if len(items) == 1:
        a = items[0]
        return f"📝 Your {a['shown_company']} assessment is {due_label(a)}. Knock it out while you're sharp."
    names = [f"{a['shown_company']} ({short_when(a)})" for a in items[:2]]
    more = f", and {len(items) - 2} more" if len(items) > 2 else ""
    return f"📝 {len(items)} assessments to finish: {names[0]} and {names[1]}{more}. Start with the first one."


def _when(hour: int) -> tuple[str, str, str]:
    """(greeting, flavor, emoji) for the hour on your computer's clock."""
    if hour < 4:
        return "Still up", "late-night", "🌙"
    if hour < 7:
        return "Up early", "early", "🌅"
    if hour < 12:
        return "Good morning", "morning", "☀️"
    if hour < 17:
        return "Good afternoon", "afternoon", "🌤️"
    if hour < 21:
        return "Good evening", "evening", "🌆"
    return "Working late", "night", "🌙"


def greet(now: datetime | None = None, s: dict | None = None, name: str | None = None) -> dict:
    """{'title': 'Good morning, Manas ☀️', 'text': '📝 Acme Games sent an assessment. …'}.
    Celebrates real progress, never invents any."""
    now = now or datetime.now()
    s = s if s is not None else stats(now.date())
    name = user_name() if name is None else name
    hello, flavor, sky = _when(now.hour)
    mark = "?" if flavor == "late-night" else ""
    title = f"{hello}, {name}{mark} {sky}" if name else f"{hello}{mark} {sky}"
    pl = lambda n, word: f"{n} {word}{'' if n == 1 else 's'}"
    return {"title": title, "text": _message(s, flavor, pl)}


def _message(s: dict, flavor: str, pl) -> str:
    # 1. Real wins first: offers, interviews, assessments from the last few days (still open in Needs you)
    if s.get("offer"):
        return f"🎉 An offer from {s['offer']['company']}! Take a moment, you earned this."
    pend = s.get("assessments") or []
    if pend and pend[0].get("urgent"):                     # due within 36 hours beats everything but an offer
        return _assessment_line(pend)
    if s.get("interview"):
        return f"📞 {s['interview']['company']} wants to talk to you. Time to prep your stories."
    if pend:
        return _assessment_line(pend)
    if s.get("assessment"):
        return f"📝 {s['assessment']['company']} sent an assessment. Knock it out while you're sharp."
    # 2. Today's momentum
    if s["today"] and s["today"] > s["past_best"] >= 3:
        return f"🏆 {pl(s['today'], 'application')} today, a new personal best. Keep it rolling."
    if s["streak"] >= 3:
        tail = f", and {s['ready']} more ready to send" if s["ready"] else ""
        return f"🔥 {s['streak']}-day application streak{tail}. Consistency wins this game."
    if s["today"]:
        if flavor == "late-night":
            return f"🦉 {pl(s['today'], 'application')} in today. The night shift counts, now get some sleep."
        if flavor == "early":
            return f"🐦 {pl(s['today'], 'application')} before most people's alarms. Early birds get interviews."
        return f"✅ {pl(s['today'], 'application')} in today, {s['week']} this week. Every one is a door you opened."
    # 3. Nothing sent yet today
    if s["ready"]:
        lead = {"late-night": "While you can't sleep: ", "early": "Quiet hours are perfect: "}.get(flavor, "")
        return f"🚀 {lead}{pl(s['ready'], 'tailored application')} ready. About 10 minutes sends them all."
    if s["week"] and s["week"] > s["prev_week"]:
        return f"📈 {s['week']} applications this week, up from {s['prev_week']} last week. The numbers are moving."
    if s["total"]:
        return f"🧭 {pl(s['total'], 'application')} so far, and the autopilot is out finding the next ones."
    return "✨ Your autopilot is ready. Click Run now and let's find your first roles."


def line(now: datetime | None = None, s: dict | None = None, name: str | None = None) -> str:
    """The greeting as one sentence (title + message)."""
    g = greet(now, s, name)
    return f"{g['title']} {g['text']}"
