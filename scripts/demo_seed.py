"""Fill a separate demo database with realistic sample data so you can explore the UI without Claude.

    python scripts/demo_seed.py            # writes demo/data and demo/profile (gitignored)
    JOB_AUTOPILOT_DATA=demo/data JOB_AUTOPILOT_PROFILE=demo/profile python -m app.cli serve

Everything here is made up (the candidate is the fictional "Jordan Lee" from the test fixture). Your real
data/ and profile/ folders are never touched. Don't click "Run now" in demo mode unless Claude is set up:
it fetches and screens real postings into the demo database.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"
if (DEMO / "data").exists():
    shutil.rmtree(DEMO / "data")
os.environ["JOB_AUTOPILOT_DATA"] = str(DEMO / "data")
os.environ["JOB_AUTOPILOT_PROFILE"] = str(DEMO / "profile")
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402  (paths above must be set before the app is imported)
from app.config import OUTPUT_DIR  # noqa: E402
from app.render import pdfgen  # noqa: E402

random.seed(7)
NOW = datetime.now(timezone.utc)
FIXTURE = json.loads((ROOT / "tests/fixtures/sample_tailor.json").read_text())


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def local_today_at(hour: int) -> datetime:
    """A time earlier today in local time, so 'Posted today' fills no matter when the demo is seeded."""
    now = datetime.now().astimezone()
    return min(now - timedelta(minutes=20), now.replace(hour=hour, minute=random.randint(0, 59))).astimezone(timezone.utc)


ROLES = {
    "Software Engineer": ["Software Engineer, New Grad", "Software Engineer I, Backend", "Software Engineer, Platform",
                          "Full Stack Engineer", "Software Engineer, Payments Infrastructure", "Backend Engineer, API"],
    "Data Engineer": ["Data Engineer", "Data Engineer I, Analytics Platform", "Analytics Engineer"],
    "ML / AI Engineer": ["Machine Learning Engineer, Early Career", "AI Engineer, Applied LLMs", "ML Engineer, Ranking"],
    "Data Scientist": ["Data Scientist, Product", "Data Scientist I"],
    "Business / Data Analyst": ["Data Analyst", "Business Intelligence Analyst"],
}
COMPANIES = [("Stripe", "greenhouse", "San Francisco, CA"), ("Datadog", "greenhouse", "New York, NY"),
             ("Databricks", "greenhouse", "San Francisco, CA"), ("Ramp", "ashby", "New York, NY"),
             ("Notion", "ashby", "San Francisco, CA"), ("Figma", "greenhouse", "San Francisco, CA"),
             ("Robinhood", "greenhouse", "Menlo Park, CA"), ("Plaid", "lever", "San Francisco, CA"),
             ("Cloudflare", "greenhouse", "Austin, TX"), ("MongoDB", "greenhouse", "New York, NY"),
             ("Twilio", "greenhouse", "Remote, US"), ("Brex", "greenhouse", "Seattle, WA"),
             ("Airtable", "greenhouse", "San Francisco, CA"), ("Snowflake", "ashby", "Bellevue, WA"),
             ("Duolingo", "greenhouse", "Pittsburgh, PA"), ("Instacart", "greenhouse", "Remote, US"),
             ("Mercury", "greenhouse", "Remote, US"), ("Vercel", "ashby", "Remote, US"),
             ("Retool", "ashby", "San Francisco, CA"), ("Gusto", "greenhouse", "Denver, CO")]
STARTUPS = {"Ramp": "Series D", "Mercury": "Series C", "Vercel": "Series E", "Retool": "Series C"}
SOURCES = ["greenhouse", "ashby", "lever", "jobright", "simplify", "hn"]


def screen(company: str, title: str, verdict: str, score: int) -> dict:
    risky = verdict != "APPLY"
    return {
        "company_name": company, "job_title": title, "verdict": verdict, "match_score": score,
        "score_arithmetic": f"Base 6 · +2 core stack match (Python, SQL, AWS) · +1 shipped production pipelines · "
                            f"{'-1 asks 3+ years' if risky else '+0 seniority fits'} = {score}/10",
        "role_type": "Backend / data platform",
        "why": ["Python + AWS pipeline work maps directly to the team's ingestion stack",
                "A tested LLM agent project covers the 'automation' line in the JD",
                "Entry-level title with 0–2 years asked"],
        "deal_breakers": ["Prefers 3+ years of production on-call experience"] if risky else [],
        "recruiter_six_second_read": "Data engineer with real AWS pipelines and a recent agent project; clean one-pager, US-based.",
        "biggest_risk": "Years-of-experience screen-out at the ATS stage" if risky else "High applicant volume on day one",
        "eligibility": {"visa": {"result": "PASS", "note": "Sponsorship history on file; JD silent"},
                        "degree": {"result": "PASS", "note": "BS/MS in CS or related"},
                        "dealbreakers": {"result": "RISK" if risky else "PASS", "note": "3+ years preferred" if risky else "None found"},
                        "location_start": {"result": "PASS", "note": "US office, flexible start"}},
        "experience_required": "1–3 years" if risky else "0–2 years",
        "sponsorship_signal": random.choice(["unclear", "explicit_yes", "unclear"]),
        "competition_level": random.choice(["Medium", "High", "Very High"]),
        "nearest_alternative": "", "missing_info": [],
    }


def tailor(company: str, title: str) -> dict:
    t = json.loads(json.dumps(FIXTURE))
    t.update(company_slug=company, exact_role_title=title, keyword_relevancy=random.randint(78, 93),
             selection_reason="Picked the browser agent and RAG projects for the automation and LLM lines in the JD.")
    t["rw_score"] = {"impact": 22, "brevity": 21, "style": 23, "ats": 18, "total": 84, "failed_checks": []}
    t["teardown"] = {
        "skim_test": "Within six seconds: data/backend engineer, AWS pipelines with real numbers, a recent agent project. Passes.",
        "credibility_flags": [], "timeline_questions": ["Overlap between research assistantship and project dates"],
        "hardest_questions": [
            {"question": "Walk me through the Glue job that cut reporting from 2 days to 15 minutes.",
             "answer_strategy": "Start with the old manual flow, then partitioning + scheduled jobs, then how you verified the numbers."},
            {"question": "How does the browser agent avoid calling the model in production?",
             "answer_strategy": "Explain the learn-once / replay design and the result contract with exit codes."}],
        "scorecard": {"relevance": 8, "credibility": 9, "impact": 8, "readability": 9, "ats": 8, "formatting": 9},
        "must_fixes_applied": ["Moved the AWS pipeline bullet to the top of experience",
                               "Replaced 'utilized' with concrete verbs in two bullets"],
        "unfixable_on_paper": ["No Go experience (listed as nice-to-have)"],
        "final_call": random.choice(["YES", "YES", "MAYBE"]),
        "final_call_reason": "Strong stack match and clean evidence; volume is the main risk, so a referral helps.",
    }
    t["linkedin"] = {
        "hiring_manager_note": f"Hi! I just applied for the {title} role at {company}. I built AWS pipelines that cut reporting "
                               "from 2 days to 15 minutes and would love to hear what your team is building next.",
        "recruiter_note": f"Hi! I applied to {title} at {company} today. Backend/data engineer with production AWS "
                          "pipelines and a tested LLM agent project. Happy to share anything that helps.",
        "alumni_note": f"Hi! Fellow State University alum here. I applied to {title} at {company} and would value 10 "
                       "minutes on what the team looks for.",
    }
    return t


def main() -> None:
    db.init()
    out = OUTPUT_DIR / "demo"
    out.mkdir(parents=True, exist_ok=True)
    resume, cover = out / "JordanLee_Resume.pdf", out / "JordanLee_CoverLetter.pdf"
    pdfgen.build_resume(FIXTURE, str(resume), title_meta="Jordan Lee — Resume (demo)")
    pdfgen.build_cover(FIXTURE["header"], FIXTURE["cover_letter"], str(cover), title_meta="Jordan Lee — Cover letter (demo)")

    plan = (["applied"] * 26 + ["interviewing"] * 2 + ["closed"] * 4 + ["ready"] * 6 + ["needs_decision"] * 2 + ["below_threshold"] * 3
            + ["rejected"] * 5 + ["filtered_out"] * 12)
    n = 0
    for status in plan:
        company, ats, loc = random.choice(COMPANIES)
        fam = random.choices(list(ROLES), weights=[6, 3, 3, 1, 1])[0]
        title = random.choice(ROLES[fam])
        n += 1
        jid = f"{ats}:{company.lower()}:{9100 + n}"
        if status in ("applied", "interviewing", "closed"):
            applied = NOW - timedelta(days=random.choice([1, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]), hours=random.randint(0, 8))
            if n <= 7:                                   # a few applications from this morning
                applied = local_today_at(random.randint(7, 11))
            posted = applied - timedelta(hours=random.randint(2, 20))
        elif status in ("ready", "needs_decision", "filtered_out") or n % 3 == 0:
            posted, applied = local_today_at(random.randint(6, 13)), None
        else:
            posted, applied = NOW - timedelta(days=random.randint(1, 3)), None
        score = {"applied": random.choice([7, 8, 8, 9]), "interviewing": 9, "ready": random.choice([7, 8, 9]),
                 "needs_decision": 7, "below_threshold": 6, "closed": 8, "rejected": random.choice([3, 4, 5])}.get(status)
        verdict = {"needs_decision": "APPLY WITH RISK", "below_threshold": "APPLY WITH RISK",
                   "rejected": "DON'T APPLY", "filtered_out": None}.get(status, "APPLY")
        row = {
            "id": jid, "company": company, "title": title, "location": loc, "ats": ats,
            "url": f"https://example.com/jobs/{company.lower()}/{9100 + n}", "apply_url": f"https://example.com/apply/{9100 + n}",
            "posted_at": iso(posted), "description": f"{company} is hiring a {title}. Build reliable services in Python and SQL.",
            "role_family": fam, "status": status, "source": random.choice(SOURCES) if status != "applied" or n % 2 else ats,
            "h1b_level": "strong" if n % 3 else "some", "h1b_signal": f"{random.randint(40, 900)} H-1B approvals in recent years",
            "startup": 1 if company in STARTUPS else 0, "company_stage": STARTUPS.get(company),
        }
        if status == "filtered_out":
            row["status_reason"] = random.choice(["Title looks senior (Senior / Staff)", "JD: 'unable to sponsor visas'",
                                                  "Asks 5+ years of experience", "Location outside the US",
                                                  "Requires an active security clearance"])
        if status == "rejected":
            row["status_reason"] = "Claude: core requirement is 4+ years of Go and Kubernetes in production"
        if status == "below_threshold":
            row["status_reason"] = "Match 6/10 is below your 7/10 threshold for building documents"
        if verdict:
            row.update(verdict=verdict, match_score=score, screen_json=json.dumps(screen(company, title, verdict, score)))
        if status in ("applied", "interviewing", "closed", "ready"):
            t = tailor(company, title)
            row.update(tailor_json=json.dumps(t), keyword_relevancy=t["keyword_relevancy"], rw_score=84,
                       final_call=t["teardown"]["final_call"], resume_pdf=str(resume), cover_pdf=str(cover),
                       checks_json=json.dumps({"resume": {"pages": 1, "words": 612, "bottom_gap_pt": 14},
                                               "cover": {"pages": 1, "words": 368},
                                               "layout": {"body_pt": 10.5, "line_pt": 12.2}, "problems": []}))
        if applied:
            row["applied_at"] = iso(applied)
        db.upsert_job(row)
        if status == "ready":
            db.add_intervention(jid, "submit", "Review documents and submit the application",
                                f"{company} · {title}\nApply link: {row['apply_url']}")
            for target, note in tailor(company, title)["linkedin"].items():
                kind = target.replace("_note", "")
                db.execute("INSERT OR IGNORE INTO outreach (job_id, target, search_url, message, status, created_at, updated_at) "
                           "VALUES (?,?,?,?, 'drafted', ?, ?)",
                           (jid, kind, "https://www.linkedin.com/search/results/people/", note, db.now(), db.now()))
        if status == "needs_decision":
            db.add_intervention(jid, "decision", f"Your call: {company} · {title} (APPLY WITH RISK, 7/10)",
                                "Prefers 3+ years of on-call experience. Build documents anyway, or skip?")

    first_ready = db.fetchone("SELECT * FROM jobs WHERE status='ready' ORDER BY posted_at DESC")
    db.add_intervention(first_ready["id"], "missing_info", "Have you used Terraform in a job or project? None is on record.",
                        "Answer once; it is saved to profile/answers.md for every future run.")

    people = [
        ("Priya Shah", first_ready["company"], "Senior Software Engineer", "Former teammate at Northwind Labs", "will_refer", "(555) 010-0142"),
        ("Marcus Chen", "Datadog", "Engineering Manager", "Met at a career fair", "warm", None),
        ("Elena Garcia", "Stripe", "Technical Recruiter", "LinkedIn connection", "unknown", None),
        ("Sam Okafor", "Databricks", "Data Engineer", "University alum", "warm", "(555) 010-0177"),
        ("Hannah Kim", "Notion", "Product Designer", "Hackathon teammate", "will_refer", None),
        ("Diego Morales", "Cloudflare", "Software Engineer II", "University alum", "no_reply", None),
        ("Aisha Patel", "Ramp", "Data Scientist", "Friend from undergrad", "student", None),
        ("Tom Becker", "Figma", "Staff Engineer", "Coffee chat in 2025", "unknown", None),
    ]
    for name, company, title, conn, stage, phone in people:
        db.execute("INSERT INTO contacts (name, company, company_key, title, connection, phone, linkedin, stage, notes, "
                   "source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (name, company, company.lower(), title, conn, phone, "https://www.linkedin.com/in/example", stage,
                    "Said to ping them when a matching role opens." if stage == "will_refer" else None, "manual", db.now(), db.now()))
    priya = db.fetchone("SELECT id FROM contacts WHERE name='Priya Shah'")
    msg = (f"Hi Priya, hope you're doing well! You mentioned pinging you when a role at {first_ready['company']} fit, and "
           f"I just found one: {first_ready['title']} ({first_ready['url']}). I recently built AWS pipelines that cut "
           "reporting from 2 days to 15 minutes, which lines up with the team's work. Would you be open to referring me? "
           "Happy to send my resume. Thank you!")
    db.execute("INSERT INTO referral_asks (job_id, contact_id, message, created_at, updated_at) VALUES (?,?,?,?,?)",
               (first_ready["id"], priya["id"], msg, db.now(), db.now()))
    db.add_intervention(first_ready["id"], "referral", f"Ask Priya Shah ({first_ready['company']}) for a referral before you submit",
                        f"Will refer · Former teammate at Northwind Labs\n\n{msg}")

    for i in reversed(range(12)):   # oldest first, so the newest run has the highest id
        start = NOW - timedelta(hours=7 + i * 12)
        stats = {"fetch": {"fetched": random.randint(2400, 4100), "new": random.randint(25, 140),
                           "feeds": {"new": random.randint(5, 40)}},
                 "screened": random.randint(12, 45), "queued_for_documents": random.randint(2, 9)}
        db.execute("INSERT INTO runs (started_at, finished_at, status, source, stats_json) VALUES (?,?,?,?,?)",
                   (iso(start), iso(start + timedelta(minutes=random.randint(6, 19), seconds=random.randint(0, 59))),
                    "done", "schedule" if i % 3 else "manual", json.dumps(stats)))
    for feed, seen, kept in (("jobright", 412, 37), ("simplify", 1650, 22), ("hn", 380, 9)):
        db.execute("INSERT OR REPLACE INTO feed_status VALUES (?,?,?,?,?,?)", (feed, 1, seen, kept, None, db.now()))
    # Board directory, USCIS records, and learned sponsors, so the Sources page looks like a real install.
    for ats, count in (("workday", 1847), ("greenhouse", 874), ("ashby", 691), ("lever", 357), ("smartrecruiters", 295)):
        for i in range(count):
            db.execute("INSERT OR IGNORE INTO discovered_boards (key, ats, slug, name, h1b, source, enabled, found_at) VALUES (?,?,?,?,?,?,1,?)",
                       (f"{ats}:demo-{i}", ats, f"demo-{i}", f"Demo company {i}", "", "directory", db.now()))
    db.kv_set("directory_refreshed", json.dumps({"boards": 4064, "added": 0}))
    for name, approvals in (("STRIPE INC", 310), ("DATADOG INC", 205), ("DATABRICKS INC", 420), ("CLOUDFLARE INC", 160)):
        db.execute("INSERT OR REPLACE INTO h1b_employers VALUES (?,?,?,?,?)", (name.lower().replace(" inc", ""), name, approvals, 3, "2026"))
    for name in ("Ramp", "Mercury", "Vercel", "Retool"):
        db.execute("INSERT OR REPLACE INTO sponsors VALUES (?,?,?,?,?)", (name.lower(), name, "Jobright: H-1B sponsor", "jobright", db.now()))
    from app.config import companies
    for c in companies():
        db.execute("INSERT OR REPLACE INTO source_status VALUES (?,?,?,?,?,?,?,?)",
                   (f"{c['ats']}:{c['slug']}", c["name"], c["ats"], c["slug"], 1, random.randint(5, 400), None, db.now()))
    # Email tracking: two connected inboxes (fictional) and a few application emails already read
    (DEMO / "data" / "mail_accounts.json").write_text(json.dumps({
        "gmail": {"address": "jordan.lee@example.com", "connected_at": db.now()},
        "applemail": {"name": "Exchange", "address": "jlee@university.example.edu", "connected_at": db.now()}}, indent=2))
    pick = lambda st: db.fetchall("SELECT id, company, title, status FROM jobs WHERE status=? ORDER BY applied_at DESC", (st,))
    emails = []
    for j in pick("closed"):
        emails.append((j, "rejection", f"Your application to {j['company']}", f"{j['company']} is moving forward with other candidates for {j['title']}.", "", ""))
    for j in pick("interviewing"):
        emails.append((j, "interview", f"Interview invitation: {j['title']}", f"{j['company']} invited you to a 30-minute recruiter call.", "https://calendly.com/example/30min", ""))
    applied = pick("applied")
    if applied:
        j = applied[0]
        due = (NOW + timedelta(days=6)).strftime("%Y-%m-%d")
        emails.append((j, "assessment", f"[Action Required] Your {j['company']} online assessment", f"{j['company']} sent a 90-minute coding assessment.", "https://www.hackerrank.com/test/example", due))
    for j in applied[1:7]:
        emails.append((j, "confirmation", f"Thank you for applying to {j['company']}", f"{j['company']} received the {j['title']} application.", "", ""))
    for n, (j, ev, subject, summary, link, due) in enumerate(emails):
        when = iso(NOW - timedelta(hours=3 + n * 5))
        new = {"rejection": "closed", "interview": "interviewing"}.get(ev, j["status"])
        prev = "applied" if ev in ("rejection", "interview") else j["status"]
        db.execute("INSERT INTO email_events (account, msg_id, received_at, sender, subject, snippet, company, role, event, link, deadline, "
                   "summary, job_id, prev_status, new_status, state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("jordan.lee@example.com" if n % 2 else "jlee@university.example.edu", f"<demo-{n}@example.com>", when,
                    f"{j['company']} Recruiting <no-reply@{j['company'].lower().replace(' ', '')}.example.com>", subject, summary,
                    j["company"], j["title"], ev, link, due, summary, j["id"], prev, new, "applied", db.now()))
        if ev in ("assessment", "interview"):
            title = ("Online assessment from " if ev == "assessment" else "Interview invite from ") + j["company"] + (f" · due {due}" if due else "")
            db.add_intervention(j["id"], ev, title, f"{summary}\n{link}\nEmail: {subject}")
    db.execute("INSERT INTO email_events (account, msg_id, received_at, sender, subject, snippet, company, role, event, link, deadline, summary, "
               "state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               ("jlee@university.example.edu", "<demo-unmatched@example.com>", iso(NOW - timedelta(hours=40)), "Talent Team <careers@initech.example.com>",
                "Update on your application", "Initech is not moving forward.", "Initech", "", "rejection", "", "",
                "Initech is not moving forward with the application.", "unmatched", db.now()))
    db.kv_set("mail_last_sync", json.dumps({"fetched": len(emails) + 1, "classified": len(emails) + 1, "applied": len(emails),
                                            "unmatched": 1, "errors": []}))

    print(f"Demo data written to {DEMO}. Start it with:\n"
          f"  JOB_AUTOPILOT_DATA=demo/data JOB_AUTOPILOT_PROFILE=demo/profile python -m app.cli serve")


if __name__ == "__main__":
    main()
