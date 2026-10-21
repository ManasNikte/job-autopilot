"""Plan-usage check: background Claude work pauses at USAGE_PAUSE_AT % and resumes after the reset;
screening batches chain on their own."""
import time

import pytest

from app import db, llm, worker

REPORT = [{"kind": "session", "group": "session", "percent": 66, "resets_at": "2026-10-21T06:40:01+00:00", "scope": None, "is_active": True},
          {"kind": "weekly_scoped", "group": "weekly", "percent": 99, "resets_at": "2026-10-21T05:00:00+00:00",
           "scope": {"model": None, "surface": {"display_name": "Cowork"}}, "is_active": False}]


@pytest.fixture()
def usage(monkeypatch):
    reading = {"limits": [dict(l) for l in REPORT]}
    monkeypatch.setattr(llm, "read_usage", lambda cli=None: reading["limits"])
    monkeypatch.setattr(llm, "backend", lambda: "claude_cli")
    llm.USAGE.update(limits=None, checked=0.0)
    return reading


def test_scoped_limits_are_ignored_and_under_threshold_passes(usage):
    llm.usage_guard()                                    # session 66%; the 99% Cowork-only limit doesn't count
    assert llm.usage_now()["worst"]["percent"] == 66


def test_pauses_at_95_until_the_reset(usage, monkeypatch):
    usage["limits"][0]["percent"] = 95
    llm.USAGE["checked"] = 0
    with pytest.raises(llm.BackendUnavailable) as e:
        llm.usage_guard()
    assert "5-hour session limit at 95%" in str(e.value)
    reset = llm._iso_epoch("2026-10-21T06:40:01+00:00")
    assert e.value.retry_at == pytest.approx(reset + 120)
    monkeypatch.setenv("USAGE_PAUSE_AT", "100")          # turned off in Settings
    llm.usage_guard()


def test_reading_is_cached(usage, monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "read_usage", lambda cli=None: calls.append(1) or REPORT)
    llm.usage_now(); llm.usage_now(); llm.usage_now()
    assert len(calls) == 1
    llm.USAGE["checked"] = time.time() - llm.USAGE_TTL - 1
    llm.usage_now()
    assert len(calls) == 2


def test_background_calls_are_guarded_but_autofill_is_not(usage, monkeypatch):
    usage["limits"][0]["percent"] = 97
    llm.USAGE["checked"] = 0
    monkeypatch.setattr(llm, "_via_cli", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(llm, "system_prompt", lambda: "sys")
    with pytest.raises(llm.BackendUnavailable):
        llm.structured(user="u", schema={}, model="m", label="screen")
    assert llm.structured(user="u", schema={}, model="m", label="autofill", interactive=True) == {"ok": True}


def test_unavailable_reading_never_blocks(monkeypatch):
    monkeypatch.setattr(llm, "read_usage", lambda cli=None: None)
    monkeypatch.setattr(llm, "backend", lambda: "claude_cli")
    llm.USAGE.update(limits=None, checked=0.0)
    llm.usage_guard()                                    # no data (older CLI, API key): carry on


def test_next_batch_chains_until_nothing_is_left(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    db.upsert_job({"id": "a", "company": "A", "title": "T", "status": "to_screen"})
    assert worker._next_batch({"screened": 60}) == ("process", "auto")
    assert worker._next_batch({"screened": 0}) is None                 # a batch that screened nothing stops the chain
    db.update_job("a", status="rejected")
    assert worker._next_batch({"screened": 60}) is None                # nothing left
    monkeypatch.setenv("AUTO_CONTINUE_SCREENING", "0")
    db.update_job("a", status="to_screen")
    assert worker._next_batch({"screened": 60}) is None
