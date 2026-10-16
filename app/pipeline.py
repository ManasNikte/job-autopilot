"""fetch -> prefilter -> screen (Claude) -> tailor (Claude) -> render -> check -> revise -> hand off to the human.

Job status lifecycle:
  new -> filtered_out | to_screen
  to_screen -> rejected (DON'T APPLY) | needs_decision (APPLY WITH RISK) | to_tailor (APPLY)
  to_tailor -> ready (documents built, waiting for you to submit) | error
  ready -> applied -> interviewing -> offer | closed
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus

from . import db, llm, prefilter, progress, salary, sources
from .config import OUTPUT_DIR, applicant, companies, env, env_int, feeds_config, search_config
from .h1b import lookup as h1b_lookup
from .render import pdf as pdfx
from .render import pdfgen
from .schemas import REVISE_PATCH, SCREEN, TAILOR

Log = Callable[[str], None]


def _print(msg: str) -> None:
    print(msg, flush=True)


# ----------------------------------------------------------------------------- fetch + prefilter

def fetch_all(log: Log = _print) -> dict:
    """Company boards in two passes: a cheap title/date/location check on every posting, then full descriptions
    only for the survivors (directory boards list titles first), then pay floor and H-1B signal."""
    from .h1b import WebChecker, seed_from_jobs
    from .sources import directory
    from .sources.feeds import SPONSOR_OFFERED

    cfg = search_config()
    within = env_int("POSTED_WITHIN_HOURS", 24)
    floor = env_int("MIN_SALARY", 100000)
    fcfg = feeds_config()
    dcfg = fcfg.get("board_directory") or {}
    require = fcfg.get("require_h1b_signal", True) and env("SCREEN_UNKNOWN_SPONSOR", "0") != "1"
    seed_from_jobs()
    if dcfg.get("enabled"):
        progress.set_run("fetch", "Refreshing the board directory from public new-grad lists")
        try:
            directory.refresh(dcfg, log)
        except Exception as e:
            log(f"  ✗ board directory refresh: {e}")
    comps = [c for c in companies() if dcfg.get("enabled") or not c.get("directory")]
    stats = {"companies": 0, "fetched": 0, "new": 0, "passed": 0, "errors": 0, "low_pay": 0, "details": 0}
    progress.set_run("fetch", f"Checking {len(comps):,} company job boards", 0, len(comps))
    norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())
    recent, batch, pending, deferred = db.recent_job_keys(), set(), [], []
    budget = env_int("SPONSOR_WEB_CHECKS", 25)
    checker = WebChecker(budget, log) if budget > 0 else None

    def one(c):
        try:
            return c, sources.fetch(c), None
        except Exception as e:  # network/HTTP/JSON errors are per-company, never fatal
            return c, [], f"{type(e).__name__}: {e}"[:300]

    def finish(c: dict, j: dict, ok: bool, reason: str, notes: list[str], looked: bool = False) -> None:
        salary.annotate(j)
        low = salary.below_floor(j, floor)
        if ok and low:
            ok, reason = False, low
            stats["low_pay"] += 1
        if c.get("directory"):
            sig = h1b_lookup(j["company"])
            desc = j.get("description") or ""
            if not sig and ok and SPONSOR_OFFERED.search(desc):
                sig = "JD says sponsorship is available"
            if ok and not sig and checker:
                if not looked:                       # look the company up online once, after the sweep
                    deferred.append((c, j, ok, reason, notes))
                    return
                sig = h1b_lookup(j["company"])
                refused = checker.refused(j["company"])
                if refused:
                    ok, reason = False, refused
            j.update(source="directory", h1b_level="strong" if sig else "unknown", h1b_signal=sig or "", h1b_note="")
            if ok and not sig and require:
                ok, reason = False, "no H-1B sponsorship signal (screen it anyway if you know they sponsor)"
            elif ok and not sig:
                notes.append("H-1B sponsorship unknown for this company: check the visa gate")
        else:
            j.update(h1b_note=c.get("h1b", ""), source="discovered" if c.get("discovered") else j["ats"],
                     h1b_level="strong", h1b_signal=c.get("h1b") or "In your sponsor list")
        if not db.upsert_job({k: v for k, v in j.items() if k in JOB_COLUMNS}):
            return
        stats["new"] += 1
        if ok:
            stats["passed"] += 1
            db.update_job(j["id"], status="to_screen", status_reason="; ".join(notes) or reason)
        else:
            db.update_job(j["id"], status="filtered_out", status_reason=reason)

    with cf.ThreadPoolExecutor(24) as ex:
        for c, jobs, err in ex.map(one, comps):
            stats["companies"] += 1
            progress.bump_run()
            key = f"{c['ats']}:{c['slug']}"
            db.execute(
                "INSERT OR REPLACE INTO source_status (key, company, ats, slug, ok, jobs_seen, error, checked_at) VALUES (?,?,?,?,?,?,?,?)",
                (key, c["name"], c["ats"], c["slug"], 0 if err else 1, len(jobs), err, db.now()),
            )
            if err:
                stats["errors"] += 1
                if not c.get("directory"):       # thousands of directory boards: a few dead ones are normal
                    log(f"  ✗ {c['name']} ({key}): {err}")
                continue
            stats["fetched"] += len(jobs)
            for j in jobs:
                ok, reason, fam, notes = prefilter.check(j, cfg, within)
                if not ok and reason.startswith(("title not in", "posted ")):
                    continue  # don't store the thousands of irrelevant/old postings
                if c.get("directory"):
                    k = norm(j["company"]) + "|" + norm(j["title"])
                    if not ok or k in recent or k in batch:
                        continue  # directory: keep only clean candidates, once (same role is often on several boards)
                    batch.add(k)
                j["role_family"] = fam
                if ok and j.get("enrich") and not j.get("description"):
                    if not db.fetchone("SELECT 1 FROM jobs WHERE id=?", (j["id"],)):
                        pending.append((c, j))
                    continue
                finish(c, j, ok, reason, notes)

    if pending:
        progress.set_run("fetch", f"Reading {len(pending):,} new job descriptions", 0, len(pending))

        def detail(cj):
            c, j = cj
            try:
                j.update({k: v for k, v in (j["enrich"](j) or {}).items() if v not in (None, "")})
            except Exception:
                pass
            progress.bump_run()
            return cj

        with cf.ThreadPoolExecutor(10) as ex:
            for c, j in ex.map(detail, pending):
                stats["details"] += 1
                ok, reason, fam, notes = prefilter.check(j, cfg, 0)
                finish(c, j, ok, reason, notes)
    if deferred:
        _web_lookups(checker, sorted({j["company"] for _, j, *_ in deferred}), log)
        for args in deferred:
            finish(*args, looked=True)
    stats["sponsor_lookups"] = budget - checker.budget if checker else 0
    log(f"Fetched {stats['fetched']:,} postings from {stats['companies']:,} company boards; "
        f"{stats['new']} new relevant, {stats['passed']} passed the prefilter, {stats['low_pay']} under your pay floor, "
        f"{stats['errors']} source errors.")
    stats["feeds"] = fetch_feeds(log, cfg, within, checker)
    return stats


def _web_lookups(checker, names: list[str], log: Log) -> None:
    """Online H-1B lookups for companies missing from the USCIS data, a few at a time."""
    names = [n for n in names if n]
    if not checker or not names:
        return
    progress.set_run("fetch", f"Checking H-1B sponsorship online for {len(names)} companies not in your USCIS data", 0, len(names))

    def one(name):
        try:
            checker.check(name)
        except llm.BackendUnavailable as e:
            checker.budget = 0                 # usage limit: stop looking things up this run
            log(f"  sponsor lookups paused: {e}")
        progress.bump_run()

    with cf.ThreadPoolExecutor(max(1, env_int("PARALLEL_JOBS", 3))) as ex:
        list(ex.map(one, names))


JOB_COLUMNS = {"id", "company", "title", "location", "url", "apply_url", "ats", "posted_at", "description", "role_family",
               "h1b_note", "source", "h1b_signal", "h1b_level", "startup", "company_stage", "salary", "salary_max"}


def fetch_feeds(log: Log, cfg: dict, within: int, checker=None) -> dict:
    """Jobright lists, SimplifyJobs, HN Who is hiring: cheap prefilter first, then fetch details for the survivors."""
    from .h1b import lookup as h1b_lookup
    from .h1b import remember as remember_sponsor
    from .sources import feeds

    fcfg = feeds_config()
    require = fcfg.get("require_h1b_signal", True) and env("SCREEN_UNKNOWN_SPONSOR", "0") != "1"
    listed = {re.sub(r"[^a-z0-9]", "", c["name"].lower()) for c in companies()}
    totals = {"new": 0, "passed": 0}
    enabled = [n for n in feeds.FEEDS if (fcfg.get(n) or {}).get("enabled")]
    for idx, (name, fn) in enumerate(feeds.FEEDS.items()):
        conf = fcfg.get(name) or {}
        if not conf.get("enabled"):
            continue
        progress.set_run("feeds", f"Reading job feeds: {name.replace('_', ' ')}", enabled.index(name), len(enabled))
        try:
            rows = fn(conf)
        except Exception as e:
            db.execute("INSERT OR REPLACE INTO feed_status VALUES (?,?,?,?,?,?)", (name, 0, 0, 0, f"{type(e).__name__}: {e}"[:300], db.now()))
            log(f"  ✗ feed {name}: {e}")
            continue
        hours = within if name != "hn_who_is_hiring" else conf.get("max_age_days", 14) * 24
        candidates, batch_seen = [], set()
        known_ids = db.existing_ids(rows[0]["id"].split(":")[0] + ":") if rows else set()
        recent = db.recent_job_keys()
        norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())
        for j in rows:
            if j["id"] in known_ids:
                continue
            ok, reason, fam, notes = prefilter.check({**j, "description": j.get("description", "") if name == "hn_who_is_hiring" else ""},
                                                     cfg, hours, skip_title=(name == "hn_who_is_hiring"))
            if not ok and not reason.startswith(("visa", "requires")):
                continue                       # wrong role, too old, or not in the US: don't store
            key = norm(j["company"]) + "|" + norm(j["title"])
            if key in batch_seen or key in recent:
                continue                       # same posting in several cities, or already seen on another board
            batch_seen.add(key)
            j["role_family"] = fam
            candidates.append(j)
        candidates.sort(key=lambda x: x.get("posted_at") or "", reverse=True)
        candidates = candidates[: conf.get("max_details_per_run", 200)]

        def enrich(j):
            if j.get("enrich"):
                try:
                    j.update({k: v for k, v in (j["enrich"](j) or {}).items() if v not in (None, "")})
                except Exception:
                    pass
                feeds.polite_pause()
            return j

        with cf.ThreadPoolExecutor(6) as ex:
            candidates = list(ex.map(enrich, candidates))
        if require and checker:
            unknown = {j["company"] for j in candidates if j.get("h1b_level") == "unknown" and not h1b_lookup(j["company"])
                       and prefilter.check(j, cfg, 0, skip_title=(name == "hn_who_is_hiring"))[0]}
            _web_lookups(checker, sorted(unknown), log)
        kept = 0
        for j in candidates:
            norm_co = re.sub(r"[^a-z0-9]", "", j["company"].lower())
            extra = []
            if norm_co in listed:
                extra.append("in your sponsor list")
            uscis = h1b_lookup(j["company"])
            if uscis:
                extra.append(uscis)
            if checker and checker.refused(j["company"]):
                j["h1b_level"], j["h1b_signal"] = "none", checker.refused(j["company"])
            if extra and j.get("h1b_level") != "none":
                j["h1b_level"] = "strong"
                j["h1b_signal"] = "; ".join(x for x in [j.get("h1b_signal")] + extra if x)
            board = j.pop("board", None)
            if board and fcfg.get("discover_boards", True) and j["h1b_level"] == "strong":
                db.execute("INSERT OR IGNORE INTO discovered_boards (key, ats, slug, name, h1b, source, found_at) VALUES (?,?,?,?,?,?,?)",
                           (f"{board[0]}:{board[1]}", board[0], board[1], j["company"], j["h1b_signal"][:200], name, db.now()))
            if j["h1b_level"] == "strong" and not extra:
                remember_sponsor(j["company"], j.get("h1b_signal") or "", name)
            salary.annotate(j)
            ok, reason, fam, notes = prefilter.check(j, cfg, 0, skip_title=(name == "hn_who_is_hiring"))
            low = salary.below_floor(j, env_int("MIN_SALARY", 100000))
            if ok and low:
                ok, reason = False, low
            elif ok and j["h1b_level"] == "none":
                ok, reason = False, j["h1b_signal"] or "source says no sponsorship"
            elif ok and require and j["h1b_level"] != "strong":
                ok, reason = False, "no H-1B sponsorship signal (screen it anyway if you know they sponsor)"
            row = {k: v for k, v in j.items() if k in JOB_COLUMNS}
            row["role_family"] = row.get("role_family") or fam
            if not db.upsert_job(row):
                continue
            totals["new"] += 1
            if ok:
                kept += 1
                db.update_job(j["id"], status="to_screen", status_reason="; ".join([j.get("h1b_signal") or ""] + notes).strip("; ") or reason)
            else:
                db.update_job(j["id"], status="filtered_out", status_reason=reason)
        totals["passed"] += kept
        db.execute("INSERT OR REPLACE INTO feed_status VALUES (?,?,?,?,?,?)", (name, 1, len(rows), kept, None, db.now()))
        log(f"  feed {name}: {len(rows)} listings, {len(candidates)} relevant and new, {kept} with an H-1B signal queued for screening")
    return totals


# ----------------------------------------------------------------------------- Claude calls

def _job_block(job: dict) -> str:
    h1b = h1b_lookup(job["company"])
    return (
        f"TODAY: {datetime.now().strftime('%B %d, %Y')}\n"
        f"COMPANY: {job['company']}\nROLE: {job['title']}\nLOCATION: {job.get('location')}\n"
        f"POSTED: {job.get('posted_at')}\nURL: {job.get('url')}\n"
        f"ROLE FAMILY (app guess): {job.get('role_family')}\n"
        f"H-1B HISTORY: {h1b or job.get('h1b_note') or 'unknown'}\n"
        f"PREFILTER NOTES: {job.get('status_reason') or 'none'}\n\n"
        f"<job_description>\n{job.get('description', '')[:30000]}\n</job_description>"
    )


def screen_job(job_id: str, log: Log = _print) -> dict | None:
    job = db.get_job(job_id)
    if not db.claim(job_id, "to_screen", "screening"):
        log(f"Skipping screen for {job['company']} | {job['title']}: status is {job['status']}")
        return None
    log(f"Screening: {job['company']} | {job['title']}")
    progress.set_stage(job_id, "screen", "Waiting for a free Claude slot", 0)
    timer = None

    def started():
        nonlocal timer
        timer = progress.Timer("screen")
        progress.set_stage(job_id, "screen", "Claude is judging fit (verdict and match score)", 3, 94, "screen")

    try:
        res = llm.structured(
            user="CALL TYPE: SCREEN\n\n" + _job_block(job),
            schema=SCREEN, model=env("MODEL_SCREEN", "claude-opus-5-5"),
            effort=env("EFFORT_SCREEN", "low"), max_tokens=8000, on_start=started, label="screen",
        )
        timer.stop()
    except llm.BackendUnavailable:
        db.update_job(job_id, status="to_screen")
        progress.clear(job_id)
        raise
    except Exception as e:
        db.update_job(job_id, status="error", status_reason=f"screen failed: {e}"[:500])
        db.add_intervention(job_id, "error", "Screening failed", str(e)[:1000])
        progress.clear(job_id)
        log(f"  ✗ screen failed: {e}")
        return None
    progress.clear(job_id)
    db.resolve_interventions(job_id, ("error",), "screened successfully")

    verdict = res["verdict"]
    fields = dict(screen_json=res, verdict=verdict, match_score=res["match_score"])
    if res.get("sponsorship_signal") == "explicit_yes":
        from .h1b import remember
        remember(fields.get("company") or job["company"], "the JD says it sponsors (Claude screen)", "screen")
    if job["id"].startswith(("ext:", "hn:")):   # free-text sources: trust Claude's reading of company and title
        if res.get("company_name"):
            fields["company"] = res["company_name"][:120]
        if res.get("job_title"):
            fields["title"] = res["job_title"][:200]
    threshold = env_int("MIN_MATCH_FOR_DOCS", 7)
    if verdict == "DON'T APPLY":
        fields.update(status="rejected", status_reason="; ".join(res["deal_breakers"] or res["why"])[:500])
    elif (res["match_score"] or 0) < threshold:      # your Settings: only build documents at or above this match
        fields.update(status="below_threshold",
                      status_reason=f"Match {res['match_score']}/10 is below your {threshold}/10 threshold for building documents")
    elif verdict == "APPLY WITH RISK" and env("AUTO_TAILOR_RISK", "1") != "1":
        fields.update(status="needs_decision", status_reason=res["biggest_risk"][:500])
        db.add_intervention(job_id, "decision", "Apply with risk? Your call",
                            f"Match {res['match_score']}/10. Risk: {res['biggest_risk']}\n\n" + "\n".join(f"• {w}" for w in res["why"]))
    else:
        fields.update(status="to_tailor", status_reason=f"{verdict} {res['match_score']}/10")
    if db.get_job(job_id)["status"] != "screening":   # you skipped it while Claude was screening: keep your decision
        log(f"  ↷ {job['company']} was skipped meanwhile; discarding the screen result")
        return None
    db.update_job(job_id, **fields)   # screen-stage questions are never shown; tailoring asks at most one
    log(f"  → {verdict} ({res['match_score']}/10)")
    return res


def _tailor_prompt(job: dict) -> str:
    extra = ""
    if job.get("status_reason") == "proceed anyway":
        extra = "\nThe candidate replied \"proceed anyway\" to a DON'T APPLY verdict. Build the package; keep the verdict on record in adversarial_failures.\n"
    screen = json.dumps(job.get("screen_json") or {}, indent=1)
    return f"CALL TYPE: TAILOR\n{extra}\nYOUR EARLIER SCREEN RESULT:\n{screen}\n\n{_job_block(job)}"


def _revise_prompt(job: dict, prev: dict, problems: list[str]) -> str:
    doc = {k: prev[k] for k in ("summary", "career_objective", "education", "skills", "experience", "projects", "leadership", "cover_letter") if k in prev}
    return (
        "CALL TYPE: REVISE (PATCH)\n\nThe app rendered your package and its checks found these problems. Fix every one "
        "honestly (real facts only, per Section I / I-B) with the SMALLEST set of changes, leaving everything else "
        "untouched. Return only the changed fields as patches:\n"
        "- path is one of: summary | experience[i].bullets[j] | projects[i].bullets[j] | skills[i].items | "
        "leadership[i].text | leadership[i].date | education[i].coursework (comma-separated courses) | cover_letter.paragraphs[i] | "
        "cover_letter.re_line | cover_letter.greeting | cover_letter.closing  (indexes start at 0)\n"
        "- op: set (replace the value), append (add a new bullet: use experience[i].bullets or projects[i].bullets), "
        "delete (remove that item).\n"
        "One-line bullets must be about 100–112 characters including spaces (the problems list says how far off each is). "
        "Every rule of the master prompt still applies to each changed line.\n\n"
        "PROBLEMS:\n" + "\n".join(f"- {p}" for p in problems) +
        f"\n\nCURRENT DOCUMENT TEXT:\n{json.dumps(doc, ensure_ascii=False)}\n\n{_job_block(job)}"
    )


# ----------------------------------------------------------------------------- rendering + checks

def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", s) or "Role"


def render(job: dict, data: dict) -> dict:
    """Draw the resume and cover letter straight to PDF, choosing the loosest spacing that fits one A4 page.

    Layout is computed with the same font metrics used for drawing, so fit checks are exact and instant.
    """
    company, position = _safe(data["company_slug"]), _safe(data["position_slug"])
    outdir = OUTPUT_DIR / f"{datetime.now():%Y-%m-%d}_{company}_{position}_{job['id'].split(':')[-1][:8]}"
    outdir.mkdir(parents=True, exist_ok=True)
    name = data["header"]["name"].title()
    resume_pdf = outdir / f"{company}{position}.pdf"
    cover_pdf = outdir / f"{company}{position}_CoverLetter.pdf"
    problems: list[str] = []

    fits = [(L, *pdfgen.resume_fits(data, L)) for L in pdfgen.LAYOUTS]
    ok = [(L, gap) for L, fit, gap in fits if fit]
    if ok:
        L, gap = ok[-1]                     # loosest spacing that still fits
    else:
        L, _, gap = fits[0]
        problems.append(f"Resume overflows one A4 page by about {int(-gap // L.line) + 1} line(s) even at the tightest "
                        "allowed spacing; trim per the TRIM ORDER (9.6)")
    pdfgen.build_resume(data, str(resume_pdf), L, f"{name} — Resume — {data['exact_role_title']}, {job['company']}")
    st = pdfx.pdf_stats(resume_pdf)
    if st["words"] > 670:
        problems.append(f"Resume has {st['words']} words; hard cap is 670")
    if gap > 3 * L.line:
        problems.append(f"Blank band at the bottom of the resume: about {int(gap // L.line)} empty lines at the loosest "
                        "spacing; add real one-line bullets per the FILL ORDER (9.6)")
    problems += pdfx.resume_line_checks(data, L.body)

    # Cover letter: 11.5pt / 1.2 by default; loosen or tighten within the X.6 range to fill one page.
    hdr, letter = data["header"], data["cover_letter"]
    presets = [(11, 1.15, 8), (11.5, 1.15, 8), (11.5, 1.2, 9), (12, 1.2, 9), (12, 1.25, 10)]
    fitting = [p for p in presets if pdfgen.cover_fits(hdr, letter, *p)[0]]
    chosen = fitting[-1] if fitting else presets[0]
    if not fitting:
        problems.append("Cover letter runs past one page; trim P3 to 2 sentences or merge P4+P5")
    pdfgen.build_cover(hdr, letter, str(cover_pdf), *chosen, title_meta=f"{name} — Cover Letter — {data['exact_role_title']}, {job['company']}")
    cst = pdfx.pdf_stats(cover_pdf)
    problems += pdfx.cover_letter_checks(letter)
    return {
        "resume_pdf": str(resume_pdf), "cover_pdf": str(cover_pdf), "resume_docx": None, "cover_docx": None,
        "checks": {
            "layout": {"body_pt": L.body, "line_pt": L.line, "before_title_pt": L.before_title},
            "resume": {"pages": 1 if ok else 2, "words": st["words"], "bottom_gap_pt": round(gap, 1), "links": st["links"]},
            "cover": {"pages": 1 if fitting else 2, "words": cst["words"], "bottom_gap_pt": round(pdfgen.cover_fits(hdr, letter, *chosen)[1], 1)},
            "problems": problems,
        },
    }


PATH_RE = re.compile(r"^(summary|career_objective|cover_letter\.(re_line|greeting|closing)|cover_letter\.paragraphs\[(\d+)\]|"
                     r"(experience|projects)\[(\d+)\]\.bullets(\[(\d+)\])?|skills\[(\d+)\]\.items|leadership\[(\d+)\]\.(text|date)|"
                     r"education\[(\d+)\]\.coursework)$")


def apply_patch(data: dict, changes: list[dict]) -> tuple[dict, int]:
    """Apply Claude's targeted fixes (set / append / delete at a path). Unknown paths are ignored."""
    out = json.loads(json.dumps(data))
    applied = 0
    for ch in changes:
        path, op, val = ch.get("path", "").strip(), ch.get("op", "set"), (ch.get("value") or "").strip().rstrip()
        m = PATH_RE.match(path)
        if not m:
            continue
        try:
            if path in ("summary", "career_objective"):
                out["summary" if "summary" in out else "career_objective"] = val
            elif m.group(2):
                out["cover_letter"][m.group(2)] = val
            elif m.group(3):
                paras = out["cover_letter"]["paragraphs"]
                i = int(m.group(3))
                if op == "delete":
                    paras.pop(i)
                elif op == "append" or i >= len(paras):
                    paras.append(val)
                else:
                    paras[i] = val
            elif m.group(4):
                lst = out[m.group(4)][int(m.group(5))]["bullets"]
                if op == "append" or m.group(7) is None:
                    lst.append(val.rstrip("."))
                elif op == "delete":
                    lst.pop(int(m.group(7)))
                else:
                    lst[int(m.group(7))] = val.rstrip(".")
            elif m.group(8):
                out["skills"][int(m.group(8))]["items"] = val
            elif m.group(9):
                out["leadership"][int(m.group(9))][m.group(10)] = val
            elif m.group(11):
                out["education"][int(m.group(11))]["coursework"] = [x.strip() for x in val.split(",") if x.strip()]
            applied += 1
        except (IndexError, KeyError, ValueError):
            continue
    return out, applied


