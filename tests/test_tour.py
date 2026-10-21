"""First-run tour: shown once to new users after they've said their name, never to existing installs."""
import pytest

from app import db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    from app import greeting
    from app.web import app
    names = {"v": "Ava"}
    monkeypatch.setattr(greeting, "user_name", lambda: names["v"])
    c = app.test_client()
    c.names = names
    return c


def has_tour(c, url="/"):
    return 'id="tour-steps"' in c.get(url).get_data(as_text=True)


def test_new_user_sees_tour_once_and_skip_is_remembered(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="tour-steps"' in html and "Welcome to Job Autopilot, Ava" in html
    assert client.post("/tour/skipped").status_code == 204
    assert not has_tour(client)
    assert has_tour(client, "/?tour=1")                                   # Settings → Replay the tour
    assert client.post("/tour/whatever").status_code == 404


def test_waits_for_the_name_box(client):
    client.names["v"] = None
    assert not has_tour(client)


def test_existing_installs_dont_get_it(client):
    from app.web import tour_steps
    db.execute("INSERT INTO runs (started_at, status) VALUES (?, 'done')", (db.now(),))
    if not db.kv_get("tour") and db.count("runs"):                        # what serve() does at startup
        db.kv_set("tour", "existing")
    assert not has_tour(client)
    steps = tour_steps("Ava Lee")
    assert steps[0]["title"].startswith("Welcome to Job Autopilot, Ava") and len(steps) == 12
    assert all(s.get("target", "[data-tour=").startswith("[data-tour=") for s in steps)


def test_every_tour_target_exists_on_the_dashboard(client):
    from app.web import tour_steps
    html = client.get("/?tour=1").get_data(as_text=True)
    for s in tour_steps("Ava"):
        if s.get("target"):
            name = s["target"].split('"')[1]
            assert f'data-tour="{name}"' in html, name
