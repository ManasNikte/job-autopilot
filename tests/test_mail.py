import pytest

from app import db, mailsync


@pytest.fixture()
def jobs_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    for jid, co, title, st in [("g:1", "Stripe", "Software Engineer, New Grad", "applied"),
                               ("g:2", "Stripe", "Data Engineer", "applied"),
                               ("g:3", "Acme Corp", "Backend Engineer", "ready")]:
        db.upsert_job({"id": jid, "company": co, "title": title, "status": st, "applied_at": "2026-10-10T12:00:00+00:00"})
    return db


@pytest.mark.parametrize("sender,subject,expected", [
    ("Stripe <no-reply@greenhouse.io>", "Thank you for applying to Stripe", True),
    ("Acme Talent <talent@acme.com>", "Update on your application", True),
    ("HackerRank <support@hackerrank.com>", "Your coding assessment for Acme", True),
    ("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>", "30+ new jobs for you", False),
    ("Mom <mom@example.com>", "Dinner on Sunday?", False),
])
def test_rule_filter(sender, subject, expected):
    assert mailsync.likely_application_email(sender, subject) is expected


def test_status_only_moves_forward():
    assert mailsync._new_status("applied", "rejection") == "closed"
    assert mailsync._new_status("closed", "confirmation") is None          # late confirmation never undoes a rejection
    assert mailsync._new_status("ready", "confirmation") == "applied"      # forgot to click "I submitted it"
    assert mailsync._new_status("closed", "interview") == "interviewing"
    assert mailsync._new_status("applied", "assessment") is None           # stays Applied; becomes a Needs-you item


def test_match_picks_company_and_closest_title(jobs_db):
    assert mailsync.match_job("Stripe, Inc.", "Data Engineer", "")["id"] == "g:2"
    assert mailsync.match_job("", "", "Recruiting <jobs@acmecorp.com>")["id"] == "g:3"
    assert mailsync.match_job("Unknown Co", "", "x@gmail.com") is None


def test_apply_undo_and_assign(jobs_db):
    ev = {"account": "me@gmail.com", "msg_id": "<1@x>", "received_at": "2026-10-18T15:00:00+00:00",
          "sender": "Stripe <no-reply@greenhouse.io>", "subject": "Your application", "body": "We regret...",
          "event": "rejection", "company": "Stripe", "role": "Software Engineer, New Grad", "link": "", "deadline": "", "summary": "Rejected"}
    row = mailsync.apply_event(ev)
    assert row["state"] == "applied" and db.get_job("g:1")["status"] == "closed"
    eid = db.fetchone("SELECT id FROM email_events WHERE msg_id='<1@x>'")["id"]
    assert mailsync.undo(eid) and db.get_job("g:1")["status"] == "applied"

    oa = {**ev, "msg_id": "<2@x>", "event": "assessment", "company": "Mystery LLC", "role": "", "link": "https://hr.example/oa", "deadline": "2026-10-25"}
    assert mailsync.apply_event(oa)["state"] == "unmatched"
    eid2 = db.fetchone("SELECT id FROM email_events WHERE msg_id='<2@x>'")["id"]
    assert mailsync.assign(eid2, "g:3")
    assert db.get_job("g:3")["status"] == "applied"                        # ready -> applied on assessment
    iv = db.fetchone("SELECT * FROM interventions WHERE job_id='g:3' AND kind='assessment'")
    assert iv and "due 2026-10-25" in iv["title"]


def test_email_page_search_and_single_email_view(tmp_path, monkeypatch):
    """Searching used to crash: the query joins jobs, whose company/title columns made bare names ambiguous."""
    from app import db
    from app.web import app
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    db.upsert_job({"id": "j:1", "company": "Roblox", "title": "Software Engineer", "status": "applied"})
    db.execute("INSERT INTO email_events (msg_id, received_at, sender, subject, snippet, company, role, event, state, job_id, created_at) "
               "VALUES ('m1', ?, 'noreply@roblox.com', 'Your Roblox Assessments', 'Hi, please finish four tasks.', 'Roblox', "
               "'Software Engineer', 'assessment', 'applied', 'j:1', ?)", (db.now(), db.now()))
    eid = db.fetchone("SELECT id FROM email_events")["id"]
    db.add_intervention("j:1", "assessment", "Online assessment from Roblox · due Mon Oct 26, 9:00 AM")
    c = app.test_client()
    for q in ("Roblox", "Software", "%'\"", ""):
        assert c.get("/email", query_string={"q": q}).status_code == 200
    one = c.get(f"/email?id={eid}").get_data(as_text=True)
    assert "Showing one email" in one and "please finish four tasks" in one
    assert f'href="/email?id={eid}">See the email' in c.get("/inbox").get_data(as_text=True)
