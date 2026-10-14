"""Cheap rule-based gate that runs before any Claude call.

Returns (passed, reason, role_family, notes). Notes are hints passed on to Claude
(e.g. "JD mentions H-1B sponsorship", "asks for 2-3 years").
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
    "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida",
    "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska",
    "nevada", "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas",
    "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming",
}
US_CITIES = {
    "san francisco", "new york", "nyc", "seattle", "boston", "austin", "chicago", "los angeles", "denver",
    "atlanta", "palo alto", "mountain view", "menlo park", "sunnyvale", "san jose", "san mateo", "redwood city",
    "bellevue", "cambridge", "pittsburgh", "philadelphia", "washington dc", "miami", "dallas", "houston",
    "san diego", "portland", "salt lake city", "raleigh", "minneapolis", "detroit", "phoenix", "nashville",
    "bay area", "silicon valley", "brooklyn", "jersey city", "irvine", "santa clara", "foster city", "south san francisco",
}


# "unable to offer visa sponsorship", "not offering visa sponsorship", "will not provide H-1B sponsorship",
# "does not participate in the H-1B lottery": wording the fixed phrase list kept missing.
VISA_NEGATION = re.compile(
    r"\b(not|unable\s+to|cannot|can't|can\s+not|won't|will\s+not|do\s+not|does\s+not|no\s+longer)\s+(be\s+)?"
    r"(offer|offering|provide|providing|support|supporting|sponsor|sponsoring|consider)\w*\s+(any\s+|future\s+|employment\s+|work\s+)?"
    r"(visa|h-?1b|immigration)?\s*(visa\s+)?sponsorship|"
    r"\bnot\s+participate\s+in\s+the\s+h-?1b|\bauthorized\s+to\s+work\s+in\s+the\s+united\s+states\s+on\s+a\s+permanent\s+basis", re.I)


def _has_word(text: str, term: str) -> bool:
    """Case-insensitive whole-word/phrase match ('intern' does not hit 'internal', 'itar' not 'military')."""
    return re.search(rf"(?<![a-z0-9]){re.escape(term.strip().lower())}(?![a-z0-9])", text.lower()) is not None


def is_us_location(location: str, cfg: dict) -> bool | None:
    """True = US, False = clearly not US, None = can't tell (let Claude decide)."""
    if not location:
        return None
    loc = location.lower()
    parts = re.split(r"[•;|/]| or ", location)
    strong_us = any(_has_word(loc, t) for t in cfg["us_location_terms"])
    strong_us |= any(_has_word(loc, c) for c in US_CITIES | US_STATE_NAMES)
    strong_us |= re.search(r"(?<![a-z])usa?\s*-\s*[a-z]{2}\b", loc) is not None   # Workday style "US-TX-McKinney"
    if strong_us:
        return True  # multi-location postings with any US office count
    if any(_has_word(loc, t) for t in cfg["non_us_terms"]):
        return False
    # State abbreviations last: "Bengaluru, IN" must not read as Indiana.
    if any(re.search(rf",\s*{s}\b", p) for p in parts for s in US_STATES):
        return True
    return None


def min_years_required(text: str) -> int | None:
    """Smallest 'N+ years' / 'N-M years' figure that appears near 'experience'."""
    found = []
    for m in re.finditer(r"(\d{1,2})\s*(?:\+|-|–|to)?\s*(\d{1,2})?\s*\+?\s*years?", text, re.I):
        window = text[m.end(): m.end() + 80].lower()
        if "experience" in window or "industry" in window or "professional" in window:
            found.append(int(m.group(1)))
    found = [n for n in found if n <= 15]
    return min(found) if found else None


def role_family(title: str, cfg: dict) -> str | None:
    t = title.lower()
    for fam, kws in cfg["role_families"].items():
        if any(k in t for k in kws):
            return fam
    return None


def check(job: dict, cfg: dict, posted_within_hours: int, now: datetime | None = None,
          skip_title: bool = False) -> tuple[bool, str, str | None, list[str]]:
    now = now or datetime.now(timezone.utc)
    notes: list[str] = []

    fam = role_family(job["title"], cfg) or (role_family(job.get("description", "")[:800], cfg) if skip_title else None)
    if not fam:
        return False, "title not in target role families", None, notes

    if posted_within_hours and job.get("posted_at"):
        posted = datetime.fromisoformat(job["posted_at"])
        if now - posted > timedelta(hours=posted_within_hours):
            return False, f"posted {posted.date()} (older than {posted_within_hours}h)", fam, notes

    for term in ([] if skip_title else cfg["title_exclude"]):
        if _has_word(job["title"], term):
            return False, f"title excluded ('{term}')", fam, notes

    us = is_us_location(job.get("location", ""), cfg) if not skip_title else (is_us_location(job.get("location", ""), cfg) or None)
    if us is False:
        return False, f"non-US location ({job.get('location')})", fam, notes
    if us is None:
        notes.append(f"location unclear: '{job.get('location')}'")

    desc = job.get("description", "")
    for phrase in cfg["visa_blockers"]:
        if _has_word(desc, phrase):
            i = desc.lower().find(phrase.strip().lower())
            snippet = desc[max(0, i - 80): i + 100].replace("\n", " ") if i >= 0 else phrase
            return False, f"visa/clearance blocker: …{snippet}…", fam, notes

    m = VISA_NEGATION.search(desc)
    if m:
        return False, f"visa/clearance blocker: …{desc[max(0, m.start() - 60): m.end() + 60].replace(chr(10), ' ')}…", fam, notes

    for phrase in cfg.get("visa_soft_signals", []):
        if _has_word(desc, phrase):
            notes.append(f"JD mentions '{phrase}' (judge whether it bars F-1/OPT candidates)")
            break

    yrs = min_years_required(desc)
    if yrs is not None:
        if yrs > cfg.get("max_years_required", 3):
            return False, f"requires {yrs}+ years", fam, notes
        notes.append(f"JD asks for about {yrs}+ years")

    if any(_has_word(desc, s) for s in cfg["sponsor_positive_signals"]):
        notes.append("JD mentions visa/OPT/H-1B (check wording)")
    return True, "passed prefilter", fam, notes
