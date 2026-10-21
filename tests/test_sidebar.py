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
    assert 'href="/users"' in client.get("/settings").get_data(as_text=True)
