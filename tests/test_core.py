import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import prefilter, sources
from app.config import search_config
from app.render import pdf as pdfx
from app.render import pdfgen

CFG = search_config()
NOW = datetime(2026, 10, 11, 15, 0, tzinfo=timezone.utc)
FIXTURE = json.loads((Path(__file__).parent / "fixtures/sample_tailor.json").read_text())


def job(**kw):
    base = {"title": "Software Engineer", "location": "New York, NY", "posted_at": (NOW - timedelta(hours=3)).isoformat(),
            "description": "Build services in Python. 1+ years of experience."}
    return {**base, **kw}


def test_passes_good_job():
    ok, reason, fam, notes = prefilter.check(job(), CFG, 24, NOW)
    assert ok and fam == "Software Engineer"


@pytest.mark.parametrize("title", ["Senior Software Engineer", "Staff Data Engineer", "Software Engineering Intern", "Engineering Manager, ML"])
def test_excludes_seniority_and_interns(title):
    assert not prefilter.check(job(title=title), CFG, 24, NOW)[0]


def test_intern_word_boundary():
    ok, *_ = prefilter.check(job(title="Software Engineer, Internal Tools"), CFG, 24, NOW)
    assert ok


def test_old_posting_filtered():
    ok, reason, *_ = prefilter.check(job(posted_at=(NOW - timedelta(days=3)).isoformat()), CFG, 24, NOW)
    assert not ok and "older" in reason


@pytest.mark.parametrize("text", [
    "We are unable to sponsor visas for this role.",
    "Must be a U.S. citizen due to ITAR.",
    "Requires an active Secret clearance.",
    "Candidates must be able to work without sponsorship now or in the future.",
])
def test_visa_blockers(text):
    ok, reason, *_ = prefilter.check(job(description=text), CFG, 24, NOW)
    assert not ok and "blocker" in reason


def test_military_is_not_itar():
    ok, *_ = prefilter.check(job(description="Experience with military logistics data is a plus."), CFG, 24, NOW)
    assert ok


@pytest.mark.parametrize("loc,expected", [
    ("San Francisco, CA • New York, NY • United States", True),
    ("Bengaluru, IN", False),
    ("London, UK", False),
    ("Toronto, Canada • Remote", False),
    ("Remote", None),
    ("Austin, TX", True),
    ("London • New York", True),
])
def test_location(loc, expected):
    assert prefilter.is_us_location(loc, CFG) is expected


def test_years():
    assert prefilter.min_years_required("5+ years of professional experience") == 5
    assert prefilter.min_years_required("2-4 years of experience building APIs") == 2
    assert prefilter.min_years_required("We were founded 10 years ago") is None
    assert not prefilter.check(job(description="Requires 5+ years of experience."), CFG, 24, NOW)[0]


def test_html_to_text_handles_greenhouse_escaping():
    raw = "&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"
    t = sources.html_to_text(raw)
    assert "Build & ship" in t and "- Python" in t and "<" not in t


def _fitting_fixture():
    data = json.loads(json.dumps(FIXTURE))
    for group in data["experience"] + data["projects"]:
        group["bullets"] = [b if pdfgen.width(b, "R", 10.5) <= pdfgen.BULLET_W else b[:95].rsplit(" ", 1)[0] for b in group["bullets"]]
    for item in data["leadership"]:
        item["text"] = item["text"][:85]
    return data


def test_resume_pdf_is_one_page_and_selectable(tmp_path):
    from pypdf import PdfReader
    out = tmp_path / "r.pdf"
    data = _fitting_fixture()
    assert pdfgen.resume_fits(data, pdfgen.DEFAULT_LAYOUT)[0]
    pdfgen.build_resume(data, str(out), title_meta="Jordan Lee — Resume — Software Engineer, Example")
    r = PdfReader(str(out))
    text = r.pages[0].extract_text()
    assert len(r.pages) == 1 and "PROFESSIONAL EXPERIENCE" in text and "Expected Dec 2026" in text
    assert "Maintained the lab's data pipeline that ingests sensor readings" in text   # justified line stays one string
    assert sum(len(p.get("/Annots") or []) for p in r.pages) == 4                      # only the 4 header links
    assert r.metadata.title.startswith("Jordan Lee")


