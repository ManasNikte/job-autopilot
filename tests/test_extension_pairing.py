"""Only the Chrome extension you approve can use the dashboard's extension API."""
import pytest

from app import db

EXT = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
OTHER = "chrome-extension://zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    from app.web import app
    return app.test_client()


def ping(c, origin):
    return c.get("/api/ext/ping", headers={"X-Autopilot": "1", "Origin": origin})


def test_unknown_extension_asks_you_first(client):
    r = ping(client, EXT)
    assert r.status_code == 403 and r.get_json()["pair"] and "Approve" in r.get_json()["error"]
    html = client.get("/").get_data(as_text=True)
    assert "A Chrome extension wants to connect" in html and "abcdefghijklmnopabcdefghijklmnop" in html
    assert client.post("/extension/approve", data={"id": "abcdefghijklmnopabcdefghijklmnop"}).status_code == 302
    assert ping(client, EXT).status_code == 200                                # approved: works
    assert "wants to connect" not in client.get("/").get_data(as_text=True)    # banner gone
    assert ping(client, OTHER).status_code == 403                              # any other extension: still blocked


def test_not_mine_keeps_it_blocked_and_ids_must_match(client):
    ping(client, OTHER)
    assert client.post("/extension/approve", data={"id": "somethingelse"}).status_code == 400   # only the one that asked
    client.post("/extension/dismiss", data={"id": "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"})
    assert ping(client, OTHER).status_code == 403


def test_extensions_cant_press_dashboard_buttons(client):
    client.post("/extension/approve", data={"id": "x"})                        # nothing pending: refused
    r = client.post("/run", headers={"Origin": EXT})
    assert r.status_code == 403                                                # extension origins only reach /api/ext


def test_web_pages_and_missing_header_still_refused(client):
    assert client.get("/api/ext/ping", headers={"X-Autopilot": "1", "Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/ext/ping", headers={"Origin": EXT}).status_code == 403
