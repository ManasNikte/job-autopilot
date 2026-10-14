"""People you know (referrals): import from your networking spreadsheet, match to jobs, draft referral texts.

Data lives in the local SQLite database (data/, gitignored). Nothing here is sent anywhere except the
contact's first name, company, how you know them, and the job title/link, which go to Claude to draft the
text message you send yourself.
"""
from __future__ import annotations

import io
import json
import re
from datetime import date, datetime

from . import db, llm
from .config import PROFILE_DIR, env
from .h1b import norm as company_norm
from .schemas import S, obj

STAGES = {
    "will_refer": "Will refer",
    "warm": "Warm · follow up",
    "unknown": "Not asked yet",
    "no_reply": "No reply",
    "student": "Student · can't refer yet",
    "avoid": "Avoid",
}
REFERABLE = ("will_refer", "warm", "unknown")

# Fix company names typed differently in your sheet so people match job postings (original kept in notes).
# Add your own in profile/company_aliases.json: {"fixes": {"redhat": "Red Hat"}, "not_companies": ["student"]}.
COMPANY_FIXES = {"redhat": "Red Hat"}
NOT_COMPANIES = {"student", "n/a", "na", "-", ""}
try:
    _aliases = json.loads((PROFILE_DIR / "company_aliases.json").read_text(encoding="utf-8"))
    COMPANY_FIXES |= {k.lower(): v for k, v in _aliases.get("fixes", {}).items()}
    NOT_COMPANIES |= {v.lower() for v in _aliases.get("not_companies", [])}
except (OSError, ValueError):
    pass


def init() -> None:
    db.init()   # tables live in db.EXTRA_SCHEMA


# ----------------------------------------------------------------------------- cleaning

def clean_phone(raw) -> str | None:
    if raw in (None, ""):
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"+1 ({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return f"+{digits}" if digits else None


def clean_company(raw) -> tuple[str | None, str | None]:
    """(company, note) — fixes known typos; drops values that aren't companies (keeps them as a note)."""
    if raw in (None, ""):
        return None, None
    s = re.sub(r"\s+", " ", str(raw)).strip()
    if s.lower() in NOT_COMPANIES:
        return None, (None if s.lower() in ("n/a", "na", "-", "") else f"Listed as: {s}")
    if s.lower().startswith("http"):
        return None, f"Company link: {s}"
    fixed = COMPANY_FIXES.get(s.lower())
    return (fixed, f"Company was spelled '{s}' in the sheet") if fixed else (s, None)


def stage_from_notes(notes: str, company_raw: str) -> str:
    n = (notes or "").lower()
    if "rude" in n or "spammed" in n or "won't help" in n:
        return "avoid"
    if re.search(r"ready to ref+er|if you find any roles|can refer", n):
        return "will_refer"
    if "student" in n or (company_raw or "").strip().lower() == "student":
        return "student"
    if "no reply" in n:
        return "no_reply"
    if any(k in n for k in ("need to talk", "connect", "busy", "ask more", "generic response", "connected me")):
        return "warm"
    return "unknown"


def _clean_url(v) -> str | None:
    s = str(v or "").strip()
    return s if s.lower().startswith("http") else None


