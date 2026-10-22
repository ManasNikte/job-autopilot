"""Dashboard charts: the Sunday-to-Saturday week, the week picker API, time saved, and the role-family donut."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app import db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    from app.web import app
    return app.test_client()


def applied_on(jid, d: date, hour=12, fam="Software Engineer"):
    at = datetime.combine(d, datetime.min.time()).replace(hour=hour).astimezone().astimezone(timezone.utc).isoformat()
    db.upsert_job({"id": jid, "company": "C", "title": "T", "status": "applied", "applied_at": at, "role_family": fam,
                   "match_score": 8, "tailor_json": "{}"})


def test_week_is_sunday_to_saturday_with_previous_week_total(client):
    from app.web import week_series
    today = datetime.now().astimezone().date()
    sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    applied_on("a", sunday); applied_on("b", sunday); applied_on("c", today)
    applied_on("old", sunday - timedelta(days=1))                          # last Saturday: previous week
    w = week_series()
    assert [d["dow"] for d in w["days"]] == ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    assert w["days"][0]["date"] == sunday.isoformat() and w["current"]
    assert w["total"] == 3 and w["prev_total"] == 1
    assert sum(d["today"] for d in w["days"]) == 1
    assert all(d["future"] == (date.fromisoformat(d["date"]) > today) for d in w["days"])


def test_week_api_snaps_any_date_to_its_sunday(client):
    d = client.get("/api/week?start=2026-10-21").get_json()                # a Wednesday
    assert d["start"] == "2026-10-18" and d["days"][-1]["date"] == "2026-10-24"
    assert client.get("/api/week?start=nonsense").get_json()["current"] is True


def test_time_saved_counts_each_step(client):
    from app.web import MANUAL_MINUTES, time_saved
    for i in range(12):
        applied_on(f"j{i}", datetime.now().astimezone().date())
    t = time_saved()
    rows = {r["key"]: r for r in t["rows"]}
    assert rows["screen"]["n"] == 12 and rows["docs"]["n"] == 12 and rows["form"]["n"] == 12
    expected = 12 * (MANUAL_MINUTES["screen"] + MANUAL_MINUTES["docs"] + MANUAL_MINUTES["form"]) / 60
    assert t["hours"] == round(expected)


def test_donut_keeps_six_families_plus_other():
    from app.web import donut_slices
    rows = [{"fam": f"F{i}", "n": 10 - i} for i in range(9)]
    d = donut_slices(rows)
    names = [s["fam"] for s in d["slices"]]
    assert names[:6] == ["F0", "F1", "F2", "F3", "F4", "F5"] and names[-1] == "Other"
    assert d["slices"][-1]["n"] == 4 + 3 + 2 and d["total"] == sum(r["n"] for r in rows)
    assert [s["slot"] for s in d["slices"]] == ["1", "2", "3", "4", "5", "6", "other"]   # colors in fixed order
    one = donut_slices([{"fam": "Data", "n": 5}])
    assert len(one["slices"]) == 1 and one["slices"][0]["pct"] == 100


def test_dashboard_renders_the_charts(client):
    applied_on("x", datetime.now().astimezone().date(), fam="Data Engineer")
    html = client.get("/").get_data(as_text=True)
    for marker in ('id="applied-week"', 'id="wk-select"', 'id="time-saved"', 'class="donut"', "Data Engineer", "Time saved"):
        assert marker in html, marker


def test_family_donuts_share_colors():
    from app.web import donut_slices
    applied = donut_slices([{"fam": "Software Engineer", "n": 13}, {"fam": "Data Analyst", "n": 6}, {"fam": "Data Engineer", "n": 6}])
    slots = {s["fam"]: s["slot"] for s in applied["slices"]}
    rejected = donut_slices([{"fam": "Data Engineer", "n": 3}, {"fam": "Quant", "n": 1}], slots=slots)
    got = {s["fam"]: s["slot"] for s in rejected["slices"]}
    assert got["Data Engineer"] == slots["Data Engineer"]                    # same family, same color
    assert got["Other"] == "other" and "Quant" not in got                     # families not on the Applied donut fold into Other
    assert donut_slices([], slots=slots) == {"total": 0, "slices": []}


def test_active_applications_by_family(tmp_path, monkeypatch):
    from datetime import date
    from app import db
    from app.web import active_series
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    for jid, fam, st, applied, changed in [("a", "Software Engineer", "applied", "2026-10-12T15:00:00+00:00", "2026-10-12T15:00:00+00:00"),
                                           ("b", "Software Engineer", "closed", "2026-10-13T15:00:00+00:00", "2026-10-15T15:00:00+00:00"),
                                           ("c", "Data Analyst", "applied", "2026-10-14T15:00:00+00:00", "2026-10-14T15:00:00+00:00"),
                                           ("d", "Quant", "applied", "2026-10-14T15:00:00+00:00", None)]:
        db.upsert_job({"id": jid, "company": "X", "title": "T", "status": st, "role_family": fam})
        db.execute("UPDATE jobs SET applied_at=?, status_changed_at=? WHERE id=?", (applied, changed, jid))
    out = active_series({"Software Engineer": "1", "Data Analyst": "2"}, today=date(2026, 10, 16))
    assert [d["iso"] for d in out["days"]][0] == "2026-10-12" and len(out["days"]) == 5
    by = {s["fam"]: s["points"] for s in out["series"]}
    assert by["Software Engineer"] == [1, 2, 2, 1, 1]                          # rejected on the 15th: no longer active
    assert by["Data Analyst"] == [0, 0, 1, 1, 1] and by["Other"] == [0, 0, 1, 1, 1]   # Quant folds into Other
    assert out["total"] == 3
