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
    (dev / "app" / "version.py").write_text('VERSION = "3.1"\n')
    (dev / "config").mkdir()
    (dev / "config" / "schedule.json").write_text('{"times": ["08:00"]}\n')
    (dev / "CHANGELOG.md").write_text("# Changelog\n\n## 3.1 (2026-10-20)\n- First.\n")
    git(dev, "add", "-A"); git(dev, "commit", "-qm", "v3.1"); git(dev, "push", "-q", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(remote), str(user)], check=True, capture_output=True)
    git(user, "config", "user.email", "u@example.com"); git(user, "config", "user.name", "User")
    monkeypatch.setattr(updater, "ROOT", user)
    monkeypatch.setattr(updater, "VERSION", "3.1")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    monkeypatch.setattr(updater, "_busy", lambda: "")
    updater.STATE.update(behind=0, remote_version=None, notes=[], commits=[], checked_at=None, error="", updating=False)
    return dev, user


def release(dev, version, notes, extra=None):
    (dev / "app" / "version.py").write_text(f'VERSION = "{version}"\n')
    cl = (dev / "CHANGELOG.md").read_text().replace("# Changelog\n\n", f"# Changelog\n\n## {version} (2026-10-21)\n" + "".join(f"- {n}\n" for n in notes) + "\n")
    (dev / "CHANGELOG.md").write_text(cl)
    for path, text in (extra or {}).items():
        (dev / path).write_text(text)
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
    assert 'VERSION = "3.2"' in (user / "app" / "version.py").read_text()
    assert not updater.update_available()


def test_keeps_users_settings_edits(repos):
    dev, user = repos
    (user / "config" / "schedule.json").write_text('{"times": ["06:30"]}\n')          # user's own setting
    release(dev, "3.2", ["Thing."], {"README.md": "hi\n"})
    updater.check(force=True)
    assert updater.apply()["ok"]
    assert "06:30" in (user / "config" / "schedule.json").read_text() and (user / "README.md").exists()


def test_conflicting_settings_file_keeps_the_users_version(repos):
    dev, user = repos
    (user / "config" / "schedule.json").write_text('{"times": ["06:30"]}\n')
    release(dev, "3.2", ["Thing."], {"config/schedule.json": '{"times": ["09:00"], "new": 1}\n'})
    updater.check(force=True)
    res = updater.apply()
    assert res["ok"], res
    text = (user / "config" / "schedule.json").read_text()
    assert "06:30" in text and "<<<<<<<" not in text
    assert 'VERSION = "3.2"' in (user / "app" / "version.py").read_text()


def test_falls_back_to_commit_subjects_without_changelog(repos):
    dev, _ = repos
    (dev / "x.txt").write_text("x")
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
