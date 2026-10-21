import json
from datetime import datetime

from app import greeting

BASE = {"today": 0, "week": 0, "prev_week": 0, "total": 0, "streak": 0, "past_best": 0, "ready": 0,
        "interviewing": 0, "offer": None, "interview": None, "assessment": None}


def test_time_of_day_and_progress():
    early = greeting.line(datetime(2026, 10, 20, 5, 0), dict(BASE, today=3, week=9, past_best=10), "Sam")
    assert early.startswith("Up early, Sam") and "3 applications" in early
    g = greeting.greet(datetime(2026, 10, 20, 9, 0), dict(BASE, assessment={"company": "Acme Games"}), "Manas")
    assert g["title"] == "Good morning, Manas ☀️" and g["text"].startswith("📝 Acme Games sent an assessment")
    late = greeting.line(datetime(2026, 10, 20, 2, 0), dict(BASE, today=1, week=5, past_best=10), "Sam")
    assert late.startswith("Still up, Sam?") and "1 application " in late and "sleep" in late
    best = greeting.line(datetime(2026, 10, 20, 15, 0), dict(BASE, today=12, past_best=9), "Sam")
    assert "personal best" in best
    win = greeting.line(datetime(2026, 10, 20, 20, 0), dict(BASE, interview={"company": "Acme"}), "Sam")
    assert "Acme wants to talk to you" in win
    assert "Run now" in greeting.line(datetime(2026, 10, 20, 9, 0), dict(BASE), "Sam")


def test_name_from_profile_and_placeholder(tmp_path, monkeypatch):
    f = tmp_path / "applicant.json"
    monkeypatch.setattr(greeting, "APPLICANT_FILE", f)
    monkeypatch.setattr(greeting, "SYSTEM_PROMPT_FILE", tmp_path / "missing.md")
    f.write_text(json.dumps({"name": "Your Name"}), encoding="utf-8")
    assert greeting.user_name() == ""                      # template placeholder: ask on the dashboard
    greeting.set_name("Priya Shah")
    assert greeting.user_name() == "Priya"
    assert json.loads(f.read_text(encoding="utf-8"))["name"] == "Priya Shah"
