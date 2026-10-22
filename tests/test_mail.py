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
    db.upsert_job({"id": "j:1", "company": "Acme Games", "title": "Software Engineer", "status": "applied"})
    db.execute("INSERT INTO email_events (msg_id, received_at, sender, subject, snippet, company, role, event, state, job_id, created_at) "
               "VALUES ('m1', ?, 'noreply@acmegames.example.com', 'Your Acme Games Assessments', 'Hi, please finish four tasks.', 'Acme Games', "
               "'Software Engineer', 'assessment', 'applied', 'j:1', ?)", (db.now(), db.now()))
    eid = db.fetchone("SELECT id FROM email_events")["id"]
    db.add_intervention("j:1", "assessment", "Online assessment from Acme Games · due Mon Oct 26, 9:00 AM")
    c = app.test_client()
    for q in ("Acme Games", "Software", "%'\"", ""):
        assert c.get("/email", query_string={"q": q}).status_code == 200
    one = c.get(f"/email?id={eid}").get_data(as_text=True)
    assert "Showing one email" in one and "please finish four tasks" in one
    assert f'href="/email?id={eid}">See the email' in c.get("/inbox").get_data(as_text=True)


def test_forwarded_emails_from_your_own_address_are_read():
    from app.mailsync import likely_application_email as likely
    own = {"me@example.com"}
    subj = "Fwd: Acme & Company - Invitation to Solve, the Acme Game"
    assert likely("Ava Lee <me@example.com>", subj, own)                         # you forwarded it on purpose
    assert likely("Ava Lee <me@example.com>", "FW: hello", own)                  # Claude decides what it is
    assert not likely("Someone <other@example.com>", "Fwd: lunch on Friday", own)
    assert not likely("Ava Lee <me@example.com>", "Fwd: Job alert: 30 new jobs for you", own)
    assert likely("Acme Recruiting <noreply@example.com>", "Invitation to Solve, the Acme Game", own)


def test_forwarded_header_date_is_not_a_deadline():
    from app.assessments import parse_due
    body = ("---------- Forwarded message ---------\nFrom: Acme Recruiting <jobs@example.com>\n"
            "Date: Thu, 22 Oct 2026 at 10:50 AM\nSubject: Invitation to Solve\n\nYour testing period begins on "
            "Thursday, October 22, 2026 and ends on Wednesday, October 28, 2026.")
    due = parse_due("2026-10-28", "Acme invited you to its assessment game.", body, received_at="2026-10-22T14:56:03+00:00")
    from datetime import datetime
    local = datetime.fromisoformat(due).astimezone()
    assert (local.month, local.day, local.hour) == (10, 28, 23)              # end of the last day, not 10:50 AM on the 22nd


def test_long_tracking_links_dont_push_the_deadline_out_of_view():
    from app.mailsync import _for_claude
    body = "Dear Ava,\n" + ("<https://track.example.com/" + "x" * 900 + ">\n") * 6 + "Complete it by October 28, 2026."
    text = _for_claude(body)
    assert "October 28" in text and "[link]" in text
    short = "https://solve.example.com/launcher/?token=abc123"
    assert short in _for_claude(f"Your link: {short}")


def test_screening_questionnaires_count_as_job_emails():
    from app.mailsync import likely_application_email as likely
    sender = "Acme University <recruiting@careeralerts.example.edu>"
    assert likely(sender, "Congratulations! You are invited to our screening process", set())
    assert likely(sender, "Your Screening for Research Analyst has been successfully submitted", set())
    assert not likely("News <news@example.com>", "Cancer screening tips for the holidays", set())