def test_overflow_never_cuts_text(tmp_path):
    from pypdf import PdfReader
    out = tmp_path / "long.pdf"
    data = json.loads(json.dumps(FIXTURE))
    data["projects"][0]["bullets"] *= 4                       # far too long for one page
    assert not pdfgen.resume_fits(data, pdfgen.LAYOUTS[0])[0]
    pdfgen.build_resume(data, str(out))
    text = "".join(p.extract_text() for p in PdfReader(str(out)).pages)
    assert "LEADERSHIP & ACHIEVEMENTS" in text and "President, Coding Club" in text   # spilled to page 2, nothing lost


def test_patch_revisions():
    from app.pipeline import apply_patch
    new, n = apply_patch(FIXTURE, [
        {"path": "experience[1].bullets[0]", "op": "set", "value": "Shorter real bullet."},
        {"path": "projects[2].bullets", "op": "append", "value": "Extra real bullet"},
        {"path": "experience[0].bullets[1]", "op": "delete", "value": ""},
        {"path": "header.name", "op": "set", "value": "nope"},          # not an allowed path: ignored
    ])
    assert n == 3
    assert new["experience"][1]["bullets"][0] == "Shorter real bullet"
    assert new["projects"][2]["bullets"][-1] == "Extra real bullet"
    assert len(new["experience"][0]["bullets"]) == len(FIXTURE["experience"][0]["bullets"]) - 1
    assert new["header"]["name"] == FIXTURE["header"]["name"]


def test_line_checks_flag_wrapping_and_dashes():
    data = json.loads(json.dumps(FIXTURE))
    data["projects"][0]["bullets"][0] = "x " * 200
    probs = pdfx.resume_line_checks(data, 10.5)
    assert any("WRAPS" in p for p in probs)
    letter = dict(FIXTURE["cover_letter"], paragraphs=["An end-to-end system."])
    assert any("hyphen" in p for p in pdfx.cover_letter_checks(letter))


def test_claim_is_atomic_and_progress_snapshot(tmp_path, monkeypatch):
    from app import db, progress
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    db.upsert_job({"id": "x:1", "company": "Acme", "title": "Software Engineer", "status": "to_tailor"})
    assert db.claim("x:1", "to_tailor", "tailoring") is True
    assert db.claim("x:1", "to_tailor", "tailoring") is False      # a second thread can't take it
    progress.set_stage("x:1", "tailor", "Claude is writing", 4, 66, "generate")
    snap = progress.snapshot()
    assert snap["active"][0]["company"] == "Acme" and 4 <= snap["active"][0]["pct"] < 70
    db.recover_interrupted()
    assert db.get_job("x:1")["status"] == "to_tailor"


def test_word_spacing_never_leaks(tmp_path):
    """Justified lines must not stretch the text drawn after them (PDF word spacing is sticky state)."""
    from pypdf import PdfReader
    out = tmp_path / "s.pdf"
    data = _fitting_fixture()
    pdfgen.build_resume(data, str(out))
    raw = PdfReader(str(out)).pages[0].get_contents().get_data().decode("latin-1")
    # every non-zero word spacing (Tw) is set inside a saved graphics state and reset to 0 before it closes
    assert raw.count(" Tw") >= 2
    for chunk in raw.split(" q")[1:]:
        body = chunk.split(" Q")[0]
        if " Tw" in body and not body.strip().startswith("0 Tw"):
            assert "0 Tw" in body
    text = PdfReader(str(out)).pages[0].extract_text()
    assert "MS Computer Science | State University | Austin, TX" in text


