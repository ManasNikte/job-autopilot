"""Public job-board APIs (no login, no scraping of logged-in pages).

Each fetcher returns a list of normalized dicts:
    {id, company, title, location, url, apply_url, ats, posted_at (ISO UTC), description (plain text)}
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

import requests

TIMEOUT = 30
HEADERS = {"User-Agent": "job-autopilot/1.0 (personal job search tool)"}


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    s = html.unescape(raw)  # Greenhouse double-escapes its HTML
    s = re.sub(r"(?i)<\s*(br|/p|/li|/h\d|/div)\s*/?>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def greenhouse(company: dict) -> list[dict]:
    slug = company["slug"]
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        posted = j.get("first_published") or j.get("updated_at")
        out.append({
            "id": f"greenhouse:{slug}:{j['id']}",
            "company": company["name"],
            "title": j.get("title", "").strip(),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url"),
            "apply_url": j.get("absolute_url"),
            "ats": "greenhouse",
            "posted_at": _iso(datetime.fromisoformat(posted)) if posted else None,
            "description": html_to_text(j.get("content", "")),
        })
    return out


def lever(company: dict) -> list[dict]:
    slug = company["slug"]
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json():
        parts = [j.get("descriptionPlain", "")]
        for lst in j.get("lists", []):
            parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content', ''))}")
        parts.append(j.get("additionalPlain", ""))
        cats = j.get("categories") or {}
        loc = cats.get("location", "")
        if cats.get("allLocations"):
            loc = " • ".join(cats["allLocations"])
        created = j.get("createdAt")
        out.append({
            "id": f"lever:{slug}:{j['id']}",
            "company": company["name"],
            "title": j.get("text", "").strip(),
            "location": loc + (f" ({j['country']})" if j.get("country") else ""),
            "url": j.get("hostedUrl"),
            "apply_url": j.get("applyUrl") or j.get("hostedUrl"),
            "ats": "lever",
            "posted_at": _iso(datetime.fromtimestamp(created / 1000, timezone.utc)) if created else None,
            "description": "\n\n".join(p for p in parts if p).strip(),
        })
    return out


def ashby(company: dict) -> list[dict]:
    slug = company["slug"]
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")) or ""
        loc = " • ".join(l for l in locs if l)
        if country and country.lower() not in loc.lower():
            loc += f" ({country})"
        if j.get("isRemote"):
            loc += " • Remote"
        posted = j.get("publishedAt")
        out.append({
            "id": f"ashby:{slug}:{j['id']}",
            "company": company["name"],
            "title": j.get("title", "").strip(),
            "location": loc,
            "url": j.get("jobUrl"),
            "apply_url": j.get("applyUrl") or j.get("jobUrl"),
            "ats": "ashby",
            "posted_at": _iso(datetime.fromisoformat(posted.replace("Z", "+00:00"))) if posted else None,
            "description": j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
        })
    return out


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}


def fetch(company: dict) -> list[dict]:
    return FETCHERS[company["ats"]](company)
