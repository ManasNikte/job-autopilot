"""Resume layout: empty sections disappear, long content never overlaps, templates reorder sections."""
import copy
import json
from pathlib import Path

import pytest
from pypdf import PdfReader

from app.render import pdfgen

FIXTURE = json.loads((Path(__file__).parent / "fixtures/sample_tailor.json").read_text(encoding="utf-8"))


def text_of(pdf: Path) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(str(pdf)).pages)


def right_edges(pdf: Path) -> list[tuple[float, str]]:
    """(x where each text run ends, text) for every run, using the same font metrics as the renderer."""
    out = []
    for page in PdfReader(str(pdf)).pages:
        def visit(text, cm, tm, font, size):
            if text.strip():
                x = tm[4] * cm[0] + cm[4]
                name = (font or {}).get("/BaseFont", "")
                face = "BI" if "BoldItalic" in name else "B" if "Bold" in name else "I" if "Italic" in name else "R"
                t = text.strip("\n")
                out.append((x + pdfgen.width(t, face, size * (tm[0] or 1)), t))
        page.extract_text(visitor_text=visit)
    return out


def test_empty_achievements_and_projects_are_left_off(tmp_path):
    d = copy.deepcopy(FIXTURE)
    d["leadership"], d["projects"] = [], []
    pdfgen.build_resume(d, str(tmp_path / "r.pdf"))
    t = text_of(tmp_path / "r.pdf")
    assert "LEADERSHIP" not in t and "PROJECTS" not in t and "PROFESSIONAL EXPERIENCE" in t


def test_overloaded_content_never_overlaps(tmp_path):
    d = copy.deepcopy(FIXTURE)
    d["header"]["line2"] = [{"text": f"contact-item-number-{i}@example.com", "url": ""} for i in range(6)]
    d["skills"][0]["items"] = ", ".join(f"Tool{i}" for i in range(40))
    d["projects"][0]["stack"] = "Python, Playwright, Flask, JSON Schema, a very long list of frameworks and libraries"
    d["experience"][0]["company"] = "An Extremely Long Company Name Incorporated Worldwide"
    d["education"][0]["coursework"] = [f"Advanced Course Number {i}" for i in range(8)]
    for name in pdfgen.TEMPLATES:
        d["_template"] = name
        out = tmp_path / f"{name}.pdf"
        pdfgen.build_resume(d, str(out))
        limit = pdfgen.PAGE_W - pdfgen.R_SIDE + 1.5
        past = [(round(x), t[:40]) for x, t in right_edges(out) if x > limit]
        assert not past, (name, past[:5])
    from app.render.pdf import resume_line_checks
    probs = " ".join(resume_line_checks(d, 10.5))
    assert "Project row" in probs and "too long for one line" in probs      # the revision step is asked to trim


@pytest.mark.parametrize("name, first, second", [("classic", "EDUCATION", "PROFESSIONAL EXPERIENCE"),
                                                 ("experienced", "PROFESSIONAL EXPERIENCE", "EDUCATION"),
                                                 ("projects_first", "PROJECTS", "PROFESSIONAL EXPERIENCE")])
def test_templates_order_sections(tmp_path, name, first, second):
    d = copy.deepcopy(FIXTURE)
    d["_template"] = name
    pdfgen.build_resume(d, str(tmp_path / "r.pdf"))
    t = text_of(tmp_path / "r.pdf")
    assert t.index(first) < t.index(second)


def test_compact_template_uses_less_space():
    d = copy.deepcopy(FIXTURE)
    classic = pdfgen.resume_fits(d, pdfgen.DEFAULT_LAYOUT)[1]
    d["_template"] = "compact"
    assert pdfgen.resume_fits(d, pdfgen.DEFAULT_LAYOUT)[1] > classic + 20


def test_unknown_template_falls_back_to_classic(monkeypatch):
    from app import pipeline
    monkeypatch.setattr(pipeline, "applicant", lambda: {"resume_template": "fancy-two-column"})
    assert pipeline.resume_template() == "classic"
    monkeypatch.setattr(pipeline, "applicant", lambda: {"resume_template": "compact"})
    assert pipeline.resume_template() == "compact"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app.config as cfg
    import app.web as web
    from app import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    prof = tmp_path / "profile"
    prof.mkdir()
    (prof / "applicant.json").write_text(json.dumps({"name": "Ava Lee"}), encoding="utf-8")
    (prof / "system_prompt.md").write_text("MASTER PROMPT\n" + "x" * 3000, encoding="utf-8")
    monkeypatch.setattr(web, "PROFILE_DIR", prof)
    monkeypatch.setattr(cfg, "APPLICANT_FILE", prof / "applicant.json")
    monkeypatch.setattr(web, "DATA_DIR", tmp_path)
    return web.app.test_client(), prof


def test_choosing_a_template_saves_it_with_history(client, monkeypatch):
    c, prof = client
    import app.pipeline as pipeline
    monkeypatch.setattr(pipeline, "applicant", lambda: json.loads((prof / "applicant.json").read_text(encoding="utf-8")))
    page = c.get("/profile/resume-template").get_data(as_text=True)
    assert "Projects first" in page and "Experience first" in page and "Compact" in page
    assert c.post("/profile/resume-template", data={"template": "compact"}).status_code == 302
    assert json.loads((prof / "applicant.json").read_text(encoding="utf-8"))["resume_template"] == "compact"
    assert any(p.name.startswith("applicant__") for p in (prof / "history").iterdir())
    assert pipeline.resume_template() == "compact"
    assert c.post("/profile/resume-template", data={"template": "two-column"}).status_code == 400


def test_preview_is_a_real_pdf(client):
    c, _ = client
    r = c.get("/profile/resume-preview?t=experienced")
    assert r.status_code == 200 and r.data[:4] == b"%PDF"
    assert c.get("/profile/resume-preview?t=nope").status_code == 404


def test_resume_rules_upgrade_is_reversible_and_idempotent(client):
    from app import resume_rules
    c, prof = client
    c.post("/profile/resume-rules")
    once = (prof / "system_prompt.md").read_text(encoding="utf-8")
    assert resume_rules.status(once) == resume_rules.RULES_VERSION and once.startswith("MASTER PROMPT")
    c.post("/profile/resume-rules")
    twice = (prof / "system_prompt.md").read_text(encoding="utf-8")
    assert twice.count(resume_rules.START) == 1                         # refreshed in place, not appended again
    saved = sorted((prof / "history").glob("system_prompt__*"), key=lambda f: f.name.replace(".md", ""))
    assert saved and "JOB-AUTOPILOT RESUME RULES" not in saved[0].read_text(encoding="utf-8")   # the original is kept
    old = once.replace(f" v{resume_rules.RULES_VERSION} ", " v0 ")
    assert resume_rules.status(resume_rules.apply(old)) == resume_rules.RULES_VERSION
