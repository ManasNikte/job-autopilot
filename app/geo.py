"""Map pins for job locations: split a posting's location into places and geocode each one once.

Geocoding uses OpenStreetMap's Nominatim (public, no key) within its usage policy: one request per second,
an identifying User-Agent, and every answer (including misses) cached forever in the local database. Only
place names like "Austin, TX" are sent. Remote / country-only locations get no pin.
"""
from __future__ import annotations

import re
import threading
import time

import requests

from . import db

UA = {"User-Agent": "job-autopilot/1.0 (personal job search dashboard; local use)"}
SPLIT = re.compile(r"\s*(?:•|;|\||/|\bor\b)\s*", re.I)
NOT_A_PLACE = re.compile(r"^(remote|hybrid|onsite|on-site|in office|full[- ]time|part[- ]time|na|n/a|anywhere|multiple locations|"
                         r"\d+ locations|united states( of america)?|usa?|u\.s\.a?\.?|north america|americas)$", re.I)
SCHEMA = "CREATE TABLE IF NOT EXISTS geocode (q TEXT PRIMARY KEY, lat REAL, lng REAL, ok INTEGER, at TEXT)"
_lock = threading.Lock()
_worker: threading.Thread | None = None


def places(location: str, limit: int = 3) -> list[str]:
    """'San Francisco • Seattle (United States)' -> ['San Francisco', 'Seattle']; 'US-CA-Menlo Park' -> ['Menlo Park, CA']."""
    out = []
    for part in SPLIT.split(location or ""):
        p = re.sub(r"\((?:hq|us|usa|united states|remote|hybrid|[^)]*\bonsite\b[^)]*)\)", "", part, flags=re.I)
        p = re.sub(r"\(([^)]*)\)", r", \1", p)                      # "New York City (FiDi)" -> "New York City, FiDi"
        m = re.match(r"^usa?\s*-\s*([a-z]{2})\s*-\s*([^~\d(]+)", p.strip(), re.I)   # Workday "US-TX-McKinney-513WD"
        if m:
            p = f"{m.group(2).strip(' -')}, {m.group(1).upper()}"
        p = re.sub(r"^(remote|hybrid)\s*[-–,:]\s*", "", p.strip(), flags=re.I)
        p = re.sub(r",?\s*(united states( of america)?|usa|us)\s*$", "", p, flags=re.I)
        p = re.sub(r"\s*,\s*fidi\b|,\s*$", "", p, flags=re.I).strip(" ,-")
        if re.match(r"^(remote|virtual|work from home|wfh|anywhere)\b", p, re.I):
            continue                                                # "Remote in USA", "Remote Office": no city to pin
        if p and not NOT_A_PLACE.match(p) and len(p) > 1 and p not in out:
            out.append(p)
        if len(out) >= limit:
            break
    return out


def cached(q: str) -> dict | None:
    return db.fetchone("SELECT lat, lng, ok FROM geocode WHERE q=?", (q.lower(),))


def _lookup(q: str) -> None:
    lat = lng = None
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": q, "format": "json", "limit": 1, "countrycodes": "us"}, headers=UA, timeout=20)
        if r.ok and r.json():
            lat, lng = float(r.json()[0]["lat"]), float(r.json()[0]["lon"])
    except (requests.RequestException, ValueError, KeyError):
        return                                   # network trouble: try again next time, don't cache a miss
    db.execute("INSERT OR REPLACE INTO geocode VALUES (?,?,?,?,?)", (q.lower(), lat, lng, int(lat is not None), db.now()))


def _queries(job: dict) -> list[str]:
    """Places to geocode for a job; if none of them resolves (e.g. a building name like "Science Hall 2"), the company itself."""
    ps = places(job.get("location") or "")
    if ps and job.get("company") and all((c := cached(p)) and not c["ok"] for p in ps):
        return [job["company"]]
    return ps


def warm(jobs: list[dict]) -> int:
    """Geocode uncached places in a background thread (1 request/second). Returns how many are still pending."""
    global _worker
    db.execute(SCHEMA)
    todo = sorted({q for j in jobs for q in _queries(j) if not cached(q)})
    if todo:
        with _lock:
            if not (_worker and _worker.is_alive()):
                def run():
                    for q in todo:
                        if not cached(q):
                            _lookup(q)
                            time.sleep(1.1)
                _worker = threading.Thread(target=run, daemon=True, name="geocoder")
                _worker.start()
    return len(todo)   # company fallbacks join the queue on the next poll, once a job's places are known to miss


def pins(jobs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Group jobs by geocoded place. Returns (pins, jobs that could not be placed on the map)."""
    db.execute(SCHEMA)
    by_place: dict[tuple, dict] = {}
    unmapped = []
    for j in jobs:
        hit = False
        qs = _queries(j)
        for p in qs:
            c = cached(p)
            if c and c["ok"]:
                rejected = j["status"] == "closed"          # rejected applications get their own (red) pin
                key = (round(c["lat"], 3), round(c["lng"], 3), rejected)
                label = p if p != j.get("company") or p in places(j.get("location") or "") else f"{p} (company location)"
                pin = by_place.setdefault(key, {"lat": c["lat"], "lng": c["lng"], "place": label, "jobs": []})
                pin["jobs"].append({"id": j["id"], "company": j["company"], "title": j["title"], "status": j["status"]})
                hit = True
        if not hit:
            unmapped.append({"id": j["id"], "company": j["company"], "title": j["title"], "status": j["status"],
                             "location": j.get("location") or "", "pending": any(cached(q) is None for q in qs)})
    for (lat, lng, rejected), pin in by_place.items():   # nudge a red pin aside only if it shares a city with others
        if rejected and (lat, lng, False) in by_place:
            pin["lng"] += 0.12
    return list(by_place.values()), unmapped