def delete_documents(job: dict) -> int:
    """Delete a job's generated resume / cover letter files (only inside data/output) and its folder if empty."""
    removed, folders = 0, set()
    for col in ("resume_pdf", "cover_pdf", "resume_docx", "cover_docx"):
        path = job.get(col)
        if path and output_path_ok(path) and Path(path).exists():
            Path(path).unlink()
            folders.add(Path(path).parent)
            removed += 1
    for folder in folders:
        if folder.resolve() != OUTPUT_DIR.resolve() and output_path_ok(str(folder)) and not any(folder.iterdir()):
            folder.rmdir()
    return removed


def skip_job(job_id: str) -> int:
    """You skipped this job: mark it Don't apply, delete its documents and outreach drafts, clear its to-dos."""
    job = db.get_job(job_id)
    if not job:
        return 0
    removed = delete_documents(job)
    db.update_job(job_id, status="rejected", verdict="DON'T APPLY", status_reason="Skipped by you",
                  resume_pdf=None, cover_pdf=None, resume_docx=None, cover_docx=None)
    db.execute("DELETE FROM outreach WHERE job_id=?", (job_id,))
    db.execute("DELETE FROM referral_asks WHERE job_id=?", (job_id,))
    db.resolve_interventions(job_id, ("decision", "submit", "missing_info", "quality", "review", "outreach", "error", "referral"), "skipped by you")
    progress.clear(job_id)
    return removed


