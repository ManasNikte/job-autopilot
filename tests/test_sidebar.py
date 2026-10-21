"""Sidebar navigation: every page and sub-page, the current one highlighted, its group open."""
import pytest

from app import cloud, db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    from app.web import app
    return app.test_client()


def test_sidebar_lists_pages_and_highlights_the_current_one(client, monkeypatch):
    monkeypatch.setitem(cloud.STATUS, "operator", None)
    html = client.get("/jobs?status=ready").get_data(as_text=True)
    for label in ("Dashboard", "Needs you", "Background", "All jobs", "Ready to submit", "In progress", "Applied",
                  "Assessments", "Email", "Not matched", "Outreach", "People", "Profile", "Form presets", "Sources",
                  "Settings", "Online dashboard", "App updates"):
        assert label in html, label
    assert 'class="sb-sub on" aria-current="page">Ready to submit' in html
    assert 'class="sb-group open" data-group="jobs"' in html                 # inside Jobs: that group is open
    assert 'class="sb-group" data-group="settings"' in html                  # others stay folded
    for tour in ("inbox", "jobs", "applied", "email", "profile", "settings", "run"):
        assert f'data-tour="{tour}"' in html, tour
    assert "/users" not in html                                              # operator-only link hidden


def test_operator_sees_users_link(client, monkeypatch):
    monkeypatch.setitem(cloud.STATUS, "operator", {"users": 3, "active": 1})
    assert 'href="/users"' in client.get("/settings/schedule").get_data(as_text=True)


def test_settings_are_separate_pages(client):
    pages = {"schedule": "Daily runs", "pipeline": "Save pipeline settings", "email": "Email: track application replies",
             "online": "Online dashboard", "updates": "App updates", "start": "Getting started"}
    for sec, marker in pages.items():
        r = client.get(f"/settings/{sec}")
        assert r.status_code == 200 and marker in r.get_data(as_text=True), sec
    sched = client.get("/settings", follow_redirects=True).get_data(as_text=True)   # /settings opens Schedule
    assert "Daily runs" in sched and "Save pipeline settings" not in sched and "Email: track application replies" not in sched
    assert 'class="sb-sub on" aria-current="page">Pipeline and limits' in client.get("/settings/pipeline").get_data(as_text=True)
    assert client.get("/settings/nope").status_code == 404
    r = client.post("/settings/cloud/cancel")
    assert r.status_code == 302 and r.headers["Location"].endswith("/settings/online")


def test_assessments_page(client):
    html = client.get("/assessments").get_data(as_text=True)
    assert "Online assessments" in html and "Finished or dismissed" in html
    assert 'class="sb-link on"' in html and "Assessments</span>" in html


def test_search_index_finds_pages_and_individual_settings(client, monkeypatch):
    from app.web import app, nav_index
    with app.test_request_context():
        idx = nav_index()
        titles = {i["t"] for i in idx}
        assert {"Dashboard", "Assessments", "Gmail", "Run times", "Pipeline and limits", "Online dashboard account"} <= titles
        pay = [i for i in idx if "pay" in (i["t"] + i["k"]).lower()]
        assert pay and all(i["u"].startswith("/settings/pipeline#set-") for i in pay if i["p"].endswith("Pipeline and limits"))
        assert any(i["u"] == "/settings/pipeline#set-USAGE_PAUSE_AT" for i in idx)
        assert "Users" not in titles and any(i["t"] == "Users" for i in nav_index(operator=True))
    assert 'id="nav-index"' in client.get("/").get_data(as_text=True)