def import_xlsx(raw: bytes, replace_source: bool = True) -> dict:
    """Import the networking sheet (Name, Last Contacted, Number, LinkedIn, Connection, LinkedIn, Company, Post,
    Last Convo Status, then any number of job-link columns). Re-importing updates people by name."""
    import openpyxl
    ws = openpyxl.load_workbook(io.BytesIO(raw), data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    header = [str(h or "").strip().lower() for h in rows[0]]

    def col(*names):
        for i, h in enumerate(header):
            if h in names:
                return i
        return None

    i_name, i_last, i_phone = col("name"), col("last contacted"), col("number", "phone")
    i_conn, i_company, i_post = col("connection"), col("company"), col("post", "title", "role")
    i_status = col("last convo status", "status", "notes")
    li_cols = [i for i, h in enumerate(header) if h == "linkedin"]
    added = updated = 0
    changes: list[str] = []
    for r in rows[1:]:
        if not r or not any(v not in (None, "") for v in r):
            continue
        g = lambda i: r[i] if i is not None and i < len(r) else None
        name = re.sub(r"\s+", " ", str(g(i_name) or "")).strip()
        if not name:
            continue
        company, cnote = clean_company(g(i_company))
        notes_raw = str(g(i_status) or "").strip()
        notes = [] if notes_raw.upper() in ("", "N/A") else [notes_raw]
        if cnote:
            notes.append(cnote)
            changes.append(f"{name}: {cnote}")
        linkedin = next((u for u in (_clean_url(g(i)) for i in li_cols) if u and "linkedin.com/in/" in u
                         and u.rstrip("/") != "https://www.linkedin.com/in"), None)
        links, extra = [], []
        for v in r[(i_status or 8) + 1:]:
            u = _clean_url(v)
            if u and "linkedin.com/in/" in u:
                extra.append(f"Related LinkedIn: {u}")
            elif u:
                links.append(u)
            elif v not in (None, ""):
                extra.append(str(v))
        notes += extra
        last = g(i_last)
        last_iso = last.date().isoformat() if isinstance(last, datetime) else (last.isoformat() if isinstance(last, date) else None)
        rec = {
            "name": name, "company": company, "company_key": company_norm(company) if company else None,
            "title": (str(g(i_post)).strip() or None) if g(i_post) else None,
            "connection": (str(g(i_conn)).strip() or None) if g(i_conn) else None,
            "phone": clean_phone(g(i_phone)), "linkedin": linkedin,
            "stage": stage_from_notes(notes_raw, str(g(i_company) or "")), "notes": "\n".join(notes) or None,
            "job_links": json.dumps(links), "last_contacted": last_iso, "source": "spreadsheet",
        }
        existing = db.fetchone("SELECT id FROM contacts WHERE lower(name)=lower(?)", (name,))
        if existing:
            sets = ",".join(f"{k}=?" for k in rec)
            db.execute(f"UPDATE contacts SET {sets}, updated_at=? WHERE id=?", (*rec.values(), db.now(), existing["id"]))
            updated += 1
        else:
            cols = ",".join(rec) + ",created_at,updated_at"
            db.execute(f"INSERT INTO contacts ({cols}) VALUES ({','.join('?' * (len(rec) + 2))})", (*rec.values(), db.now(), db.now()))
            added += 1
    return {"added": added, "updated": updated, "changes": changes}


# ----------------------------------------------------------------------------- matching jobs <-> people

def contacts_for_company(company: str, referable_only: bool = True) -> list[dict]:
    key = company_norm(company or "")
    if not key:
        return []
    rows = db.fetchall("SELECT * FROM contacts WHERE company_key IS NOT NULL AND company_key != ''")
    out = [c for c in rows if c["company_key"] == key or key.startswith(c["company_key"] + " ")
           or c["company_key"].startswith(key + " ")]
    if referable_only:
        out = [c for c in out if c["stage"] in REFERABLE]
    order = {s: i for i, s in enumerate(STAGES)}
    return sorted(out, key=lambda c: order.get(c["stage"], 9))


REFERRAL_SCHEMA = obj(message=S)


def best_link(job: dict, contact: dict) -> str:
    """Prefer an official posting link the contact shared for this same role (referral portals need the req ID)."""
    title_slug = re.sub(r"[^a-z0-9]+", "-", (job.get("title") or "").lower()).strip("-")
    words = [w for w in title_slug.split("-") if len(w) > 1]
    for u in json.loads(contact.get("job_links") or "[]"):
        path = re.sub(r"[^a-z0-9]+", "-", u.lower())
        if title_slug and (title_slug in path or (words and all(w in path.split("-") for w in words))):
            return u
    return job.get("url") or job.get("apply_url") or ""


def draft_message(job: dict, contact: dict) -> str:
    """A short, personal text asking this person for a referral to this exact role (you send it yourself)."""
    first = contact["name"].split("(")[0].split()[0] if contact["name"] else "there"
    link = best_link(job, contact)
    screen = job.get("screen_json") or {}
    fit = (screen.get("why") or [""])[0] if isinstance(screen, dict) else ""
    user = f"""CALL TYPE: REFERRAL TEXT
Write one SMS / iMessage the candidate will send to a contact asking for a referral to a specific role.
Return JSON {{"message": "..."}} only.

CONTACT: first name "{first}"; works at {contact.get('company')} as {contact.get('title') or 'unknown title'};
how the candidate knows them: {contact.get('connection') or 'unknown'}; last conversation notes: {contact.get('notes') or 'none'};
referral stage: {STAGES.get(contact.get('stage'), 'unknown')}.
ROLE: {job['title']} at {job['company']} ({job.get('location') or ''})
LINK: {link}
STRONGEST FIT (from the screen; use at most one concrete proof point, from Section I / I-B only): {fit}

RULES: 300–480 characters. Warm, plain, first person, US English, like a real text (no subject line, no
sign-off block). Greet by first name, one line of context that fits how they know each other (if the notes
say they already offered to refer, thank them and say you found a matching role), the exact role title and
the link, one honest proof point, a clear low-pressure ask ("would you be open to referring me?"), and offer
to send the resume. Never invent facts about the contact or the candidate. No emojis."""
    try:
        res = llm.structured(user=user, schema=REFERRAL_SCHEMA, model=env("MODEL_SCREEN", "claude-opus-5-5"),
                             effort="low", max_tokens=4000, label="referral")
        msg = (res.get("message") or "").strip()
        if msg:
            return msg
    except llm.BackendUnavailable:
        raise
    except Exception:
        pass
    return (f"Hi {first}, hope you're doing well! I found a role at {job['company']} that fits my background: "
            f"{job['title']} ({link}). Would you be open to referring me? Happy to send my resume "
            f"and a short note on why I'm a fit. Thank you!")


def queue_referral_asks(job_id: str, log=print) -> int:
    """Called when a job's documents are ready: draft a referral text for each person you know there."""
    job = db.get_job(job_id)
    if not job:
        return 0
    n = 0
    for c in contacts_for_company(job["company"])[:3]:
        if db.fetchone("SELECT 1 FROM referral_asks WHERE job_id=? AND contact_id=?", (job_id, c["id"])):
            continue
        msg = draft_message(job, c)
        db.execute("INSERT OR IGNORE INTO referral_asks (job_id, contact_id, message, created_at, updated_at) VALUES (?,?,?,?,?)",
                   (job_id, c["id"], msg, db.now(), db.now()))
        db.add_intervention(job_id, "referral", f"Ask {c['name']} ({c['company']}) for a referral before you submit",
                            f"{STAGES.get(c['stage'])} · {c.get('connection') or ''}\n\n{msg}")
        log(f"  ✉ referral text drafted for {c['name']} at {c['company']}")
        n += 1
    return n


def asks_for_job(job_id: str) -> list[dict]:
    return db.fetchall("SELECT a.*, c.name, c.company, c.title, c.phone, c.email, c.linkedin, c.stage, c.connection "
                       "FROM referral_asks a JOIN contacts c ON c.id=a.contact_id WHERE a.job_id=? ORDER BY a.id", (job_id,))
