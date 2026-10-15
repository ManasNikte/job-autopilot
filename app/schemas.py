"""JSON schemas for Claude's structured outputs. Strict style: every object lists all keys as required."""
from __future__ import annotations


def obj(**props) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def arr(items: dict) -> dict:
    return {"type": "array", "items": items}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}


def enum(*vals: str) -> dict:
    return {"type": "string", "enum": list(vals)}


GATE = obj(result=enum("PASS", "FAIL", "FLAG"), note=S)

SCREEN = obj(
    company_name=S,
    job_title=S,
    verdict=enum("APPLY", "APPLY WITH RISK", "DON'T APPLY"),
    match_score=I,
    score_arithmetic=S,
    role_type=S,
    why=arr(S),
    deal_breakers=arr(S),
    recruiter_six_second_read=S,
    biggest_risk=S,
    eligibility=obj(visa=GATE, degree=GATE, dealbreakers=GATE, location_start=GATE),
    experience_required=S,
    sponsorship_signal=enum("explicit_yes", "explicit_no", "unclear"),
    competition_level=enum("Low", "Medium", "High", "Very High"),
    nearest_alternative=S,
    missing_info=arr(S),
)

LINK = obj(text=S, url=S)

TAILOR = obj(
    company_slug=S,
    position_slug=S,
    exact_role_title=S,
    projects_selected=arr(S),
    selection_reason=S,
    header=obj(name=S, line2=arr(LINK), line3=arr(LINK)),
    summary_title=S,          # the section title your master prompt uses (e.g. "SUMMARY")
    summary=S,
    education=arr(obj(degree=S, institution=S, location=S, dates=S, coursework=arr(S))),
    skills=arr(obj(label=S, items=S)),
    experience=arr(obj(company=S, location=S, title=S, dates=S, bullets=arr(S))),
    projects=arr(obj(title=S, stack=S, dates=S, bullets=arr(S))),
    leadership=arr(obj(label=S, text=S, date=S)),   # date: right-aligned "Mon YYYY" (or "" if undated)
    cover_letter=obj(
        date=S, recipient_lines=arr(S), re_line=S, greeting=S, paragraphs=arr(S), closing=S, signature=S,
    ),
    linkedin=obj(hiring_manager_note=S, recruiter_note=S, alumni_note=S),
    outreach_targets=obj(hiring_manager_titles=arr(S), recruiter_titles=arr(S), referral_ideas=arr(S)),
    keyword_table=arr(obj(keyword=S, in_resume=B, locations=S, evidence_type=enum("JOB", "PROJECT", "CERT-ONLY", "ADJACENT", "NONE"))),
    keyword_relevancy=I,
    rw_score=obj(impact=I, brevity=I, style=I, ats=I, total=I, failed_checks=arr(S)),
    teardown=obj(
        skim_test=S,
        credibility_flags=arr(S),
        timeline_questions=arr(S),
        hardest_questions=arr(obj(question=S, answer_strategy=S)),
        scorecard=obj(relevance=I, credibility=I, impact=I, readability=I, ats=I, formatting=I),
        must_fixes_applied=arr(S),
        unfixable_on_paper=arr(S),
        final_call=enum("YES", "MAYBE", "NO"),
        final_call_reason=S,
    ),
    adversarial_failures=arr(S),
    missing_info=arr(S),
)

# Targeted fixes after the app's checks (much smaller and faster than regenerating the whole package).
REVISE_PATCH = obj(
    changes=arr(obj(path=S, op=enum("set", "append", "delete"), value=S)),
    note=S,
)
