"""In-app updates against a real throwaway git remote and clone."""
import subprocess

import pytest

from app import db, updater


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture()
def repos(tmp_path, monkeypatch):
    remote, dev, user = tmp_path / "remote.git", tmp_path / "dev", tmp_path / "user"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(dev)], check=True, capture_output=True)
    for r in (dev,):
        git(r, "config", "user.email", "dev@example.com"); git(r, "config", "user.name", "Dev")
    (dev / "app").mkdir()
    (dev / "app" / "version.py").write_text('VERSION = "3.1"\n', encoding="utf-8")
    (dev / "config").mkdir()
    (dev / "config" / "schedule.json").write_text('{"times": ["08:00"]}\n', encoding="utf-8")
    (dev / "CHANGELOG.md").write_text("# Changelog\n\n## 3.1 (2026-10-20)\n- First.\n", encoding="utf-8")
    git(dev, "add", "-A"); git(dev, "commit", "-qm", "v3.1"); git(dev, "push", "-q", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(remote), str(user)], check=True, capture_output=True)
    git(user, "config", "user.email", "u@example.com"); git(user, "config", "user.name", "User")
    monkeypatch.setattr(updater, "ROOT", user)
    monkeypatch.setattr(updater, "VERSION", "3.1")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    monkeypatch.setattr(updater, "_busy", lambda: "")
    updater.STATE.update(behind=0, remote_version=None, notes=[], commits=[], checked_at=None, error="", updating=False)
    updater.PROGRESS.update(stage="", pct=0, message="", target=None, error="", lock=False)
    yield dev, user
    updater.PROGRESS.update(stage="", lock=False)


def release(dev, version, notes, extra=None):
    (dev / "app" / "version.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")
    cl = (dev / "CHANGELOG.md").read_text(encoding="utf-8").replace("# Changelog\n\n", f"# Changelog\n\n## {version} (2026-10-21)\n" + "".join(f"- {n}\n" for n in notes) + "\n")
    (dev / "CHANGELOG.md").write_text(cl, encoding="utf-8")
    for path, text in (extra or {}).items():
        (dev / path).write_text(text, encoding="utf-8")
    git(dev, "add", "-A"); git(dev, "commit", "-qm", f"Release {version}"); git(dev, "push", "-q", "origin", "main")


def test_no_update_when_current(repos):
    st = updater.check(force=True)
    assert st["behind"] == 0 and not updater.update_available() and st["error"] == ""


def test_finds_update_with_notes_and_applies_it(repos):
    dev, user = repos
    release(dev, "3.2", ["**Faster** screening.", "New maps."])
    st = updater.check(force=True)
    assert st["behind"] == 1 and st["remote_version"] == "3.2" and updater.update_available()
    assert st["notes"][0]["items"] == ["**Faster** screening.", "New maps."]
    res = updater.apply()
    assert res["ok"] and res["restart"] and res["version"] == "3.2"
    assert 'VERSION = "3.2"' in (user / "app" / "version.py").read_text(encoding="utf-8")
    assert not updater.update_available()


def test_keeps_users_settings_edits(repos):
    dev, user = repos
    (user / "config" / "schedule.json").write_text('{"times": ["06:30"]}\n', encoding="utf-8")          # user's own setting
    release(dev, "3.2", ["Thing."], {"README.md": "hi\n"})
    updater.check(force=True)
    assert updater.apply()["ok"]
    assert "06:30" in (user / "config" / "schedule.json").read_text(encoding="utf-8") and (user / "README.md").exists()


def test_conflicting_settings_file_keeps_the_users_version(repos):
    dev, user = repos
    (user / "config" / "schedule.json").write_text('{"times": ["06:30"]}\n', encoding="utf-8")
    release(dev, "3.2", ["Thing."], {"config/schedule.json": '{"times": ["09:00"], "new": 1}\n'})
    updater.check(force=True)
    res = updater.apply()
    assert res["ok"], res
    text = (user / "config" / "schedule.json").read_text(encoding="utf-8")
    assert "06:30" in text and "<<<<<<<" not in text
    assert 'VERSION = "3.2"' in (user / "app" / "version.py").read_text(encoding="utf-8")