def test_fix_type_moves_the_job_with_it(jobs_db):
    """Claude called a screening questionnaire an interview: fixing the type puts the job back and adds the assessment."""
    ev = {"account": "me@school.edu", "msg_id": "<s@x>", "received_at": "2026-10-15T13:26:04+00:00",
          "sender": "Stripe <recruiting@stripe.com>", "subject": "You are invited to our screening process",
          "body": "Submit by Oct 22nd, 2026 (11:59 PM EDT). 5 questions.", "event": "interview", "company": "Stripe",
          "role": "Software Engineer, New Grad", "link": "", "deadline": "", "summary": "Screening questions"}
    mailsync.apply_event(ev)
    assert db.get_job("g:1")["status"] == "interviewing"
    eid = db.fetchone("SELECT id FROM email_events WHERE msg_id='<s@x>'")["id"]
    row = mailsync.reclassify(eid, "assessment")
    assert row["state"] == "applied" and db.get_job("g:1")["status"] == "applied"
    assert not db.fetchone("SELECT 1 FROM interventions WHERE job_id='g:1' AND kind='interview' AND status='open'")
    assert db.fetchone("SELECT 1 FROM interventions WHERE job_id='g:1' AND kind='assessment' AND status='open'")
    new = db.fetchone("SELECT * FROM email_events WHERE msg_id='<s@x>'")
    assert new["event"] == "assessment" and new["due_at"].startswith("2026-10-23")   # 11:59 PM EDT = 03:59 UTC next day

    assert mailsync.reclassify(new["id"], "not_application")["state"] == "ignored"   # not about a job: hidden, change reversed
    assert db.fetchone("SELECT state FROM email_events WHERE msg_id='<s@x>'")["state"] == "ignored"
    assert mailsync.reclassify(new["id"], "bogus") is None


