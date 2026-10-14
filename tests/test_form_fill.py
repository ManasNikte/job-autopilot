"""Fill + learn loop against a local fake form (no network, no Claude)."""
import json

import pytest

from app import form_fill

pw = pytest.importorskip("playwright.sync_api")

FORM = """
<form>
  <label for="fn">First Name*</label><input id="fn" required>
  <label for="hear">How did you hear about Acme?</label>
  <select id="hear"><option></option><option>Acme Careers Website</option><option>Referral</option></select>
  <fieldset><legend>Will you require sponsorship?</legend>
    <label><input type="radio" name="sp" value="y"> Yes</label>
    <label><input type="radio" name="sp" value="n"> No</label>
  </fieldset>
  <label for="sal">Salary expectations*</label><input id="sal" required>
  <label><input type="checkbox" id="ok" required> I certify the above is true</label>
</form>
"""


@pytest.fixture()
def page():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.set_content(FORM)
        yield pg
        b.close()


def test_extract_fill_and_learn(page, tmp_path, monkeypatch):
    monkeypatch.setattr(form_fill, "MEMORY_FILE", tmp_path / "mem.json")
    ex = form_fill.extract(page)
    q = {f["question"]: f for _, f in ex}
    assert "How did you hear about Acme?" in q and q["How did you hear about Acme?"]["options"]
    sp = next(f for _, f in ex if f["type"] == "radio")
    assert [o["label"] for o in sp["options"]] == ["Yes", "No"]

    key = {f["question"]: f["key"] for _, f in ex}
    answers = [
        {"key": key["First Name*"], "action": "fill", "value": "Jordan", "needs_human": False, "drafted": False, "note": ""},
        {"key": key["How did you hear about Acme?"], "action": "fill", "value": "Acme Careers Website", "needs_human": False, "drafted": False, "note": ""},
        {"key": sp["key"], "action": "fill", "value": "Yes", "needs_human": False, "drafted": False, "note": ""},
        {"key": key["Salary expectations*"], "action": "skip", "value": "", "needs_human": True, "drafted": False, "note": "your call"},
    ]
    res = form_fill.apply(page, ex, answers)
    assert page.input_value("#fn") == "Jordan"
    assert page.eval_on_selector("input[value=y]", "e => e.checked")
    assert any("Salary" in n for n in res["needs"])

    # The candidate edits two answers, then the confirmation page appears.
    page.select_option("#hear", label="Referral")
    page.fill("#sal", "120000")
    snap = form_fill.snapshot(page)
    learned = form_fill.learn({"company": "Acme"}, ex, res["values"], snap, submitted=True)
    assert len(learned) == 2
    mem = json.loads((tmp_path / "mem.json").read_text())
    assert mem["how did you hear about {company}"]["answer"] == "Referral"
    assert mem["salary expectations"]["answer"] == "120000"
    assert "will you require sponsorship" in mem      # kept-and-submitted answers are remembered too
    assert "first name" not in mem                   # contact fields come from presets, not memory
    assert form_fill.learned_answers()["salary expectations"] == "120000"


STRICT = """<html><head><meta http-equiv="Content-Security-Policy"
  content="default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; require-trusted-types-for 'script'">
</head><body><form><label for="a">A</label><input id="a"></form></body></html>"""


def test_banner_hide_works_under_strict_csp():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.set_content(STRICT)
        form_fill.status(pg, "Claude is reading this form…")
        form_fill.banner(pg, ["Salary expectations"], ["Why this company?"])
        assert pg.is_visible("#autopilot-banner")
        assert "Why this company?" in pg.inner_text("#autopilot-banner")
        pg.click("#autopilot-banner button")
        assert not pg.is_visible("#autopilot-banner")
        assert pg.is_visible("#autopilot-pill")
        pg.click("#autopilot-pill")
        assert pg.is_visible("#autopilot-banner") and not pg.is_visible("#autopilot-pill")
        b.close()
