"""Online assessments: exact due times, matched or not, the greeting, Needs you, and reminders."""
from datetime import datetime, timedelta, timezone

import pytest

from app import assessments, db


def test_parse_due_reads_time_and_zone_from_the_email():
    # the two real formats: "by 6:00am PT on October 26, 2026" and "due October 25, 2026 at 8:41 PM EDT"
    assert assessments.parse_due("2026-10-26", "finish four online assessments by 6:00am PT on October 26, 2026.") == "2026-10-26T13:00+00:00"
    assert assessments.parse_due("2026-10-25", "due October 25, 2026 at 8:41 PM EDT.") == "2026-10-26T00:41+00:00"
    assert assessments.parse_due("2026-10-25T20:41:00-04:00") == "2026-10-26T00:41+00:00"        # Claude's ISO answer
    assert assessments.parse_due(None, "Please complete it by Nov 3 at 11:59 pm ET", received_at="2026-10-20T10:00:00+00:00") == "2026-11-04T04:59+00:00"
    assert assessments.parse_due(None, "no date here") is None
    no_time = assessments.parse_due("2026-10-30", "complete it soon")                              # end of that local day
    assert no_time and datetime.fromisoformat(no_time).astimezone().strftime("%H:%M") == "23:59"


@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    db.upsert_job({"id": "j:roblox", "company": "Roblox", "title": "Software Engineer", "status": "applied"})
    now = datetime.now(timezone.utc)

    def add(mid, company, event, due=None, job_id=None, state="applied", received=None):
        db.execute("INSERT INTO email_events (msg_id, received_at, company, role, event, summary, state, job_id, due_at, link, created_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (mid, (received or now - timedelta(hours=5)).isoformat(), company, "Analyst", event, f"{company} {event}",
                    state, job_id, due.isoformat(timespec="minutes") if due else None, "https://assess.example/x", db.now()))
        return db.fetchone("SELECT id FROM email_events WHERE msg_id=?", (mid,))["id"]
    return now, add


def test_pending_includes_unmatched_soonest_first_and_marks_done(data):
    now, add = data
    r = add("m1", "Roblox", "assessment", now + timedelta(days=6), job_id="j:roblox", state="applied")
    o = add("m2", "OneMain Financial", "assessment", now + timedelta(days=5), state="unmatched")   # applied outside the app
    db.add_intervention("j:roblox", "assessment", "Online assessment from Roblox · due 2026-10-26")
    p = assessments.pending()
    assert [a["shown_company"] for a in p] == ["OneMain Financial", "Roblox"]
    assessments.sync_todos()
    titles = sorted(t["title"] for t in db.fetchall("SELECT title FROM interventions WHERE kind='assessment' AND status='open'"))
    assert len(titles) == 2 and all(" · due " in t for t in titles) and not any("(in " in t for t in titles)
    assessments.sync_todos()                                                    # idempotent
    assert db.count("interventions", "kind='assessment' AND status='open'") == 2
    assessments.mark([o])
    assert [a["shown_company"] for a in assessments.pending()] == ["Roblox"]
    assert db.count("interventions", "kind='assessment' AND status='open' AND job_id IS NULL") == 0
    assessments.mark([r], "skipped")
    assert assessments.pending() == [] and db.count("interventions", "kind='assessment' AND status='open'") == 0


def test_reminder_emails_and_moved_on_processes(data):
    now, add = data
    add("m1", "Acme", "assessment", now + timedelta(days=2), received=now - timedelta(days=3))
    add("m1b", "Acme", "assessment", now + timedelta(days=2), received=now - timedelta(days=1))   # a reminder email: one entry
    add("m2", "Globex", "assessment", now + timedelta(days=3), received=now - timedelta(days=4))
    add("m3", "Globex", "interview", received=now - timedelta(days=1))                          # they moved on: done
    add("m4", "Old Co", "assessment", now - timedelta(days=3))                                 # long overdue: dropped
    p = assessments.pending()
    assert [a["shown_company"] for a in p] == ["Acme"] and len(p[0]["ids"]) == 2


def test_reminders_daily_from_9am_and_3_hours_before(data):
    now, add = data
    morning = (datetime.now().astimezone() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    add("m1", "Acme", "assessment", morning.astimezone(timezone.utc) + timedelta(hours=2), received=morning - timedelta(days=1))
    sent = []
    notify = lambda title, text: sent.append((title, text)) or True
    out = assessments.remind_tick(now=morning.astimezone(timezone.utc), notify=notify)
    assert "daily" in out and any(s.startswith("soon:") for s in out)
    assert assessments.remind_tick(now=morning.astimezone(timezone.utc), notify=notify) == []   # once each
    early = assessments.remind_tick(now=(morning.replace(hour=7) - timedelta(days=1)).astimezone(timezone.utc), notify=notify)
    assert "daily" not in early                                                 # before 9 AM: no daily reminder


def test_greeting_names_every_pending_assessment(data):
    from app import greeting
    now, add = data
    add("m1", "Roblox", "assessment", now + timedelta(days=6), job_id="j:roblox")
    add("m2", "OneMain Financial", "assessment", now + timedelta(days=5), state="unmatched")
    text = greeting.greet(name="Manas")["text"]
    assert text.startswith("📝 2 assessments to finish: OneMain Financial (") and "Roblox (" in text
    db.execute("DELETE FROM email_events WHERE company='Roblox'")
    db.execute("UPDATE email_events SET due_at=? WHERE company='OneMain Financial'", ((now + timedelta(hours=20)).isoformat(),))
    assert greeting.greet(name="Manas")["text"].startswith("📝 Your OneMain Financial assessment is due ")


def test_dashboard_tile_and_card(data, monkeypatch):
    now, add = data
    a = add("m2", "OneMain Financial", "assessment", now + timedelta(hours=20), state="unmatched")
    from app.web import app
    c = app.test_client()
    html = c.get("/").get_data(as_text=True)
    assert "Online assessments to finish" in html and 'id="assessments"' in html and "tile-alert" in html
    assert "Open assessment" in html and "OneMain Financial" in html
    assert c.post(f"/assessments/{a}/done").status_code == 302
    assert 'id="assessments"' not in c.get("/").get_data(as_text=True)
    assert c.post(f"/assessments/{a}/done").status_code == 404
