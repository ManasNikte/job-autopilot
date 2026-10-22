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
    with client.application.test_request_context():
        steps = tour_steps("Ava Lee")
    assert steps[0]["title"].startswith("Welcome to Job Autopilot, Ava") and len(steps) >= 20
    assert all(s.get("target", "[data-tour=").startswith("[data-tour=") for s in steps)
    assert steps[-1]["page"] == "/" and steps[-1]["cta"]                   # ends back on the dashboard


def test_every_tour_target_exists_on_its_page(client):
    """The walkthrough visits every page; each step's element must be on the page it opens."""
    from app.web import tour_steps
    pages = {}
    with client.application.test_request_context():
        steps = tour_steps("Ava")
    for st in steps:
        page = st["page"] or "/"
        assert client.get(page).status_code == 200, page
        if st.get("target"):
            html = pages.setdefault(page, client.get(page + ("&" if "?" in page else "?") + "tour=1").get_data(as_text=True))
            name = st["target"].split('"')[1]
            assert f'data-tour="{name}"' in html, (page, name)
    assert len(pages) >= 12                                                 # it really goes through the app


def test_tour_continues_on_other_pages(client):
    steps = client.get("/tour/steps").get_json()
    assert steps[0]["title"].startswith("Welcome") and any(s["page"].startswith("/settings/") for s in steps)
    assert "tour.js" in client.get("/inbox?tour_at=7").get_data(as_text=True)     # carries on mid-walkthrough
    assert "tour.js" not in client.get("/inbox").get_data(as_text=True)            # and stays out of the way otherwise
    html = client.get("/?tour_at=3").get_data(as_text=True)
    assert html.count("tour.js") == 1                                              # never loaded twice on the dashboard
