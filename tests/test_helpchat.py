"""Help chat: answers from the app's own docs plus this install's state; never sees passwords; nothing stored."""
import pytest

from app import db, helpchat, llm


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    from app.web import app
    return app.test_client()


def test_knowledge_comes_from_the_latest_docs():
    k = helpchat.knowledge()
    from app.version import VERSION
    assert f"## {VERSION} (" in k                                            # this release's changelog
    assert "Troubleshooting" in k and "How Job Autopilot works" in k and "/settings/email" in k
    assert "docs/screenshots/" not in k                                        # screenshots stripped


def test_state_has_no_secrets(client):
    s = helpchat.state()
    assert {"version", "jobs_by_status", "recent_errors"} <= set(s)
    blob = str(s).lower()
    assert "password" not in blob and "app_password" not in blob and "token" not in blob


def test_help_route(client, monkeypatch):
    seen = {}
    def fake(**kw):
        seen.update(kw)
        return {"answer": "Open [Email inboxes](/settings/email) and click **Check email now**."}
    monkeypatch.setattr(llm, "structured", fake)
    r = client.post("/api/help", json={"q": "Why weren't my emails scanned?", "history": [{"role": "user", "text": "hi"}]})
    assert r.status_code == 200 and "/settings/email" in r.get_json()["answer"]
    assert seen["interactive"] and "Troubleshooting" in seen["system"] and "<install_state>" in seen["user"]
    assert "User: hi" in seen["user"] and "Question: Why weren't my emails scanned?" in seen["user"]
    assert client.post("/api/help", json={"q": " "}).status_code == 400

    def expired(**kw):
        raise llm.BackendUnavailable("Claude needs you to sign in again (OAuth session expired)")
    monkeypatch.setattr(llm, "structured", expired)
    r = client.post("/api/help", json={"q": "help"})
    assert r.status_code == 503 and "/login" in r.get_json()["error"]


def test_help_button_is_on_every_page(client):
    for page in ("/", "/jobs", "/email", "/settings/schedule"):
        html = client.get(page).get_data(as_text=True)
        assert 'id="help-chat"' in html and "help.js" in html, page