def hold_below_threshold() -> int:
    """Move queued jobs under the current match threshold out of the build queue (jobs you approved by hand stay)."""
    threshold = env_int("MIN_MATCH_FOR_DOCS", 7)
    rows = db.fetchall("SELECT id, match_score FROM jobs WHERE status IN ('to_tailor','needs_decision') AND match_score IS NOT NULL "
                       "AND match_score < ? AND coalesce(status_reason,'') NOT IN ('approved by you','proceed anyway','build anyway')", (threshold,))
    for r in rows:
        db.update_job(r["id"], status="below_threshold",
                      status_reason=f"Match {r['match_score']}/10 is below your {threshold}/10 threshold for building documents")
        db.resolve_interventions(r["id"], ("decision",), "below match threshold")
    return len(rows)


def release_above_threshold() -> int:
    """After lowering the threshold, put held jobs that now qualify back in the build queue."""
    threshold = env_int("MIN_MATCH_FOR_DOCS", 7)
    rows = db.fetchall("SELECT id FROM jobs WHERE status='below_threshold' AND match_score >= ?", (threshold,))
    for r in rows:
        db.update_job(r["id"], status="to_tailor", status_reason=f"meets your {threshold}/10 threshold")
    return len(rows)


def tailor_job(job_id: str, log: Log = _print) -> bool:
    job = db.get_job(job_id)
    if not db.claim(job_id, "to_tailor", "tailoring"):   # built or taken by another thread; never pay twice
        log(f"Skipping {job['company']} | {job['title']}: status is {job['status']}")
        return False
    pv = llm.prompt_version()   # the prompt file is read fresh on every call below; record which save it was
    log(f"Tailoring: {job['company']} | {job['title']} (master prompt saved {pv.replace('T', ' ')})")
    db.update_job(job_id, prompt_version=pv)
    model = env("MODEL_TAILOR", "claude-opus-5-5")
    effort, revise_effort = env("EFFORT_TAILOR", "medium"), env("EFFORT_REVISE", "low")
    P = lambda stage, base, span=0, step="": progress.set_stage(job_id, "tailor", stage, base, span, step)
    P("Waiting for a free Claude slot", 1)
    timers: dict = {}

    def gen_started():
        timers["gen"] = progress.Timer("generate")
        P("Claude is writing the resume, cover letter, LinkedIn notes, and teardown", 4, 66, "generate")

    try:
        data = llm.structured(user=_tailor_prompt(job), schema=TAILOR, model=model, effort=effort, max_tokens=64000,
                              on_start=gen_started, label="tailor")
        timers["gen"].stop()
        P("Rendering PDFs and checking one page, word cap, and line fill", 70, 6, "render")
        t = progress.Timer("render")
        out = render(job, data)
        t.stop()
        max_rev = env_int("MAX_REVISIONS", 2)
        for attempt in range(max_rev):
            probs = out["checks"]["problems"]
            if not probs:
                break
            log(f"  revision {attempt + 1}: {len(probs)} check(s) failed")
            lo = 76 + attempt * (20 // max_rev)
            span = 20 // max_rev

            def rev_started(lo=lo, span=span, attempt=attempt, n=len(probs)):
                timers["rev"] = progress.Timer("revise")
                P(f"Revision {attempt + 1}: Claude is fixing {n} failed check(s)", lo, span * 0.75, "revise")

            patch = llm.structured(user=_revise_prompt(job, data, probs), schema=REVISE_PATCH, model=model, effort=revise_effort,
                                   max_tokens=16000, on_start=rev_started, label="revise")
            timers["rev"].stop()
            data2, n = apply_patch(data, patch.get("changes") or [])
            if not n:
                break
            P(f"Revision {attempt + 1}: re-rendering and re-checking", lo + span * 0.75, span * 0.25, "render")
            out2 = render(job, data2)
            if len(out2["checks"]["problems"]) <= len(probs):
                data, out = data2, out2
    except llm.BackendUnavailable:
        db.update_job(job_id, status="to_tailor")
        progress.clear(job_id)
        raise
    except Exception as e:
        progress.clear(job_id)
        db.update_job(job_id, status="error", status_reason=f"tailor failed: {e}"[:500])
        db.add_intervention(job_id, "error", "Tailoring failed", f"{e}\n\n{traceback.format_exc()[-1500:]}")
        log(f"  ✗ tailor failed: {e}")
        return False

    if db.get_job(job_id)["status"] != "tailoring":   # you skipped it while documents were being built
        delete_documents({"resume_pdf": out["resume_pdf"], "cover_pdf": out["cover_pdf"]})
        progress.clear(job_id)
        log(f"  ↷ {job['company']} was skipped meanwhile; deleted the documents just built")
        return False
    td = data["teardown"]
    db.update_job(
        job_id, status="ready", status_reason=f"documents ready; recruiter call {td['final_call']}",
        tailor_json=data, checks_json=out["checks"],
        keyword_relevancy=data["keyword_relevancy"], rw_score=data["rw_score"]["total"], final_call=td["final_call"],
        resume_pdf=out["resume_pdf"], resume_docx=out["resume_docx"], cover_pdf=out["cover_pdf"], cover_docx=out["cover_docx"],
    )
    db.resolve_interventions(job_id, ("decision", "error"), "documents built")
    _queue_human_steps(job, data, out["checks"])
    try:                                         # someone you know there? draft a referral text first
        from . import contacts
        contacts.queue_referral_asks(job_id, log)
    except llm.BackendUnavailable:
        pass                                      # usage limit: the job is ready; the text can be drafted later
    except Exception as e:
        log(f"  (referral text not drafted: {e})")
    progress.clear(job_id)
    log(f"  ✓ ready: KW {data['keyword_relevancy']}% · RW {data['rw_score']['total']} · recruiter {td['final_call']}")
    return True


FIT_PROBLEM = re.compile(r"overflows one A4 page|WRAPS to two lines|runs past one page|runs past 2 lines|Resume has \d+ words", re.I)


def refit_job(job_id: str, log: Log = _print) -> bool:
    """'Fix it' for document-quality warnings. First re-fit the existing text with no Claude call (letter-spacing
    squeeze for lines a hair too long, then the smaller sizes the prompt allows). Only if lines still wrap or the
    page still overflows, ask Claude for small trim patches (up to MAX_REVISIONS rounds)."""
    job = db.get_job(job_id)
    data = job and job.get("tailor_json")
    if not data:
        log(f"Fix skipped: {job_id} has no documents to fix")
        return False
    P = lambda stage, base, span=0, step="": progress.set_stage(job_id, "tailor", stage, base, span, step)
    log(f"Fixing documents: {job['company']} | {job['title']}")
    P("Re-fitting to one page: tighter spacing within your rules (no Claude call)", 10, 20, "render")
    old = {"resume_pdf": job.get("resume_pdf"), "cover_pdf": job.get("cover_pdf")}
    try:
        out = render(job, data)
        model, revise_effort = env("MODEL_TAILOR", "claude-opus-5-5"), env("EFFORT_REVISE", "low")
        rounds = max(1, env_int("MAX_REVISIONS", 2))
        for attempt in range(rounds):
            fit = [p for p in out["checks"]["problems"] if FIT_PROBLEM.search(p)]
            if not fit:
                break
            log(f"  still {len(fit)} fit problem(s) after re-fitting; asking Claude for a trim (round {attempt + 1})")
            P(f"Claude is trimming {len(fit)} line(s) so everything fits (round {attempt + 1})", 30 + attempt * 30, 25, "revise")
            patch = llm.structured(user=_revise_prompt(job, data, fit), schema=REVISE_PATCH, model=model,
                                   effort=revise_effort, max_tokens=16000, label="revise")
            data2, n = apply_patch(data, patch.get("changes") or [])
            if not n:
                break
            out2 = render(job, data2)
            if len([p for p in out2["checks"]["problems"] if FIT_PROBLEM.search(p)]) <= len(fit):
                data, out = data2, out2
    except llm.BackendUnavailable:
        progress.clear(job_id)
        raise
    except Exception as e:
        progress.clear(job_id)
        log(f"  ✗ fix failed: {e}")
        return False
    if old["resume_pdf"] and old["resume_pdf"] != out["resume_pdf"]:
        delete_documents(old)               # the re-fit wrote today's folder; drop the old copies
    db.update_job(job_id, tailor_json=data, checks_json=out["checks"], resume_pdf=out["resume_pdf"], cover_pdf=out["cover_pdf"])
    left = [p for p in out["checks"]["problems"] if FIT_PROBLEM.search(p)]
    title = "Documents do not fit on one page after revisions"
    if left:
        db.execute("UPDATE interventions SET status='open', detail=?, resolved_at=NULL WHERE job_id=? AND kind='quality' AND title=?",
                   ("\n".join(f"• {p}" for p in left), job_id, title))
        log(f"  ⚠ {len(left)} fit problem(s) remain; they need a human edit")
    else:
        db.resolve_interventions(job_id, ("quality",), "fixed: fits one page")
        lay = out["checks"]["layout"]
        log(f"  ✓ fits one page ({lay['body_pt']}pt body, {lay['line_pt']}pt lines)")
    progress.clear(job_id)
    return not left


def _already_asked(question: str) -> bool:
    """True if a similar question was answered before or is already waiting, so it is never asked twice."""
    import difflib
    from .config import ANSWERS_FILE
    seen = [r["title"] for r in db.fetchall("SELECT title FROM interventions WHERE kind='missing_info'")]
    if ANSWERS_FILE.exists():
        seen += re.findall(r"- Q: (.+)", ANSWERS_FILE.read_text(encoding="utf-8"))
    q = question.lower()
    return any(difflib.SequenceMatcher(None, q, s.lower()).ratio() > 0.55 for s in seen)


def _queue_human_steps(job: dict, data: dict, checks: dict) -> None:
    jid = job["id"]
    td = data["teardown"]
    # Only interrupt the candidate for things that block a submission. Everything else lives on the job page.
    if checks["resume"]["pages"] != 1 or checks["cover"]["pages"] != 1:
        db.add_intervention(jid, "quality", "Documents do not fit on one page after revisions",
                            "\n".join(f"• {p}" for p in checks["problems"]))
    if td["final_call"] == "NO":
        db.add_intervention(jid, "quality", "Recruiter teardown says do NOT submit", td["final_call_reason"])
    q = next((q for q in data.get("missing_info") or [] if q.strip() and not _already_asked(q)), None)
    if q:
        db.add_intervention(jid, "missing_info", q[:300], "Answer once; it is saved to profile/answers.md for every future run.")
    db.add_intervention(jid, "submit", "Review documents and submit the application",
                        f"{job['company']} · {job['title']}\nApply link: {job.get('apply_url') or job.get('url')}")

    co = job["company"]
    t = data["outreach_targets"]
    targets = {
        "hiring_manager": (f"{co} {(t['hiring_manager_titles'] or ['engineering manager'])[0]}", data["linkedin"]["hiring_manager_note"]),
        "recruiter": (f"{co} {(t['recruiter_titles'] or ['technical recruiter'])[0]}", data["linkedin"]["recruiter_note"]),
        "alumni": (f"{co} {applicant().get('school') or 'alumni'}", data["linkedin"]["alumni_note"]),
    }
    for target, (kw, msg) in targets.items():
        url = f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(kw)}&origin=GLOBAL_SEARCH_HEADER"
        db.execute(
            "INSERT OR REPLACE INTO outreach (job_id, target, search_url, message, status, created_at, updated_at) "
            "VALUES (?,?,?,?, COALESCE((SELECT status FROM outreach WHERE job_id=? AND target=?), 'drafted'), ?, ?)",
            (jid, target, url, msg, jid, target, db.now(), db.now()),
        )


