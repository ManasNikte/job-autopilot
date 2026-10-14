"""SQLite storage. One file, no server."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,               -- ats:slug:job_id
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    url TEXT,
    apply_url TEXT,
    ats TEXT,
    posted_at TEXT,                    -- ISO UTC
    fetched_at TEXT,
    description TEXT,
    role_family TEXT,
    h1b_note TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    status_reason TEXT,
    verdict TEXT,                      -- APPLY | APPLY WITH RISK | DON'T APPLY
    match_score INTEGER,
    keyword_relevancy INTEGER,
    rw_score INTEGER,
    final_call TEXT,                   -- recruiter teardown: YES | MAYBE | NO
    screen_json TEXT,
    tailor_json TEXT,
    checks_json TEXT,
    resume_pdf TEXT, resume_docx TEXT, cover_pdf TEXT, cover_docx TEXT,
    applied_at TEXT,
    notes TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_posted ON jobs(posted_at);
CREATE INDEX IF NOT EXISTS jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS jobs_applied ON jobs(applied_at);

CREATE TABLE IF NOT EXISTS interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'open',   -- open | resolved | dismissed
    resolution TEXT,
    created_at TEXT, resolved_at TEXT,
    UNIQUE(job_id, kind, title)
);

CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    target TEXT NOT NULL,                  -- hiring_manager | recruiter | alumni
    search_url TEXT,
    message TEXT,
    status TEXT NOT NULL DEFAULT 'drafted', -- drafted | sent | accepted | replied | skipped
    person_name TEXT, profile_url TEXT,
    sent_at TEXT, created_at TEXT, updated_at TEXT,
    UNIQUE(job_id, target)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT,
    status TEXT, stats_json TEXT, log TEXT
);

CREATE TABLE IF NOT EXISTS source_status (
    key TEXT PRIMARY KEY, company TEXT, ats TEXT, slug TEXT,
    ok INTEGER, jobs_seen INTEGER, error TEXT, checked_at TEXT
);

CREATE TABLE IF NOT EXISTS progress (
    job_id TEXT PRIMARY KEY, kind TEXT, stage TEXT,
    base REAL, span REAL, step TEXT, stage_started REAL, started REAL
);
CREATE TABLE IF NOT EXISTS timings (id INTEGER PRIMARY KEY AUTOINCREMENT, step TEXT, secs REAL, at TEXT);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, at TEXT);

CREATE TABLE IF NOT EXISTS h1b_employers (
    name_norm TEXT PRIMARY KEY, name TEXT, approvals INTEGER, denials INTEGER, fiscal_years TEXT
);
"""

_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_local = threading.local()


@contextmanager
def connect():
    """One reused connection per thread. (Opening a fresh connection per query and re-running
    PRAGMA journal_mode on each one took an exclusive lock thousands of times per run and starved
    the dashboard; WAL mode is set once in init() and is stored in the database file.)"""
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != str(DB_PATH):   # tests may point DB_PATH elsewhere
        if conn is not None:
            conn.close()
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn, _local.path = conn, str(DB_PATH)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


