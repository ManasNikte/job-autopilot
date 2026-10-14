"""Public job-board APIs (no login, no scraping of logged-in pages).

Each fetcher returns a list of normalized dicts:
    {id, company, title, location, url, apply_url, ats, posted_at (ISO UTC), description (plain text)}
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

import requests

from ..salary import parse as salary_parse

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


def _gh_pay(j: dict) -> tuple[int, int] | None:
    rs = [r for r in j.get("pay_input_ranges") or [] if (r.get("currency_type") or "USD") == "USD" and r.get("max_cents")]
    return (min(r["min_cents"] for r in rs) // 100, max(r["max_cents"] for r in rs) // 100) if rs else None


def _gh_detail(slug: str):
    def enrich(job: dict) -> dict:
        jid = job["id"].rsplit(":", 1)[1]
        d = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}?pay_transparency=true",
                         timeout=TIMEOUT, headers=HEADERS).json()
        return {"description": html_to_text(d.get("content", "")), "pay": _gh_pay(d)}
    return enrich


def greenhouse(company: dict) -> list[dict]:
    """Curated companies: one call with descriptions. Directory boards (light=True): titles only first;
    full descriptions are fetched later for the few postings that pass the title/date/location filter."""
    slug, light = company["slug"], company.get("light")
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs" + ("" if light else "?content=true&pay_transparency=true"),
                     timeout=TIMEOUT, headers=HEADERS)
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
            "pay": _gh_pay(j),
            **({"enrich": _gh_detail(slug)} if light else {}),
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
            "pay": _lever_pay(j.get("salaryRange")),
        })
    return out


def _lever_pay(sr: dict | None) -> tuple[int, int] | None:
    if not sr or (sr.get("currency") or "USD") != "USD" or not sr.get("max"):
        return None
    mult = {"per-hour-wage": 2080, "per-month-salary": 12}.get(sr.get("interval"), 1)
    return int(sr.get("min") or sr["max"]) * mult, int(sr["max"]) * mult


def ashby(company: dict) -> list[dict]:
    slug = company["slug"]
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true", timeout=TIMEOUT, headers=HEADERS)
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
            "pay": salary_parse((j.get("compensation") or {}).get("scrapeableCompensationSalarySummary") or ""),
        })
    return out


BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) job-autopilot/1.0", "Accept": "application/json"}
WD_AGE = re.compile(r"posted (today|yesterday|(\d+)\+? days? ago)", re.I)


def workday(company: dict) -> list[dict]:
    """Workday career sites expose the same public JSON their own search page uses, newest first.
    slug = "host|tenant|site". Reads up to 3 pages while postings are still from the last 2 days."""
    host, tenant, site = company["slug"].split("|")
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    out, now = [], datetime.now(timezone.utc)
    for page in range(3):
        r = requests.post(f"{base}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": ""},
                          headers=BROWSER, timeout=TIMEOUT)
        r.raise_for_status()
        posts = r.json().get("jobPostings") or []
        stale = False
        for j in posts:
            m = WD_AGE.search(j.get("postedOn") or "")
            days = 0 if not m or m.group(1).lower() == "today" else 1 if m.group(1).lower() == "yesterday" else int(m.group(2))
            if not m:
                days = 30          # "Posted 30+ Days Ago" and unknown formats
            stale = stale or days > 1
            path = j.get("externalPath") or ""
            out.append({
                "id": f"workday:{tenant}:{path.rsplit('_', 1)[-1] or path}",
                "company": company["name"], "title": (j.get("title") or "").strip(),
                "location": j.get("locationsText") or "",
                "url": f"https://{host}/{site}{path}", "apply_url": f"https://{host}/{site}{path}", "ats": "workday",
                "posted_at": _iso(now - timedelta(days=days)), "description": "",
                "enrich": _wd_detail(base, path),
            })
        if stale or len(posts) < 20:
            break
    return out


def _wd_detail(base: str, path: str):
    def enrich(job: dict) -> dict:
        info = requests.get(f"{base}{path}", headers=BROWSER, timeout=TIMEOUT).json().get("jobPostingInfo") or {}
        loc = info.get("location") or job.get("location")
        if info.get("additionalLocations"):
            loc = " • ".join([loc] + info["additionalLocations"])
        return {"description": html_to_text(info.get("jobDescription", "")), "location": loc}
    return enrich


def smartrecruiters(company: dict) -> list[dict]:
    slug = company["slug"]
    r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&country=us", timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json().get("content") or []:
        loc = j.get("location") or {}
        where = ", ".join(x for x in (loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()) if x)
        if loc.get("remote"):
            where += " • Remote"
        posted = j.get("releasedDate")
        out.append({
            "id": f"smartrecruiters:{slug}:{j['id']}", "company": company["name"], "title": (j.get("name") or "").strip(),
            "location": where, "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
            "apply_url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}", "ats": "smartrecruiters",
            "posted_at": _iso(datetime.fromisoformat(posted.replace("Z", "+00:00"))) if posted else None,
            "description": "", "enrich": _sr_detail(slug, j["id"]),
        })
    return out


def _sr_detail(slug: str, jid: str):
    def enrich(job: dict) -> dict:
        d = requests.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{jid}", timeout=TIMEOUT, headers=HEADERS).json()
        secs = (d.get("jobAd") or {}).get("sections") or {}
        return {"description": "\n\n".join(html_to_text((secs.get(k) or {}).get("text", ""))
                                            for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"))}
    return enrich


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workday": workday, "smartrecruiters": smartrecruiters}


def fetch(company: dict) -> list[dict]:
    return FETCHERS[company["ats"]](company)
