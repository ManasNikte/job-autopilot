"""The online-dashboard connector against a fake Supabase (auth + REST), and the local origin check."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest

from app import cloud, db


class FakeResp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body
        self.text = "" if body is None else json.dumps(body)

    def json(self):
        return self._body


class FakeSupabase:
    """Just enough of GoTrue + PostgREST for the connector: tokens, upserts, filters, PATCH with status guard."""

    def __init__(self):
        self.tables = {"machines": [], "snapshots": [], "jobs": [], "email_updates": [], "commands": []}
        self.password_logins = 0

    def post(self, url, json=None, headers=None, timeout=None):
        assert "/auth/v1/token" in url and headers["apikey"] == "anon"
        if "grant_type=password" in url:
            self.password_logins += 1
            if json["password"] != "correct horse 1":
                return FakeResp(400, {"error_description": "Invalid login credentials"})
        return FakeResp(200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, "user": {"id": "user-1"}})

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        assert headers["Authorization"] == "Bearer AT"
        table = urlparse(url).path.rsplit("/", 1)[-1]
        rows = self.tables[table]
        params = dict(params or {})
        filt = {k: v.split(".", 1)[1] for k, v in params.items() if isinstance(v, str) and v.startswith("eq.")}
        infilt = {k: [x.strip('"') for x in v[4:-1].split(",")] for k, v in params.items()
                  if isinstance(v, str) and v.startswith("in.(")}
        match = lambda r: all(str(r.get(k)) == v for k, v in filt.items()) and all(str(r.get(k)) in vs for k, vs in infilt.items())
        if method == "POST":
            items = json if isinstance(json, list) else [json]
            keys = (params.get("on_conflict") or "").split(",") if params.get("on_conflict") else None
            out = []
            for it in items:
                it = dict(it)
                if table == "machines":
                    it.setdefault("id", f"m-{len(rows) + 1}")
                if table == "commands":
                    it.setdefault("id", len(rows) + 1)
                old = next((r for r in rows if keys and all(r.get(k) == it.get(k) for k in keys)), None)
                if old:
                    old.update(it)
                else:
                    rows.append(it)
                out.append(it)
            return FakeResp(201, out)
        if method == "GET":
            return FakeResp(200, [dict(r) for r in rows if match(r)])
        if method == "PATCH":
            hit = [r for r in rows if match(r)]
            for r in hit:
                r.update(json)
            return FakeResp(200, [dict(r) for r in hit])
        if method == "DELETE":
            self.tables[table] = [r for r in rows if not match(r)]
            return FakeResp(204, None)
        raise AssertionError(method)


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    db.upsert_job({"id": "g:1", "company": "Acme", "title": "Software Engineer", "status": "applied",
                   "applied_at": datetime.now(timezone.utc).isoformat()})
    db.upsert_job({"id": "g:2", "company": "Initech", "title": "Data Engineer", "status": "filtered_out"})
    monkeypatch.setattr(cloud, "config", lambda: {"supabase_url": "https://x.supabase.co", "anon_key": "anon", "site_url": "https://site"})
    monkeypatch.setattr(cloud, "STATE_FILE", tmp_path / "cloud.json")
    secrets = {}
    import app.mailsync as ms
    monkeypatch.setattr(ms, "set_secret", lambda k, v: secrets.__setitem__(k, v))
    monkeypatch.setattr(ms, "get_secret", lambda k: secrets.get(k))
    monkeypatch.setattr(ms, "delete_secret", lambda k: secrets.pop(k, None))
    cloud._token.update(access="", exp=0)
    sb = FakeSupabase()
    monkeypatch.setattr(cloud.requests, "post", sb.post)
    monkeypatch.setattr(cloud.requests, "request", sb.request)
    submitted = []
    import app.worker as worker
    monkeypatch.setattr(worker, "submit", lambda kind, arg=None: submitted.append((kind, arg)) or True)
    monkeypatch.setattr(worker, "resume", lambda: False)
    sb.secrets, sb.submitted = secrets, submitted
    return sb


def test_connect_keeps_only_a_refresh_token(fake, tmp_path):
    with pytest.raises(RuntimeError, match="Invalid login"):
        cloud.connect("me@example.com", "wrong", "Laptop")
    st = cloud.connect("me@example.com", "correct horse 1", "Laptop")
    assert st["machine_id"] == "m-1" and fake.tables["machines"][0]["user_id"] == "user-1"
    assert fake.secrets == {cloud.KEY: "RT"}                              # no password anywhere
    assert "correct horse" not in (tmp_path / "cloud.json").read_text()


def test_sync_pushes_summaries_and_runs_allowed_requests(fake):
    cloud.connect("me@example.com", "correct horse 1", "Laptop")
    now = datetime.now(timezone.utc)
    fake.tables["commands"] += [
        {"id": 1, "machine_id": "m-1", "kind": "run", "status": "pending", "created_at": now.isoformat()},
        {"id": 2, "machine_id": "m-1", "kind": "rm -rf", "status": "pending", "created_at": now.isoformat()},
        {"id": 3, "machine_id": "m-1", "kind": "mail", "status": "pending", "created_at": (now - timedelta(hours=9)).isoformat()},
    ]
    res = cloud.sync_once(log=lambda *_: None)
    snap = fake.tables["snapshots"][0]
    assert snap["machine_id"] == "m-1" and snap["data"]["kpis"]["applied_total"] == 1
    assert [j["job_id"] for j in fake.tables["jobs"]] == ["g:1"]           # filtered-out jobs aren't shared
    assert res["queued"] == 1 and fake.submitted == [("run", "phone")]
    status = {c["id"]: c["status"] for c in fake.tables["commands"]}
    assert status == {1: "done", 2: "failed", 3: "expired"}
    assert fake.password_logins == 1                                        # later calls use the token


def test_disconnect_removes_machine_and_token(fake):
    cloud.connect("me@example.com", "correct horse 1", "Laptop")
    cloud.disconnect()
    assert fake.tables["machines"] == [] and fake.secrets == {} and not cloud.connected()


def test_cross_site_posts_are_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    from app.web import app
    c = app.test_client()
    assert c.post("/email/check", headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/email/check", headers={"Origin": "http://localhost"}).status_code in (200, 302)
    assert c.post("/email/check").status_code in (200, 302)                 # same-origin form (no Origin header)


def test_operator_stats_only_when_the_site_returns_them(fake, monkeypatch):
    cloud.connect("me@example.com", "correct horse 1", "Laptop")
    replies = {"value": None}

    def post(url, json=None, headers=None, timeout=None):
        if "/rpc/operator_stats" in url:
            return FakeResp(200, replies["value"])
        return fake.post(url, json=json, headers=headers, timeout=timeout)
    monkeypatch.setattr(cloud.requests, "post", post)
    assert cloud.operator_stats() is None                                  # not the operator: null
    replies["value"] = {"users": 7, "active": 4, "connected": 5}
    assert cloud.operator_stats()["active"] == 4


def test_footer_shows_user_counts_only_for_the_operator(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    from app.web import app
    c = app.test_client()
    monkeypatch.setitem(cloud.STATUS, "operator", None)
    assert "op-stats" not in c.get("/").get_data(as_text=True)
    monkeypatch.setitem(cloud.STATUS, "operator", {"users": 12, "active": 5})
    html = c.get("/").get_data(as_text=True)
    assert "12 users" in html and "5 active" in html
    assert "op-stats" not in c.get("/settings").get_data(as_text=True)    # dashboard footer only


def test_operator_gets_one_email_per_batch_of_new_people(fake, monkeypatch):
    import app.mailsync as ms
    cloud.connect("me@example.com", "correct horse 1", "Laptop")
    sent, calls = [], []
    events = {"accounts": [{"email": "ava@example.com", "name": "Ava", "opt_in": True}],
              "computers": [{"email": "ava@example.com", "name": "Ava", "computer": "Ava's MacBook"}],
              "as_of": "2026-10-20T15:00:00+00:00"}
    monkeypatch.setattr(cloud, "_rpc", lambda name, args: calls.append(args["since"]) or events)
    monkeypatch.setattr(ms, "send_to_self", lambda subj, body: sent.append((subj, body)) or True)
    assert cloud.notify_operator(lambda *_: None) == 0 and not sent           # first run only sets the start
    assert cloud.notify_operator(lambda *_: None) == 2
    subj, body = sent[0]
    assert subj == "Job Autopilot: 1 new account and 1 new computer"
    assert "Ava <ava@example.com> (opted in to updates)" in body and "Ava's MacBook" in body
    assert (db.kv_get("op_notify_since") or {}).get("v") == "2026-10-20T15:00:00+00:00"
    monkeypatch.setattr(ms, "send_to_self", lambda subj, body: False)        # no Gmail: don't advance
    events["as_of"] = "2026-10-20T16:00:00+00:00"
    cloud.notify_operator(lambda *_: None)
    assert (db.kv_get("op_notify_since") or {}).get("v") == "2026-10-20T15:00:00+00:00"


def test_users_page_is_operator_only_and_shows_no_companies(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    from app.web import app
    c = app.test_client()
    monkeypatch.setattr(cloud, "connected", lambda: True)
    monkeypatch.setattr(cloud, "operator_users", lambda: None)
    assert c.get("/users").status_code == 404                               # everyone else: no such page
    monkeypatch.setattr(cloud, "operator_users", lambda: [
        {"name": "John Doe", "email": "john@example.com", "city": "Boston, MA", "joined": "2026-10-01T00:00:00+00:00",
         "computers": 1, "last_seen": None, "applied_total": 40, "applied_week": 5, "applied_14d": 11},
        {"name": "Idle", "email": "idle@example.com", "city": None, "joined": "2026-10-01T00:00:00+00:00",
         "computers": 1, "last_seen": None, "applied_total": 9, "applied_week": 0, "applied_14d": 0}])
    html = c.get("/users").get_data(as_text=True)
    assert "John Doe" in html and "Boston, MA" in html and "Not shared" in html
    assert html.count("badge apply\">Active") == 1 and "Inactive" in html


def test_city_defaults_to_form_presets_and_can_be_cleared(fake, tmp_path, monkeypatch):
    import app.config as cfg
    f = tmp_path / "applicant.json"
    f.write_text(json.dumps({"location": "Austin, TX"}))
    monkeypatch.setattr(cfg, "APPLICANT_FILE", f)
    cloud.connect("me@example.com", "correct horse 1", "Laptop")
    assert cloud.city() == "Austin, TX" and fake.tables["machines"][0]["city"] == "Austin, TX"
    cloud.sync_once(log=lambda *_: None)
    assert fake.tables["machines"][0]["city"] == "Austin, TX"
    assert cloud.set_city("  ") == "" and fake.tables["machines"][0]["city"] == ""
    f.write_text(json.dumps({"location": "City, ST"}))
    assert cloud.default_city() == ""


def test_job_leaving_shared_statuses_is_removed_online_and_reconnect_resends(fake):
    cloud.connect("me@example.com", "correct horse 1", "Laptop")
    cloud.sync_once(log=lambda *_: None)
    assert [j["job_id"] for j in fake.tables["jobs"]] == ["g:1"]
    db.update_job("g:1", status="rejected")                                  # you skipped it
    cloud.sync_once(log=lambda *_: None)
    assert fake.tables["jobs"] == []
    db.update_job("g:1", status="applied")
    cloud.disconnect()
    assert db.kv_get("cloud_jobs_since") is None
    cloud.connect("me@example.com", "correct horse 1", "Laptop")
    cloud.sync_once(log=lambda *_: None)
    assert [j["job_id"] for j in fake.tables["jobs"]] == ["g:1"]
