"""Pay ranges: read them from ATS fields or the job description, normalized to annual USD.

Most US postings now list a range (pay-transparency laws). Jobs whose listed pay tops out below your
floor (Settings: MIN_SALARY, default $100K) are filtered before any Claude call. Jobs with no listed pay
are kept: no number is not a low number.
"""
from __future__ import annotations

import re

HOURS_PER_YEAR = 2080
_NUM = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kK])?"
RANGE = re.compile(
    rf"(?:US\$|USD\s*\$?|\$)\s*{_NUM}\s*(?:USD)?(?:\s*/\s*(?:yr|year|hr|hour))?\s*(?:-|–|—|to)\s*(?:US\$|USD\s*\$?|\$)?\s*{_NUM}\s*(?:USD)?"
    r"(?P<unit>\s*(?:/|per|an|a)\s*(?:hour|hr|year|yr|annum|month|mo)\b)?", re.I)
SINGLE = re.compile(rf"(?:up to|max(?:imum)?(?: of)?|salary(?: of)?:?)\s*(?:US\$|USD\s*\$?|\$)\s*{_NUM}"
                    r"(?P<unit>\s*(?:/|per|an|a)\s*(?:hour|hr|year|yr|annum|month|mo)\b)?", re.I)
HOURLY_CONTEXT = re.compile(r"hourly|per hour|/\s*hr\b|/\s*hour", re.I)


def _val(num: str, k: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * 1000 if k else v


def _annual(lo: float, hi: float, unit: str, context: str) -> tuple[int, int] | None:
    unit = (unit or "").lower()
    if "hour" in unit or "hr" in unit or (not unit and hi < 300 and HOURLY_CONTEXT.search(context)):
        lo, hi = lo * HOURS_PER_YEAR, hi * HOURS_PER_YEAR
    elif "mo" in unit:
        lo, hi = lo * 12, hi * 12
    elif hi < 300:          # "$45 - $60" with no unit and no hourly wording: too ambiguous to judge
        return None
    if lo > hi:
        lo, hi = hi, lo
    if hi < 20_000 or hi > 2_000_000:   # funding rounds, bonuses, typos
        return None
    return int(lo), int(hi)


def parse(text: str) -> tuple[int, int] | None:
    """Widest plausible annual range in the text: (lowest low, highest high) across geo tiers."""
    if not text:
        return None
    found = []
    for m in RANGE.finditer(text):
        ctx = text[max(0, m.start() - 120): m.end() + 60]
        if re.search(r"\b(million|billion|funding|raised|revenue|valuation)\b|\$\s*\d+(\.\d+)?\s*[mb]\b", ctx, re.I):
            continue
        r = _annual(_val(m.group(1), m.group(2)), _val(m.group(3), m.group(4)), m.group("unit"), ctx)
        if r:
            found.append(r)
    if not found:
        for m in SINGLE.finditer(text):
            v = _val(m.group(1), m.group(2))
            r = _annual(v, v, m.group("unit"), text[max(0, m.start() - 120): m.end() + 60])
            if r:
                found.append(r)
    if not found:
        return None
    return min(r[0] for r in found), max(r[1] for r in found)


def fmt(rng: tuple[int, int] | None) -> str:
    if not rng:
        return ""
    k = lambda v: f"${v / 1000:.0f}K"
    return k(rng[0]) if rng[0] == rng[1] else f"{k(rng[0])}–{k(rng[1])}"


def annotate(job: dict) -> dict:
    """Set job['salary'] (display text) and job['salary_max'] from a structured 'pay' tuple or the description."""
    rng = job.pop("pay", None) or parse(job.get("description") or "")
    if rng:
        job["salary"], job["salary_max"] = fmt(rng), rng[1]
    return job


def below_floor(job: dict, floor: int) -> str | None:
    """Reason string when the listed pay tops out under the floor, else None."""
    top = job.get("salary_max")
    if floor and top and top < floor:
        return f"pay below ${floor / 1000:.0f}K (listed {job.get('salary')})"
    return None