def test_falls_back_to_commit_subjects_without_changelog(repos):
    dev, _ = repos
    (dev / "x.txt").write_text("x", encoding="utf-8")
    git(dev, "add", "-A"); git(dev, "commit", "-qm", "Fix the map legend"); git(dev, "push", "-q", "origin", "main")
    st = updater.check(force=True)
    assert st["behind"] == 1 and st["notes"] == [] and st["commits"] == ["Fix the map legend"]


def test_refuses_while_busy(repos, monkeypatch):
    dev, _ = repos
    release(dev, "3.2", ["Thing."])
    updater.check(force=True)
    monkeypatch.setattr(updater, "_busy", lambda: "A run is in progress.")
    res = updater.apply()
    assert not res["ok"] and "run is in progress" in res["message"]


def test_dashboard_banner_and_dialog(repos, monkeypatch):
    dev, _ = repos
    release(dev, "3.2", ["**Faster** screening."])
    updater.check(force=True)
    from app.web import app
    html = app.test_client().get("/").get_data(as_text=True)
    assert "Update available: v3.2" in html and "<b>Faster</b> screening." in html and 'id="update-dialog"' in html
    updater.STATE["behind"] = 0
    assert "update-dialog" not in app.test_client().get("/").get_data(as_text=True)


def test_not_a_git_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "ROOT", tmp_path)
    st = updater.check(force=True)
    assert "isn't a git checkout" in st["error"] and not updater.apply()["ok"]


def test_updating_page_is_standalone():
    """An older running version renders this page right after the new files land: it must not depend on
    the base layout, url_for, or anything else that version might lack."""
    from pathlib import Path
    src = (Path(__file__).parent.parent / "app" / "templates" / "updating.html").read_text(encoding="utf-8")
    assert "{% extends" not in src and "url_for(" not in src and "{% include" not in src
    from jinja2 import Environment
    html = Environment().from_string(src).render(version="3.2")            # bare Jinja, only `version`
    assert "Updating Job Autopilot to v3.2" in html and "/update/progress" in html and 'href = "/?updated=1"' in html


def test_background_update_reports_progress_and_restarts(repos, monkeypatch):
    import time
    dev, _ = repos
    release(dev, "3.2", ["Thing."])
    updater.check(force=True)
    restarted = []
    monkeypatch.setattr(updater, "restart_soon", lambda delay=1.5: restarted.append(delay))
    updater.PROGRESS.update(stage="", pct=0, message="", target=None, error="", lock=False)
    res = updater.start()
    assert res["ok"] and res["target"] == "3.2"
    for _ in range(100):
        if restarted:
            break
        time.sleep(0.05)
    assert restarted and updater.PROGRESS["stage"] == "restart" and updater.PROGRESS["lock"]
    assert db.kv_get("updated_from")["v"] == "3.1"

    from app.web import app
    c = app.test_client()
    r = c.get("/")
    assert r.status_code == 503 and "is updating" in r.get_data(as_text=True)    # no half-updated pages
    assert c.get("/api/map").status_code == 503
    p = c.get("/update/progress").get_json()
    assert p["stage"] == "restart" and p["target"] == "3.2"
    assert c.get("/healthz").status_code == 200
    updater.PROGRESS.update(stage="", lock=False)
    assert c.get("/").status_code == 200


def test_background_update_failure_unlocks(repos, monkeypatch):
    import time
    dev, _ = repos
    release(dev, "3.2", ["Thing."])
    updater.check(force=True)
    def fake_git(*a, **k):
        if a[0] == "pull":
            raise RuntimeError("network down")
        return "abc" if a[0] == "rev-parse" else ""
    monkeypatch.setattr(updater, "_git", fake_git)
    updater.PROGRESS.update(stage="", pct=0, message="", target=None, error="", lock=False)
    assert updater.start()["ok"]
    for _ in range(100):
        if updater.PROGRESS["stage"] == "error":
            break
        time.sleep(0.05)
    assert updater.PROGRESS["stage"] == "error" and "network down" in updater.PROGRESS["error"] and not updater.PROGRESS["lock"]
    monkeypatch.setattr(updater, "_busy", lambda: "A run is in progress.")
    updater.PROGRESS["stage"] = ""
    assert not updater.start()["ok"]


