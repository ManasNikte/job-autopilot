"""Checks on generated PDFs: page/word counts, blank band at the bottom, line fill, cover letter rules."""
from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader

A4_HEIGHT_PT = 841.89
BOTTOM_MARGIN_PT = 28.8          # 0.4"
BULLET_WIDTH_PT = 509.5          # 10610 - 420 twips
OBJECTIVE_WIDTH_PT = 530.5


def pdf_stats(pdf: Path) -> dict:
    """Page count, word count, lowest text baseline (to detect a blank band at the bottom), dash characters."""
    r = PdfReader(str(pdf))
    ys: list[float] = []

    def visit(text, cm, tm, font, size):
        if text and text.strip():
            ys.append(tm[4] * cm[1] + tm[5] * cm[3] + cm[5])

    first = r.pages[0].extract_text(visitor_text=visit) or ""
    full = "\n".join(p.extract_text() or "" for p in r.pages)
    lowest = min(ys) if ys else A4_HEIGHT_PT
    return {
        "pages": len(r.pages),
        "words": len(full.split()),
        "bottom_gap_pt": round(lowest - BOTTOM_MARGIN_PT, 1),
        "text": full,
        "first_page_text": first,
        "links": sum(len(p.get("/Annots") or []) for p in r.pages),
    }


def text_width_pt(text: str, size_pt: float, style: str = "regular") -> float | None:
    from .pdfgen import width
    return width(text, {"regular": "R", "bold": "B", "italic": "I"}[style], size_pt)


def bullet_fill(text: str, size_pt: float) -> float | None:
    w = text_width_pt(text, size_pt)
    return None if w is None else w / BULLET_WIDTH_PT


DASHES = re.compile(r"[-‐‑‒–—―−]")


def resume_line_checks(data: dict, body_pt: float) -> list[str]:
    """Per-bullet line fill (Section IX-V.9): every bullet must be 95–100% of one line."""
    problems = []
    groups = [(f"{e['company']} bullet", e["bullets"]) for e in data["experience"]] + \
             [(f"{p['title']} bullet", p["bullets"]) for p in data["projects"]]
    for label, bullets in groups:
        for i, b in enumerate(bullets, 1):
            fill = bullet_fill(b, body_pt)
            if fill is None:
                return ["(font file not found; line-fill not measured)"]
            if b.strip().endswith("."):
                problems.append(f"{label} {i} ends with a period: \"{b}\"")
            if fill > 1.0:
                problems.append(f"{label} {i} WRAPS to two lines ({fill:.0%} of one line, cut ~{int((fill - 1) * 95) + 2} characters): \"{b}\"")
            elif fill < 0.95:
                problems.append(f"{label} {i} is SHORT ({fill:.0%} of the line, add ~{int((0.97 - fill) * 95)} characters of real detail): \"{b}\"")
    obj, name = summary_text(data), summary_title(data).title()
    w = text_width_pt(obj, body_pt)
    if w and w > 2 * OBJECTIVE_WIDTH_PT:
        problems.append(f"{name} runs past 2 lines ({w / OBJECTIVE_WIDTH_PT:.2f} lines)")
    elif w and w < 1.9 * OBJECTIVE_WIDTH_PT:
        problems.append(f"{name} line 2 is short ({w / OBJECTIVE_WIDTH_PT - 1:.0%} of the line; needs >= 90%)")
    for s in data["skills"]:
        lw = text_width_pt(s["items"], body_pt)
        if lw and lw > 8767 / 20:
            problems.append(f"Skills row '{s['label']}' items run past the line ({lw / (8767 / 20):.0%}); shorten them")
        label_w = text_width_pt(s["label"].rstrip(":"), body_pt, "bold")
        if label_w and label_w > 1843 / 20 - 4:
            problems.append(f"Skills label '{s['label']}' is too wide for its column; use a shorter label (max ~14 characters)")
    for i, ed in enumerate(data["education"]):
        cw = text_width_pt(", ".join(ed.get("coursework") or []), body_pt - 0.5, "italic")
        cell = (6946 if i == 0 else 6521) / 20
        if cw and cw > cell:
            problems.append(f"{ed['degree']} coursework wraps ({cw / cell:.0%} of its cell); drop or shorten a course")
    for item in data["leadership"]:
        lw = (text_width_pt(item["label"].rstrip(":") + ": ", body_pt, "bold") or 0) + (text_width_pt(item["text"], body_pt) or 0)
        if item.get("date"):
            lw += (text_width_pt(item["date"], body_pt, "bold") or 0) + 12   # right-aligned date plus a gap
        if lw > OBJECTIVE_WIDTH_PT:
            problems.append(f"Leadership & Achievements line '{item['label']}' wraps ({lw / OBJECTIVE_WIDTH_PT:.0%} of a line); shorten it")
    if len(data["skills"]) != 4:
        problems.append(f"Technical Skills has {len(data['skills'])} rows; needs exactly 4")
    for p in data["projects"]:
        if len(p["bullets"]) != 3:
            problems.append(f"Project '{p['title']}' has {len(p['bullets'])} bullets; needs exactly 3")
    return problems


def cover_letter_checks(letter: dict) -> list[str]:
    problems = []
    fields = [letter["re_line"], letter["greeting"], letter["closing"], *letter["recipient_lines"], *letter["paragraphs"]]
    for f in fields:
        if DASHES.search(f):
            problems.append(f"Cover letter contains a hyphen/dash: \"{f[:120]}\"")
    words = sum(len(p.split()) for p in letter["paragraphs"])
    if not 330 <= words <= 420:
        problems.append(f"Cover letter body is {words} words; needs 330–420")
    for p in letter["paragraphs"]:
        for s in re.split(r"(?<=[.!?])\s+", p):
            if len(s.split()) > 30:
                problems.append(f"Cover letter sentence over 30 words: \"{s[:100]}…\"")
    return problems


def summary_text(data: dict) -> str:
    """The summary paragraph; packages built before v18 prompts called it career_objective."""
    return (data.get("summary") or data.get("career_objective") or "").strip()


def summary_title(data: dict) -> str:
    """Section title from the package (the master prompt decides it), e.g. 'SUMMARY'."""
    return (data.get("summary_title") or ("Summary" if "summary" in data else "Career Objective")).strip()