# ----------------------------------------------------------------------------- full run

def online(timeout: float = 6) -> bool:
    """Can we reach the job boards at all? (A run with no internet should fail loudly, not 'finish' in 1 second.)"""
    import requests
    for url in ("https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=false", "https://api.github.com"):
        try:
            requests.head(url, timeout=timeout)
            return True
        except requests.RequestException:
            continue
    return False


def run(log: Log = _print, fetch: bool = True, enqueue=None, source: str = "manual") -> dict:
    """Fetch and screen in parallel. Document builds go to `enqueue` (the web worker pool) or run here (CLI)."""
    db.init()
    progress.clear_run()
    progress.set_run("start", "Starting", source=source)
    rid = None
    lines: list[str] = []

    def L(msg: str):
        lines.append(f"{datetime.now():%H:%M:%S} {msg}")
        log(msg)

    db.execute("INSERT INTO runs (started_at, status, source) VALUES (?, 'running', ?)", (db.now(), source))
    rid = db.fetchone("SELECT max(id) AS id FROM runs")["id"]
    stats: dict = {}
    try:
        if fetch and not online():
            progress.set_run("fetch", "No internet connection; nothing could be fetched")
            raise ConnectionError("no internet connection")
        if fetch:
            stats["fetch"] = fetch_all(L)
            f = stats["fetch"]
            if f["companies"] and f["errors"] == f["companies"]:
                raise ConnectionError(f"all {f['companies']} job boards were unreachable")
        to_screen = db.query_jobs("status='to_screen'", order="CASE h1b_level WHEN 'strong' THEN 0 ELSE 1 END, posted_at DESC",
                                  limit=env_int("MAX_SCREEN_PER_RUN", 60))
        lanes = max(1, env_int("PARALLEL_JOBS", 3))
        L(f"Screening {len(to_screen)} job(s) with Claude, {lanes} at a time…")
        progress.set_run("screen", f"Claude is screening {len(to_screen)} new job(s), {lanes} at a time", 0, len(to_screen))

        def screen_one(j):
            try:
                return screen_job(j["id"], L)
            finally:
                progress.bump_run()

        with cf.ThreadPoolExecutor(lanes) as ex:
            stats["screened"] = sum(1 for r in ex.map(screen_one, to_screen) if r)
        if stats["screened"]:
            db.execute("UPDATE interventions SET status='resolved', resolution='backend working', resolved_at=? "
                       "WHERE job_id IS NULL AND title='Claude backend needs setup' AND status='open'", (db.now(),))
        hold_below_threshold()
        to_tailor = db.query_jobs("status='to_tailor'", order="match_score DESC, posted_at DESC", limit=env_int("MAX_TAILOR_PER_RUN", 15))
        progress.set_run("docs", f"Queuing {len(to_tailor)} job(s) for tailored resumes and cover letters", len(to_tailor), max(1, len(to_tailor)))
        if enqueue:
            for j in to_tailor:
                enqueue("tailor", j["id"])
            stats["queued_for_documents"] = len(to_tailor)
            L(f"Queued {len(to_tailor)} job(s) for documents ({lanes} build at a time).")
        else:
            L(f"Building documents for {len(to_tailor)} job(s), {lanes} at a time…")
            with cf.ThreadPoolExecutor(lanes) as ex:
                stats["tailored"] = sum(1 for r in ex.map(lambda j: tailor_job(j["id"], L), to_tailor) if r)
        status = "done"
    except llm.BackendUnavailable as e:
        if e.retry_at:                         # usage limit: the worker pauses and resumes this run automatically
            L(f"⏸ Claude usage limit reached; pausing until {datetime.fromtimestamp(e.retry_at):%I:%M %p}.")
            db.execute("UPDATE runs SET finished_at=?, status='paused', stats_json=?, log=? WHERE id=?",
                       (db.now(), json.dumps(stats), "\n".join(lines), rid))
            progress.clear_run()
            raise
        L(f"✗ Claude backend problem: {e}")
        db.add_intervention(None, "error", "Claude backend needs setup", str(e))
        status = "failed"
    except llm.LLMError as e:
        L(f"✗ Claude backend problem: {e}")
        db.add_intervention(None, "error", "Claude backend needs setup", str(e))
        status = "failed"
    except ConnectionError as e:
        L(f"✗ Run stopped: {e}. It will run again at the next scheduled time, or click Run now when you're back online.")
        stats["error"] = str(e)
        status = "failed"
    except Exception as e:
        L(f"✗ Run crashed: {e}\n{traceback.format_exc()}")
        stats["error"] = f"crashed: {e}"[:200]
        status = "failed"
    db.execute("UPDATE runs SET finished_at=?, status=?, stats_json=?, log=? WHERE id=?",
               (db.now(), status, json.dumps(stats), "\n".join(lines), rid))
    progress.clear_run()
    L(f"Run {status}: {stats}")
    return stats


def output_path_ok(path: str) -> bool:
    try:
        return Path(path).resolve().is_relative_to(OUTPUT_DIR.resolve())
    except (OSError, ValueError):
        return False


def today_utc_bounds() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