def test_restart_releases_the_inherited_listening_socket(monkeypatch):
    """An in-place restart inherits werkzeug's listening socket; the new copy must close it or it can't bind."""
    import os
    import socket
    from app.web import _release_inherited_socket
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0)); s.listen(); port = s.getsockname()[1]
    fd = s.detach()
    monkeypatch.setenv("WERKZEUG_SERVER_FD", str(fd))
    _release_inherited_socket(port)
    assert "WERKZEUG_SERVER_FD" not in os.environ
    again = socket.socket(); again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    again.bind(("127.0.0.1", port)); again.close()                               # the port is free again

    r, w = os.pipe()                                                             # a number that isn't our socket: left alone
    monkeypatch.setenv("WERKZEUG_SERVER_FD", str(r))
    _release_inherited_socket(port)
    os.fstat(r); os.close(r); os.close(w)


def test_waits_for_a_busy_port_then_gives_up():
    import socket
    from app.web import _wait_for_port
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(); port = s.getsockname()[1]
    assert _wait_for_port(port, seconds=0.6) is False                       # still held: give up (and exit for a restart)
    s.close()
    assert _wait_for_port(port, seconds=2) is True


def test_conflicting_code_file_takes_the_update_and_keeps_your_edits_in_a_stash(repos):
    """Keeping the user's whole old copy of a code file silently dropped every upstream change to it (a Windows
    install lost 248 new lines of mailsync.py). Code takes the update; the user's edits stay in the stash."""
    dev, user = repos
    (dev / "app" / "mailer.py").write_text("def read():\n    return 'v1'\n", encoding="utf-8")
    git(dev, "add", "-A"); git(dev, "commit", "-qm", "mailer"); git(dev, "push", "-q", "origin", "main")
    git(user, "pull", "-q")
    (user / "app" / "mailer.py").write_text("def read():\n    return 'my outlook reader'\n", encoding="utf-8")   # local edit
    (user / "config" / "schedule.json").write_text('{"times": ["06:30"]}\n', encoding="utf-8")
    release(dev, "3.2", ["Better matching."], {"app/mailer.py": "def read():\n    return 'v2 with fix type'\n",
                                              "config/schedule.json": '{"times": ["09:00"]}\n'})
    updater.check(force=True)
    res = updater.apply()
    assert res["ok"], res
    assert "v2 with fix type" in (user / "app" / "mailer.py").read_text(encoding="utf-8")     # the update's code
    assert "06:30" in (user / "config" / "schedule.json").read_text(encoding="utf-8")         # your settings
    stash = subprocess.run(["git", "-C", str(user), "stash", "show", "-p"], capture_output=True, text=True).stdout
    assert "my outlook reader" in stash                                                     # your edit, recoverable
    import json
    assert json.loads(db.kv_get("update_set_aside")["v"]) == ["app/mailer.py"]


def test_windows_restart_goes_through_task_scheduler(monkeypatch):
    s = updater._windows_restart_script(1234, task=True)
    assert "Wait-Process -Id 1234" in s and "Start-ScheduledTask -TaskName 'JobAutopilot Dashboard'" in s
    s = updater._windows_restart_script(1234, task=False)
    assert "Start-Process -WindowStyle Hidden" in s and "'app.cli'" in s and "'serve'" in s
    calls = []
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater, "_restart_windows", lambda: calls.append("win"))
    monkeypatch.setattr(updater.os, "execv", lambda *a: calls.append("execv"))
    monkeypatch.setattr(updater.os, "chdir", lambda *a: None)
    monkeypatch.setattr(updater.time, "sleep", lambda *a: None)
    class Now:
        def __init__(self, target, daemon=None, **k): self.t = target
        def start(self): self.t()
    monkeypatch.setattr(updater.threading, "Thread", Now)
    updater.restart_soon(0)
    assert calls == ["win"]                                                  # never execv on Windows