def test_email_page_has_fix_type_not_undo(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    db.upsert_job({"id": "g:1", "company": "Stripe", "title": "Engineer", "status": "applied"})
    mailsync.apply_event({"account": "a", "msg_id": "<r@x>", "received_at": "2026-10-18T15:00:00+00:00", "sender": "s",
                          "subject": "Your application", "body": "", "event": "rejection", "company": "Stripe",
                          "role": "Engineer", "link": "", "deadline": "", "summary": ""})
    from app.web import app
    html = app.test_client().get("/email").get_data(as_text=True)
    assert "Fix type" in html and ">Undo<" not in html and ">Ignore<" not in html
    eid = db.fetchone("SELECT id FROM email_events")["id"]
    r = app.test_client().post(f"/email/{eid}/type", data={"event": "interview"})
    assert r.status_code == 302 and db.get_job("g:1")["status"] == "interviewing"


def test_application_system_emails_count_even_with_just_a_job_title():
    from app.mailsync import likely_application_email as likely
    assert likely("acme@myworkday.com", "Business Insights Data Analyst", set())          # a rejection, subject = title
    assert likely("Acme <no-reply@us.greenhouse-mail.io>", "Software Engineer", set())
    assert not likely("LinkedIn <jobs-noreply@linkedin.com>", "Software Engineer", set())
    assert not likely("acme@myworkday.com", "Job alert: 30 new jobs for you", set())


def test_assessments_page_flags_ones_the_company_later_rejected(jobs_db):
    from app.web import app
    base = {"account": "a", "sender": "s", "company": "Stripe", "role": "Software Engineer, New Grad", "link": "", "deadline": ""}
    mailsync.apply_event({**base, "msg_id": "<oa@x>", "received_at": "2026-10-15T18:34:00+00:00", "subject": "Screening invite",
                          "event": "assessment", "summary": "Screening due Oct 18", "body": "due Oct 18, 2026"})
    db.execute("UPDATE email_events SET done_at='2026-10-15T19:51:00+00:00', done_how='done' WHERE msg_id='<oa@x>'")
    mailsync.apply_event({**base, "msg_id": "<rj@x>", "received_at": "2026-10-15T22:38:00+00:00", "subject": "Your application",
                          "event": "rejection", "summary": "Pursuing other candidates", "body": ""})
    assert db.get_job("g:1")["status"] == "closed"
    html = app.test_client().get("/assessments").get_data(as_text=True)
    i = html.index("Finished, dismissed, or closed")
    rest = html[i:]
    assert "Stripe" in rest and ">Done<" in rest and ">Rejected<" in rest
    assert ">undone<" not in app.test_client().get("/email").get_data(as_text=True)


def test_marking_an_assessment_rejected_keeps_the_assessment(jobs_db):
    from app.web import app
    from app import assessments
    mailsync.apply_event({"account": "a", "msg_id": "<oa2@x>", "received_at": "2026-10-15T18:34:00+00:00", "sender": "s",
                          "subject": "You are invited to our screening process", "company": "Stripe",
                          "role": "Software Engineer, New Grad", "link": "", "deadline": "2026-10-30",
                          "event": "assessment", "summary": "Screening", "body": ""})
    eid = db.fetchone("SELECT id FROM email_events WHERE msg_id='<oa2@x>'")["id"]
    assert [a["shown_company"] for a in assessments.pending()] == ["Stripe"]
    r = app.test_client().post(f"/email/{eid}/type", data={"event": "rejection"})
    assert r.status_code == 302
    assert db.fetchone("SELECT event FROM email_events WHERE id=?", (eid,))["event"] == "assessment"   # still an assessment
    assert db.get_job("g:1")["status"] == "closed"                                                   # job rejected
    assert assessments.pending() == []                                                               # no more reminders
    html = app.test_client().get("/assessments").get_data(as_text=True)
    assert ">Rejected<" in html[html.index("Finished, dismissed, or closed"):]
    em = app.test_client().get("/email").get_data(as_text=True)
    assert "Rejected (keeps the assessment)" in em or ">Rejected</span>" in em
    app.test_client().post(f"/email/{eid}/type", data={"event": "rejection"})                       # twice: no duplicate
    assert db.count("email_events", "msg_id='<oa2@x>#rejected'") == 1


def test_matching_from_the_whole_email(jobs_db, monkeypatch):
    db.upsert_job({"id": "workday:peak6:R-110635", "company": "PEAK6", "title": "Software Engineer I", "status": "rejected"})
    db.upsert_job({"id": "g:9", "company": "Chime", "title": "Software Engineer, Trust & Safety", "status": "applied"})
    db.upsert_job({"id": "g:10", "company": "Chime", "title": "Data Analyst", "status": "applied"})
    asked = []
    monkeypatch.setattr(mailsync, "ask_claude_to_match", lambda ev, text, cands: asked.append([c["id"] for c in cands]) or
                        next(c for c in cands if c["id"] == "g:9"))
    # requisition number in the text + company in the sender: clear, no Claude; a held job moves to Applied
    ev = {"account": "a", "msg_id": "<p@x>", "received_at": "2026-10-20T12:00:00+00:00", "sender": "peak6group@myworkday.com",
          "subject": "Thank You For Applying!", "company": "", "role": "", "link": "", "deadline": "", "event": "confirmation",
          "summary": "", "body": "Thanks for applying to Software Engineer I (R-110635)."}
    assert mailsync.apply_event(ev)["state"] == "applied" and not asked
    j = db.get_job("workday:peak6:R-110635")
    assert j["status"] == "applied" and j["applied_at"].startswith("2026-10-20")
    # two jobs at the company, no title in the email: Claude picks from just those
    ev2 = {**ev, "msg_id": "<c@x>", "sender": "no-reply@chime.com", "subject": "Your Application to Chime", "event": "rejection",
           "body": "We won't be moving forward with your application for the Trust & Safety team."}
    assert mailsync.apply_event(ev2)["job_id"] == "g:9" and set(asked[0]) == {"g:9", "g:10"}
    # nothing plausible: stays unmatched, no Claude call
    n = len(asked)
    ev3 = {**ev, "msg_id": "<n@x>", "sender": "no-reply@nobody.example", "subject": "Thanks for applying", "company": "Zzyzx Co", "body": "hi"}
    assert mailsync.apply_event(ev3)["state"] == "unmatched" and len(asked) == n


def test_rematch_unmatched(jobs_db, monkeypatch):
    mailsync.apply_event({"account": "a", "msg_id": "<u@x>", "received_at": "2026-10-20T12:00:00+00:00", "sender": "s@nowhere.example",
                          "subject": "Update", "company": "Unknown", "role": "", "link": "", "deadline": "", "event": "rejection",
                          "summary": "", "body": ""})
    assert db.count("email_events", "state='unmatched'") == 1
    monkeypatch.setattr(mailsync, "full_text", lambda row: "Your application for Backend Engineer at Acme Corp was not selected.")
    st = mailsync.rematch_unmatched(lambda *a: None, use_claude=False)
    assert st == {"checked": 1, "matched": 1, "left": 0}
    assert db.get_job("g:3")["status"] == "closed"                     # Acme Corp backend job, found from the full text