def test_skip_marks_dont_apply_and_deletes_documents(tmp_path, monkeypatch):
    from app import db, pipeline
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", tmp_path / "output")
    folder = tmp_path / "output" / "2026-10-14_Acme_SWE_1"
    folder.mkdir(parents=True)
    (folder / "AcmeSWE.pdf").write_bytes(b"%PDF")
    (folder / "AcmeSWE_CoverLetter.pdf").write_bytes(b"%PDF")
    outside = tmp_path / "keep.pdf"
    outside.write_bytes(b"%PDF")                                   # never touch files outside data/output
    db.init()
    db.upsert_job({"id": "x:2", "company": "Acme", "title": "SWE", "status": "ready",
                   "resume_pdf": str(folder / "AcmeSWE.pdf"), "cover_pdf": str(folder / "AcmeSWE_CoverLetter.pdf"),
                   "resume_docx": str(outside)})
    db.execute("INSERT INTO outreach (job_id, target, message) VALUES ('x:2', 'recruiter', 'hi')")
    assert pipeline.skip_job("x:2") == 2
    j = db.get_job("x:2")
    assert j["status"] == "rejected" and j["verdict"] == "DON'T APPLY" and j["resume_pdf"] is None
    assert not folder.exists() and outside.exists()
    assert db.fetchall("SELECT * FROM outreach WHERE job_id='x:2'") == []


def test_summary_title_and_right_aligned_dates(tmp_path):
    """The section title comes from the package (the master prompt), and achievement dates sit flush right."""
    from pypdf import PdfReader
    out = tmp_path / "s.pdf"
    pdfgen.build_resume(_fitting_fixture(), str(out))
    text = PdfReader(str(out)).pages[0].extract_text()
    assert "SUMMARY" in text and "CAREER OBJECTIVE" not in text
    assert "(Sep 2022" not in text and "Sep 2022 – Apr 2023" in text


def test_older_packages_with_career_objective_still_render(tmp_path):
    from pypdf import PdfReader
    data = _fitting_fixture()
    data["career_objective"] = data.pop("summary")
    data.pop("summary_title")
    for item in data["leadership"]:
        item.pop("date")
    out = tmp_path / "old.pdf"
    pdfgen.build_resume(data, str(out))
    assert "CAREER OBJECTIVE" in PdfReader(str(out)).pages[0].extract_text()


def test_bullet_a_hair_too_long_stays_on_one_line(tmp_path):
    """Up to 3% over the line is squeezed (tighter letter spacing) instead of wrapping; more than that still wraps."""
    from pypdf import PdfReader
    data = _fitting_fixture()
    base = data["experience"][1]["bullets"][0]
    while pdfgen.width(base, "R", 10.5) < pdfgen.BULLET_W * 1.015:
        base += " x"
    data["experience"][1]["bullets"][0] = base
    assert not any("WRAPS" in p for p in pdfx.resume_line_checks(data, 10.5))
    out = tmp_path / "sq.pdf"
    pdfgen.build_resume(data, str(out))
    text = PdfReader(str(out)).pages[0].extract_text()
    assert base.split()[-3] in text and len(PdfReader(str(out)).pages) == 1
    data["experience"][1]["bullets"][0] = base + " far too long now for any squeeze to save it"
    assert any("WRAPS" in p for p in pdfx.resume_line_checks(data, 10.5))


def test_network_errors_pause_instead_of_failing():
    from app import llm
    assert llm.NETWORK_RE.search("API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)")
    assert not llm.NETWORK_RE.search("Claude declined this request")


def test_builtin_clock_runs_each_slot_once(tmp_path, monkeypatch):
    """Windows / Linux scheduler: a due time starts one run; a time missed while asleep (< 90 min) still runs once."""
    from datetime import datetime
    from app import db, schedule, worker
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    monkeypatch.setattr(schedule, "load_times", lambda: ["08:00", "15:00"])
    calls = []
    monkeypatch.setattr(worker, "submit", lambda kind, arg=None: calls.append((kind, arg)) or True)
    assert worker.clock_tick(datetime(2026, 10, 16, 7, 59)) == []
    assert worker.clock_tick(datetime(2026, 10, 16, 8, 0, 20)) == ["08:00"]
    assert worker.clock_tick(datetime(2026, 10, 16, 8, 0, 50)) == []          # not twice
    assert worker.clock_tick(datetime(2026, 10, 16, 16, 10)) == ["15:00"]     # woke 70 min late: still runs
    assert worker.clock_tick(datetime(2026, 10, 17, 18, 0)) == []             # 3 hours late: skipped
    assert calls == [("run", "schedule"), ("run", "schedule")]


def test_dates_format_without_leading_zeros_on_every_os():
    from datetime import datetime
    from app.web import strf
    assert strf(datetime(2026, 10, 6, 9, 5), "%a %b %-d, %-I:%M %p") == "Tue Oct 6, 9:05 AM"
