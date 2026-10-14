"""Aggregator feeds: Jobright's public GitHub lists, SimplifyJobs new-grad listings, and Hacker News
'Who is hiring?' (startups). Each returns normalized job dicts like the ATS fetchers, plus:
    source, h1b_level ('strong' | 'none' | 'unknown'), h1b_signal (human text), startup, company_stage,
    enrich (callable that fetches the full description and sponsorship details, or None)
Enrichment is done later and only for jobs that pass the cheap title/date/location prefilter.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

from . import HEADERS, TIMEOUT, html_to_text

BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) job-autopilot/1.0"}
VISA_YES = re.compile(r"\bvisa\b|h-?1b|sponsor(ship)?\b|\bopt\b|stem opt", re.I)
VISA_NO = re.compile(r"no visa|not (able to )?sponsor|can(no|')t sponsor|unable to sponsor|no sponsorship|without sponsorship|"
                     r"us citizens? only|u\.s\. citizens? only|citizenship required|no h-?1b", re.I)
STARTUP_STAGES = re.compile(r"seed|series [a-d]\b|pre-seed|angel|early stage", re.I)


def _uid(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:14]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------------- Jobright (GitHub lists)

ROW = re.compile(r"^\|\s*(?P<company>.*?)\s*\|\s*(?P<title>.*?)\s*\|\s*(?P<loc>.*?)\s*\|\s*(?P<model>.*?)\s*\|\s*(?P<date>.*?)\s*\|\s*$", re.M)
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def jobright(cfg: dict) -> list[dict]:
    out, now = [], datetime.now(timezone.utc)
    for repo in cfg.get("repos", []):
        text = ""
        for branch in ("master", "main"):
            r = requests.get(f"https://raw.githubusercontent.com/{repo}/{branch}/README.md", headers=HEADERS, timeout=TIMEOUT)
            if r.ok:
                text = r.text
                break
        last_company = ""
        for m in ROW.finditer(text):
            company_cell, title_cell = m.group("company"), m.group("title")
            if company_cell.startswith("---") or company_cell.lower() == "company":
                continue
            cm = LINK.search(company_cell)
            company = (cm.group(1) if cm else company_cell).strip("* ")
            if company in ("↳", ""):
                company = last_company
            last_company = company
            tm = LINK.search(title_cell)
            if not tm:
                continue
            title, url = tm.group(1).strip("* "), tm.group(2).split("?")[0]
            posted = _parse_month_day(m.group("date"), now)
            out.append({
                "id": f"jobright:{url.rstrip('/').split('/')[-1]}",
                "company": html.unescape(company), "title": html.unescape(title), "location": m.group("loc"),
                "url": url, "apply_url": url, "ats": "jobright", "source": "jobright",
                "posted_at": _iso(posted) if posted else None, "description": "",
                "h1b_level": "unknown", "h1b_signal": "", "startup": 0, "company_stage": "",
                "enrich": _enrich_jobright,
            })
    return out


def _parse_month_day(s: str, now: datetime) -> datetime | None:
    try:
        d = datetime.strptime(f"{s.strip()} {now.year}", "%b %d %Y").replace(tzinfo=timezone.utc, hour=12)
        return d.replace(year=now.year - 1) if d > now + timedelta(days=2) else d
    except ValueError:
        return None


def _enrich_jobright(job: dict) -> dict:
    """Public job page: Jobright's own sponsor / citizenship / clearance flags plus company H-1B filings."""
    r = requests.get(job["url"], headers=BROWSER_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        return {}
    ds = json.loads(m.group(1))["props"]["pageProps"].get("dataSource") or {}
    j, c = ds.get("jobResult") or {}, ds.get("companyResult") or {}
    q = j.get("qualifications") or {}
    parts = [j.get("jobSummary") or "",
             "Responsibilities:\n- " + "\n- ".join(j.get("coreResponsibilities") or []),
             "Must have:\n- " + "\n- ".join(q.get("mustHave") or []),
             "Preferred:\n- " + "\n- ".join(q.get("preferredHave") or []),
             f"Experience: {j.get('minYearsOfExperience')}–{j.get('maxYearsOfExperience')} years; seniority {j.get('jobSeniority')}; "
             f"work model {j.get('workModel')}; salary {j.get('salaryDesc') or 'n/a'}"]
    filings = c.get("h1bAnnualJobCount") or []
    signals, level = [], "unknown"
    if j.get("isCitizenOnly") or j.get("isClearanceRequired"):
        level = "none"
        signals.append("Jobright: US citizenship or clearance required")
    else:
        if j.get("isH1bSponsor"):
            level = "strong"
            signals.append("Jobright: H-1B sponsor")
        if filings:
            level = "strong"
            total = sum(int(x.get("count") or x.get("jobCount") or 0) for x in filings if isinstance(x, dict)) if isinstance(filings, list) else 0
            signals.append(f"Jobright: company has H-1B filings{f' ({total:,} on record)' if total else ''}")
    stage = c.get("fundraisingCurrentStage") or ""
    size = str(c.get("companySize") or "")
    small = bool(re.match(r"^(1|2|11|51)\D", size))
    return {
        "description": "\n\n".join(p for p in parts if p.strip()),
        "location": j.get("jobLocation") or job.get("location"),
        "posted_at": _iso(datetime.strptime(j["publishTime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)) if j.get("publishTime") else job.get("posted_at"),
        "h1b_level": level, "h1b_signal": "; ".join(signals),
        "startup": int(bool(STARTUP_STAGES.search(stage)) or small),
        "company_stage": ", ".join(x for x in (stage, size if "employee" in size.lower() else (f"{size} employees" if size else "")) if x),
    }


# ----------------------------------------------------------------------------- SimplifyJobs new-grad listings

def simplify(cfg: dict) -> list[dict]:
    r = requests.get(cfg["url"], headers=HEADERS, timeout=60)
    r.raise_for_status()
    out = []
    for x in r.json():
        if not x.get("active") or not x.get("is_visible", True):
            continue
        sp = x.get("sponsorship") or "Other"
        level = "none" if sp in ("Does Not Offer Sponsorship", "U.S. Citizenship is Required") else "strong" if sp == "Offers Sponsorship" else "unknown"
        out.append({
            "id": f"simplify:{x['id']}", "company": x.get("company_name", ""), "title": x.get("title", ""),
            "location": " • ".join(x.get("locations") or []), "url": x.get("url"), "apply_url": x.get("url"),
            "ats": "simplify", "source": "simplify",
            "posted_at": _iso(datetime.fromtimestamp(x["date_posted"], timezone.utc)) if x.get("date_posted") else None,
            "description": "", "h1b_level": level, "h1b_signal": f"Simplify: {sp}" if sp != "Other" else "",
            "startup": 0, "company_stage": "", "enrich": _enrich_url,
        })
    return out


# ----------------------------------------------------------------------------- Hacker News "Who is hiring?"

def hn_who_is_hiring(cfg: dict) -> list[dict]:
    hits = requests.get("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&query=who%20is%20hiring&hitsPerPage=5",
                        headers=HEADERS, timeout=TIMEOUT).json()["hits"]
    story = next((h for h in hits if h["title"].lower().startswith("ask hn: who is hiring")), None)
    if not story:
        return []
    item = requests.get(f"https://hn.algolia.com/api/v1/items/{story['objectID']}", headers=HEADERS, timeout=60).json()
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.get("max_age_days", 14))
    out = []
    for c in item.get("children") or []:
        raw = c.get("text") or ""
        created = datetime.fromtimestamp(c.get("created_at_i") or 0, timezone.utc)
        if not raw or created < cutoff:
            continue
        text = html_to_text(raw)
        first = text.split("\n", 1)[0]
        if not VISA_YES.search(text) or VISA_NO.search(text):
            continue
        segs = [s.strip() for s in first.split("|")]
        urls = re.findall(r'href="([^"]+)"', html.unescape(raw))
        out.append({
            "id": f"hn:{c['id']}", "company": segs[0][:80] if segs else "HN startup",
            "title": " | ".join(segs[1:3])[:160] if len(segs) > 1 else first[:160],
            "location": " | ".join(segs[2:5])[:160], "url": f"https://news.ycombinator.com/item?id={c['id']}",
            "apply_url": urls[0] if urls else f"https://news.ycombinator.com/item?id={c['id']}",
            "ats": "hn", "source": "hn", "posted_at": _iso(created), "description": text[:20000],
            "h1b_level": "strong", "h1b_signal": "HN post mentions visa / H-1B sponsorship",
            "startup": 1, "company_stage": "Startup (HN Who is hiring)", "enrich": None,
        })
    return out


# ----------------------------------------------------------------------------- description fetchers by URL

_ashby_cache: dict[str, dict] = {}


def _enrich_url(job: dict) -> dict:
    """Full description from the posting's own site: ATS APIs where possible, otherwise the page text."""
    url = job.get("url") or ""
    u = urlparse(url)
    host, parts = u.netloc.lower(), [p for p in u.path.split("/") if p]
    try:
        if "greenhouse.io" in host and "jobs" in parts:
            slug, jid = parts[0], parts[parts.index("jobs") + 1]
            d = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}", headers=HEADERS, timeout=TIMEOUT).json()
            return {"description": html_to_text(d.get("content", "")), "board": ("greenhouse", slug)}
        if host == "jobs.lever.co" and len(parts) >= 2:
            d = requests.get(f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}", headers=HEADERS, timeout=TIMEOUT).json()
            desc = d.get("descriptionPlain", "") + "\n\n" + "\n\n".join(
                f"{x.get('text', '')}\n{html_to_text(x.get('content', ''))}" for x in d.get("lists", []))
            return {"description": desc + "\n\n" + d.get("additionalPlain", ""), "board": ("lever", parts[0])}
        if host == "jobs.ashbyhq.com" and len(parts) >= 2:
            slug = parts[0]
            if slug not in _ashby_cache:
                _ashby_cache[slug] = {j["id"]: j for j in requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", headers=HEADERS, timeout=TIMEOUT).json().get("jobs", [])}
            j = _ashby_cache[slug].get(parts[1]) or {}
            return {"description": j.get("descriptionPlain", ""), "board": ("ashby", slug)}
        if "myworkdayjobs.com" in host and "job" in parts:
            tenant = host.split(".")[0]
            site = parts[1] if parts[0].lower() in ("en-us", "en") else parts[0]
            rest = "/".join(parts[parts.index("job"):])
            d = requests.get(f"https://{host}/wday/cxs/{tenant}/{site}/{rest}", headers={**BROWSER_HEADERS, "Accept": "application/json"}, timeout=TIMEOUT).json()
            info = d.get("jobPostingInfo") or {}
            return {"description": html_to_text(info.get("jobDescription", "")), "location": info.get("location") or job.get("location")}
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=TIMEOUT)
        page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", r.text)
        return {"description": html_to_text(page)[:20000]}
    except Exception:
        return {}


def polite_pause() -> None:
    time.sleep(0.4)   # be gentle with the sites we read (and with Jobright's pages)


FEEDS = {"jobright": jobright, "simplify": simplify, "hn_who_is_hiring": hn_who_is_hiring}