MIGRATIONS = {   # columns added after the first release; applied idempotently on startup
    "jobs": {"source": "TEXT", "h1b_signal": "TEXT", "h1b_level": "TEXT", "startup": "INTEGER DEFAULT 0", "company_stage": "TEXT",
             "salary": "TEXT", "salary_max": "INTEGER"},
    "runs": {"source": "TEXT"},
}

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS discovered_boards (
    key TEXT PRIMARY KEY, ats TEXT, slug TEXT, name TEXT, h1b TEXT, source TEXT, enabled INTEGER DEFAULT 1, found_at TEXT
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, company TEXT, company_key TEXT, title TEXT, connection TEXT,
    phone TEXT, email TEXT, linkedin TEXT, stage TEXT DEFAULT 'unknown', notes TEXT,
    job_links TEXT DEFAULT '[]', last_contacted TEXT, source TEXT DEFAULT 'manual',
    created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS contacts_company ON contacts(company_key);
CREATE TABLE IF NOT EXISTS referral_asks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL, contact_id INTEGER NOT NULL, message TEXT,
    status TEXT DEFAULT 'drafted',
    created_at TEXT, updated_at TEXT, asked_at TEXT,
    UNIQUE(job_id, contact_id)
);
CREATE TABLE IF NOT EXISTS sponsors (      -- companies seen sponsoring H-1B (Jobright, Simplify, HN, Claude screens)
    name_norm TEXT PRIMARY KEY, name TEXT, signal TEXT, source TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS sponsor_checks ( -- web lookups for companies missing from the USCIS data (cached 90 days)
    name_norm TEXT PRIMARY KEY, name TEXT, verdict TEXT, evidence TEXT, url TEXT, checked_at TEXT
);
CREATE TABLE IF NOT EXISTS feed_status (
    feed TEXT PRIMARY KEY, ok INTEGER, seen INTEGER, kept INTEGER, error TEXT, checked_at TEXT
);
"""


def init() -> None:
    with _lock, connect() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        c.executescript(EXTRA_SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        c.execute("UPDATE jobs SET source=ats WHERE source IS NULL")


def upsert_job(job: dict) -> bool:
    """Insert a job if unseen. Returns True when it is new."""
    with _lock, connect() as c:
        if c.execute("SELECT 1 FROM jobs WHERE id=?", (job["id"],)).fetchone():
            return False
        job = {**job, "fetched_at": now(), "updated_at": now()}
        cols = ",".join(job)
        c.execute(f"INSERT INTO jobs ({cols}) VALUES ({','.join('?' * len(job))})", list(job.values()))
        return True


def update_job(job_id: str, **fields) -> None:
    for k, v in list(fields.items()):
        if isinstance(v, (dict, list)):
            fields[k] = json.dumps(v)
    fields["updated_at"] = now()
    sets = ",".join(f"{k}=?" for k in fields)
    with _lock, connect() as c:
        c.execute(f"UPDATE jobs SET {sets} WHERE id=?", [*fields.values(), job_id])


def claim(job_id: str, from_status: str, to_status: str) -> bool:
    """Atomically move a job between statuses; False if another thread already took it."""
    with _lock, connect() as c:
        cur = c.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=? AND status=?", (to_status, now(), job_id, from_status))
        return cur.rowcount == 1


def recover_interrupted() -> None:
    """Jobs that were mid-flight when the app stopped go back in the queue."""
    with _lock, connect() as c:
        c.execute("UPDATE jobs SET status='to_tailor' WHERE status='tailoring'")
        c.execute("UPDATE jobs SET status='to_screen' WHERE status='screening'")
        c.execute("UPDATE runs SET status='interrupted', finished_at=? WHERE status='running'", (now(),))


def kv_set(k: str, v: str) -> None:
    execute("INSERT OR REPLACE INTO kv (k, v, at) VALUES (?,?,?)", (k, v, now()))


def kv_get(k: str) -> dict | None:
    return fetchone("SELECT v, at FROM kv WHERE k=?", (k,))


def existing_ids(prefix: str) -> set[str]:
    with connect() as c:
        return {r[0] for r in c.execute("SELECT id FROM jobs WHERE id LIKE ?", (prefix + "%",))}


def recent_job_keys() -> set[str]:
    """Normalized company+title of postings seen in the last 21 days (for cross-board duplicate checks)."""
    import re as _re
    norm = lambda x: _re.sub(r"[^a-z0-9]", "", (x or "").lower())
    with connect() as c:
        return {norm(r[0]) + "|" + norm(r[1]) for r in c.execute("SELECT company, title FROM jobs WHERE fetched_at >= datetime('now', '-21 days')")}


def similar_job_exists(company: str, title: str) -> bool:
    """The same posting often appears on several boards (company ATS, Jobright, Simplify): keep one."""
    import re as _re
    norm = lambda x: _re.sub(r"[^a-z0-9]", "", (x or "").lower())
    c, t = norm(company), norm(title)
    if not c or not t:
        return False
    with connect() as conn:
        for r in conn.execute("SELECT company, title FROM jobs WHERE fetched_at >= datetime('now', '-21 days')"):
            if norm(r[0]) == c and norm(r[1]) == t:
                return True
    return False


def get_job(job_id: str) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _job(r) if r else None


def _job(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("screen_json", "tailor_json", "checks_json"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d


def query_jobs(where: str = "1=1", params: tuple = (), order: str = "posted_at DESC", limit: int = 500) -> list[dict]:
    with connect() as c:
        rows = c.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY {order} LIMIT ?", (*params, limit)).fetchall()
    return [_job(r) for r in rows]


LIST_COLUMNS = ("id, company, title, location, url, apply_url, posted_at, status, status_reason, verdict, match_score, "
                "keyword_relevancy, rw_score, final_call, resume_pdf, cover_pdf, applied_at, source, h1b_level, h1b_signal, "
                "startup, company_stage, role_family, salary, salary_max, updated_at")


def list_jobs(where: str = "1=1", params: tuple = (), order: str = "posted_at DESC", limit: int = 50, offset: int = 0) -> list[dict]:
    """Lightweight rows for list pages (no descriptions or Claude JSON), one page at a time."""
    with connect() as c:
        rows = c.execute(f"SELECT {LIST_COLUMNS} FROM jobs WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                         (*params, limit, offset)).fetchall()
    return [dict(r) for r in rows]


def count(table: str, where: str = "1=1", params: tuple = ()) -> int:
    with connect() as c:
        return c.execute(f"SELECT count(*) FROM {table} WHERE {where}", params).fetchone()[0]


def add_intervention(job_id: str | None, kind: str, title: str, detail: str = "") -> None:
    """Idempotent: re-raising the same question for the same job reopens nothing new."""
    with _lock, connect() as c:
        if job_id is None and c.execute(  # NULLs never collide in UNIQUE, so dedupe global alerts by hand
                "SELECT 1 FROM interventions WHERE job_id IS NULL AND kind=? AND title=? AND status='open'", (kind, title)).fetchone():
            return
        c.execute(
            "INSERT OR IGNORE INTO interventions (job_id, kind, title, detail, created_at) VALUES (?,?,?,?,?)",
            (job_id, kind, title, detail, now()),
        )


def resolve_interventions(job_id: str, kinds: tuple[str, ...], resolution: str = "auto") -> None:
    q = ",".join("?" * len(kinds))
    with _lock, connect() as c:
        c.execute(
            f"UPDATE interventions SET status='resolved', resolution=?, resolved_at=? "
            f"WHERE job_id=? AND kind IN ({q}) AND status='open'",
            (resolution, now(), job_id, *kinds),
        )


def execute(sql: str, params: tuple = ()) -> None:
    with _lock, connect() as c:
        c.execute(sql, params)


def fetchall(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def fetchone(sql: str, params: tuple = ()) -> dict | None:
    with connect() as c:
        r = c.execute(sql, params).fetchone()
    return dict(r) if r else None
