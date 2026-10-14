"""Board directory: thousands of company job boards collected from public new-grad / internship lists.

The community lists on GitHub (SimplifyJobs, speedyapply, vanshb03, ...) link to postings on each company's
own Greenhouse, Lever, Ashby, Workday, or SmartRecruiters board. Every board seen there joins the watch list
(table discovered_boards, source='directory'), so each run reads those companies' boards directly through
their public APIs and catches same-day postings that never make it onto a list. Refreshed once a day.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import requests

from .. import db
from . import HEADERS

URL_RE = re.compile(r"https?://[^\s)\"'<>|\]]+")
SKIP_SLUGS = {"embed", "v1", "boards", "jobs", "job", "careers", "api", "en-us", "en", "j", "o"}


def board_from_url(url: str) -> tuple[str, str] | None:
    """(ats, slug) for a posting URL on a supported public board, else None."""
    u = urlparse(url.strip())
    host, parts = u.netloc.lower(), [p for p in u.path.split("/") if p]
    if not parts:
        return None
    if host.endswith("greenhouse.io"):
        if parts[0] == "embed":
            m = re.search(r"(?:^|&)for=([\w-]+)", u.query)
            return ("greenhouse", m.group(1).lower()) if m else None
        return ("greenhouse", parts[0].lower()) if parts[0].lower() not in SKIP_SLUGS else None
    if host == "jobs.lever.co":
        return ("lever", parts[0].lower())
    if host == "jobs.ashbyhq.com":
        return ("ashby", parts[0].lower())
    if host.endswith("myworkdayjobs.com"):
        site = parts[1] if parts[0].lower() in ("en-us", "en", "en-gb") and len(parts) > 1 else parts[0]
        if site.lower() in ("job", "details"):
            return None
        return ("workday", f"{host}|{host.split('.')[0]}|{site}")
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"):
        return ("smartrecruiters", parts[0])
    return None


def _pretty(slug: str) -> str:
    base = slug.split("|")[1] if "|" in slug else slug
    return re.sub(r"[-_]+", " ", base).strip().title()


def collect(lists: list[str]) -> dict[str, dict]:
    """key -> {ats, slug, name} from every list (JSON listings with company names, or markdown with links)."""
    boards: dict[str, dict] = {}
    for url in lists:
        try:
            r = requests.get(url, headers=HEADERS, timeout=60)
            r.raise_for_status()
        except requests.RequestException:
            continue
        pairs: list[tuple[str, str]] = []
        if url.endswith(".json"):
            try:
                pairs = [(x.get("url") or "", x.get("company_name") or "") for x in r.json() if isinstance(x, dict)]
            except ValueError:
                pass
        else:
            for line in r.text.splitlines():
                cells = [c.strip() for c in line.split("|")]
                name = re.sub(r"\[([^\]]+)\]\([^)]*\)|<[^>]+>|\*", r"\1", cells[1]).strip() if len(cells) > 2 else ""
                pairs += [(u, name) for u in URL_RE.findall(line)]
        for u, name in pairs:
            b = board_from_url(u)
            if not b:
                continue
            key = f"{b[0]}:{b[1]}"
            if key not in boards or (name and boards[key]["name"] == _pretty(b[1])):
                boards[key] = {"ats": b[0], "slug": b[1], "name": name.strip() or _pretty(b[1])}
    return boards


def refresh(cfg: dict, log=print) -> int:
    """Add new boards from the lists (at most once per refresh_hours). Returns how many were added."""
    last = db.kv_get("directory_refreshed")
    if last:
        from datetime import datetime, timedelta, timezone
        if datetime.now(timezone.utc) - datetime.fromisoformat(last["at"]) < timedelta(hours=cfg.get("refresh_hours", 24)):
            return 0
    wanted = set(cfg.get("ats") or ["greenhouse", "lever", "ashby", "workday", "smartrecruiters"])
    boards = {k: v for k, v in collect(cfg.get("lists") or []).items() if v["ats"] in wanted}
    have = {r["key"] for r in db.fetchall("SELECT key FROM discovered_boards")}
    added = 0
    for key, b in boards.items():
        if key in have:
            continue
        db.execute("INSERT OR IGNORE INTO discovered_boards (key, ats, slug, name, h1b, source, enabled, found_at) "
                   "VALUES (?,?,?,?,?,?,1,?)", (key, b["ats"], b["slug"], b["name"], "", "directory", db.now()))
        added += 1
    db.kv_set("directory_refreshed", json.dumps({"boards": len(boards), "added": added}))
    log(f"  board directory: {len(boards):,} boards on the public lists, {added:,} new added to the watch list")
    return added
