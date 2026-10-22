"""Resume and cover letter drawn straight to PDF (no .docx, no LibreOffice/Word conversion).

Geometry follows Section IX-V of the master prompt: A4, Times New Roman, 0.35"/0.4"/0.45" margins,
name 14pt bold caps, 11pt contact lines with live black links, 12pt bold caps section titles over a
0.75pt rule, 10.5pt body on a 13.5pt pitch, column positions of the template's borderless tables,
round bullets, distributed (fully justified) bullets and Career Objective, dates flush right.
Text stays real, selectable text in reading order (ATS-safe), with fonts embedded.

Because layout is computed here with the same font metrics used for drawing, every check (one page,
line fill per bullet, blank band at the bottom) is exact, and choosing spacing costs milliseconds.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

# Times New Roman on macOS, Windows, and Linux (ttf-mscorefonts); otherwise the metric-compatible Liberation Serif.
FONT_DIRS = ["/System/Library/Fonts/Supplemental", "/Library/Fonts", str(Path.home() / "Library/Fonts"),
             "C:/Windows/Fonts", str(Path.home() / "AppData/Local/Microsoft/Windows/Fonts"),
             "/usr/share/fonts/truetype/msttcorefonts", "/usr/share/fonts/msttcore", "/usr/share/fonts/TTF",
             "/usr/share/fonts/truetype/liberation", "/usr/share/fonts/liberation-serif", "/usr/share/fonts/liberation",
             str(Path.home() / ".local/share/fonts"), str(Path.home() / ".fonts")]
FACES = {
    "R": ["Times New Roman.ttf", "times.ttf", "Times_New_Roman.ttf", "LiberationSerif-Regular.ttf"],
    "B": ["Times New Roman Bold.ttf", "timesbd.ttf", "Times_New_Roman_Bold.ttf", "LiberationSerif-Bold.ttf"],
    "I": ["Times New Roman Italic.ttf", "timesi.ttf", "Times_New_Roman_Italic.ttf", "LiberationSerif-Italic.ttf"],
    "BI": ["Times New Roman Bold Italic.ttf", "timesbi.ttf", "Times_New_Roman_Bold_Italic.ttf", "LiberationSerif-BoldItalic.ttf"],
}
_registered = False


def _fonts() -> None:
    global _registered
    if _registered:
        return
    for face, names in FACES.items():
        path = next((Path(d) / n for d in FONT_DIRS for n in names if (Path(d) / n).exists()), None)
        if not path:
            raise RuntimeError(f"Times New Roman ({face}) font file not found")
        pdfmetrics.registerFont(TTFont(f"TNR-{face}", str(path)))
    _registered = True


def width(text: str, face: str, size: float) -> float:
    _fonts()
    return pdfmetrics.stringWidth(text, f"TNR-{face}", size)


PAGE_W, PAGE_H = A4                      # 595.27 x 841.89 pt
TW = lambda twips: twips / 20.0          # twips -> points

# Resume page geometry (IX-V.1) and table columns (IX-V.4–7)
R_TOP, R_BOTTOM, R_SIDE = 25.2, 28.8, 32.4
R_TEXT_W = PAGE_W - 2 * R_SIDE           # ≈ 530.5pt
BULLET_X, BULLET_TEXT_X = TW(60), TW(420)
BULLET_W = R_TEXT_W - BULLET_TEXT_X      # ≈ 509.5pt
SKILL_COL = TW(1843)
ROLE_COL = TW(4253)
COURSE_COLS = (TW(1985), TW(2410))       # MS row, BE row


@dataclass(frozen=True)
class Layout:
    body: float = 10.5
    line: float = 13.5
    before_title: float = 12

    @property
    def small(self) -> float:
        return round(self.body - 0.5, 1)


# Tightest first. The 10.25pt and 10pt bodies stay inside the prompt's allowed range (IX-V.2: body 10–11pt,
# pitch 13.2–14.5pt, 10–14pt before titles) and are used only when 10.5pt can't fit one page.
LAYOUTS = [Layout(10.0, 13.2, 10), Layout(10.25, 13.2, 10), Layout(10.5, 13.2, 10), Layout(10.5, 13.5, 12),
           Layout(10.5, 14.0, 13), Layout(10.5, 14.5, 14)]
DEFAULT_LAYOUT = Layout(10.5, 13.5, 12)
# A bullet up to 3% too long stays on its one line with slightly tighter letter spacing (about 0.03pt per
# character, invisible at print size) instead of wrapping onto a second line.
SQUEEZE = 1.03


class _Page:
    """Tiny drawing helper: y runs downward from the top margin; `dry` measures without drawing."""

    def __init__(self, c: canvas.Canvas | None, top: float, left: float, text_w: float):
        self.c, self.y, self.left, self.text_w = c, top, left, text_w

    def runs(self, x: float, baseline: float, parts: list[tuple[str, str, float]]) -> float:
        """Mixed-font pieces of one line drawn as a single text object, so ATS text extraction keeps the line together."""
        parts = [p for p in parts if p[0]]
        if self.c and parts:
            t = self.c.beginText(self.left + x, PAGE_H - baseline)
            for text, face, size in parts:
                t.setFont(f"TNR-{face}", size)
                t.textOut(text)
            self.c.drawText(t)
        return x + sum(width(text, face, size) for text, face, size in parts)

    def right(self, baseline: float, text: str, face: str, size: float) -> None:
        if self.c:
            self.c.setFont(f"TNR-{face}", size)
            self.c.drawRightString(self.left + self.text_w, PAGE_H - baseline, text)

    def justified(self, x: float, baseline: float, text: str, face: str, size: float, w: float, stretch: bool) -> None:
        if not self.c:
            return
        # Word spacing is PDF text *state*: it persists into later text unless reset, so isolate it.
        self.c.saveState()
        t = self.c.beginText(self.left + x, PAGE_H - baseline)
        t.setFont(f"TNR-{face}", size)
        gaps = text.count(" ")
        natural = width(text, face, size)
        if natural > w and len(text) > 1:                   # a hair too long: tighten letters to keep one line
            t.setCharSpace((w - natural) / (len(text) - 1))
        elif stretch and gaps and natural < w:
            t.setWordSpace((w - natural) / gaps)
        t.textOut(text)
        t.setWordSpace(0)
        t.setCharSpace(0)
        self.c.drawText(t)
        self.c.restoreState()

    def link(self, x: float, baseline: float, w: float, size: float, url: str) -> None:
        if self.c and url:
            y = PAGE_H - baseline
            self.c.linkURL(url, (self.left + x, y - 2, self.left + x + w, y + size), relative=0)


def wrap(text: str, face: str, size: float, w: float) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if width(trial, face, size) <= w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


# Resume templates: all single-column, standard headings, real text in reading order (ATS-friendly).
# The app arranges the sections; the master prompt supplies the content. Chosen in Profile → Resume template.
TEMPLATES = {
    "classic": {"label": "Classic", "order": ["summary", "education", "skills", "experience", "projects", "leadership"],
                "for": "New grads and students: education first, then skills, experience, and projects."},
    "projects_first": {"label": "Projects first", "order": ["summary", "education", "skills", "projects", "experience", "leadership"],
                       "for": "Little or no work experience, strong projects: projects come before experience."},
    "experienced": {"label": "Experience first", "order": ["summary", "experience", "skills", "projects", "education", "leadership"],
                    "for": "Two or more years of work: experience leads, education moves down."},
    "compact": {"label": "Compact", "order": ["summary", "education", "skills", "experience", "projects", "leadership"],
                "compact": True, "for": "A lot to say: one contact line, smaller headings, and tighter spacing."},
}
DEFAULT_TEMPLATE = "classic"
SECTION_TITLES = {"education": "Education", "skills": "Technical Skills", "experience": "Professional Experience",
                  "projects": "Projects", "leadership": "Leadership & Achievements"}


def template_of(data: dict) -> dict:
    return TEMPLATES.get(data.get("_template") or DEFAULT_TEMPLATE, TEMPLATES[DEFAULT_TEMPLATE])


def entry_row_fits(left: list[tuple[str, str, float]], middle: tuple[str, str, float] | None, date: str, body: float) -> bool:
    """Does an entry row (left parts, optional role title at ROLE_COL, date flush right) fit on one line without
    anything touching? Shared by the renderer and the checks."""
    date_w = width(date, "B", body) + 10 if date else 0
    left_w = sum(width(t, f, s) for t, f, s in left)
    room = R_TEXT_W - date_w
    if not middle:
        return left_w <= room
    return left_w + 8 <= ROLE_COL and ROLE_COL + width(*middle) <= room


def _fit_lines(items: list[dict], w: float, size: float) -> list[list[dict]]:
    """Split header contact items into centered lines that each fit the page width (never run off the edge)."""
    sep = width(" | ", "R", size)
    lines, cur, cur_w = [], [], 0.0
    for it in items:
        iw = width(it["text"], "R", size)
        if cur and cur_w + sep + iw > w:
            lines.append(cur)
            cur, cur_w = [], 0.0
        cur_w += (sep if cur else 0) + iw
        cur.append(it)
    if cur:
        lines.append(cur)
    return lines


def _resume_pass(c, data: dict, L: Layout) -> float:
    """Draw (or measure when c is None). Returns the y of the last baseline + descent.

    Nothing can overlap: header contact lines, entry rows, coursework, and skills wrap when they're too long
    (the checks still flag them so the content gets trimmed), and empty sections are left off entirely."""
    tpl = template_of(data)
    compact = tpl.get("compact", False)
    title_size = 11 if compact else 12
    before_title = max(6, L.before_title - 4) if compact else L.before_title
    pg = _Page(c, R_TOP, R_SIDE, R_TEXT_W)
    y = R_TOP
    h = data["header"]

    # Header (IX-V.3): name line exactly 26pt, contact lines 13.8pt, centered; wrapped instead of running off.
    y += 24 if compact else 26
    name = h["name"].upper()
    nx = (R_TEXT_W - width(name, "B", 14)) / 2
    pg.runs(nx, y - 7, [(name, "B", 14)])
    contact = [it for it in (h.get("line2") or []) + (h.get("line3") or []) if it.get("text")] if compact else None
    size = 10.5 if compact else 11
    rows = _fit_lines(contact, R_TEXT_W, size) if compact else \
        [ln for line in (h.get("line2") or [], h.get("line3") or []) for ln in _fit_lines([i for i in line if i.get("text")], R_TEXT_W, size)]
    for line in rows:
        y += 13.2 if compact else 13.8
        parts = []
        for i, item in enumerate(line):
            if i:
                parts.append((" | ", "R", size, ""))
            parts.append((item["text"], "R", size, item.get("url") or ""))
        total = sum(width(t, f, s) for t, f, s, _ in parts)
        x = max(0, (R_TEXT_W - total) / 2)
        pg.runs(x, y - 3, [(t, f, s) for t, f, s, _ in parts])
        for t, f, s, url in parts:
            w = width(t, f, s)
            pg.link(x, y - 3, w, s, url)
            x += w

    def page_break_if_needed(need: float):
        # Never cut text off: an overflowing resume continues on page 2 (and stays flagged by the checks).
        nonlocal y
        if c and y + need > PAGE_H - R_BOTTOM:
            c.showPage()
            y = R_TOP

    def title(text: str):
        nonlocal y
        page_break_if_needed(before_title + 13.8 + L.line)
        y += before_title + (13 if compact else 13.8)
        base = y - 3.5
        pg.runs(0, base, [(text.upper(), "B", title_size)])
        if c:
            c.setLineWidth(0.75)
            c.line(R_SIDE, PAGE_H - (base + 3.2), R_SIDE + R_TEXT_W, PAGE_H - (base + 3.2))
        y += 3

    def body_line(extra_before: float = 0) -> float:
        nonlocal y
        page_break_if_needed(extra_before + L.line)
        y += extra_before + L.line
        return y - L.line * 0.27

    def entry_row(left: list[tuple[str, str, float]], middle: tuple[str, str, float] | None, date: str):
        """One entry line: left parts, an optional middle part at ROLE_COL, and the date flush right. If they
        would collide, the middle part (and then the rest of the left parts) moves to the next line."""
        b = body_line(3)
        room = R_TEXT_W - (width(date, "B", L.body) + 10 if date else 0)
        if entry_row_fits(left, middle, date, L.body):
            pg.runs(0, b, left)
            if middle:
                pg.runs(ROLE_COL, b, [middle])
        else:
            first, rest, used = [], [], 0.0
            for part in left:                      # keep what fits on the first line, the rest goes below
                pw = width(part[0], part[1], part[2])
                if not rest and used + pw <= room:
                    first.append(part); used += pw
                else:
                    rest.append(part)
            while first and first[-1][0].strip() in ("|", ""):
                rest.insert(0, first.pop())
            pg.runs(0, b, first or left[:1])
            below = [p for p in rest if p[0].strip() != "|"]
            if middle:
                below += ([(" · ", "R", L.small)] if below else []) + [middle]
            if below:
                pg.runs(0, body_line(), below)
        if date:
            pg.right(b, date, "B", L.body)

    def bullets(items: list[str]):
        for text in items:
            text = text.strip().rstrip(".")
            fits_squeezed = width(text, "R", L.body) <= BULLET_W * SQUEEZE
            parts = [text] if fits_squeezed else wrap(text, "R", L.body, BULLET_W)
            for j, ln in enumerate(parts):
                b = body_line()
                if j == 0:
                    pg.runs(BULLET_X, b, [("•", "R", L.body)])
                # Distributed alignment for full lines; the tail of a (flagged) wrapped bullet stays left aligned.
                pg.justified(BULLET_TEXT_X, b, ln, "R", L.body, BULLET_W, stretch=(j < len(parts) - 1 or len(parts) == 1))

    def hanging(col: float, label: list[tuple[str, str, float]], text: str, face: str, size: float):
        """A label, then text that wraps under itself at `col` (or after the label, if the label is wider)."""
        start = max(col, sum(width(t, f, s) for t, f, s in label) + 6)
        for j, ln in enumerate(wrap(text, face, size, R_TEXT_W - start)):
            b = body_line()
            if j == 0:
                pg.runs(0, b, label)
            pg.runs(start, b, [(ln, face, size)])

    from .pdf import summary_text, summary_title

    def section_summary():
        text = summary_text(data)
        if not text.strip():
            return
        title(summary_title(data))
        obj_lines = wrap(text, "R", L.body, R_TEXT_W)
        for j, ln in enumerate(obj_lines):
            last_short = j == len(obj_lines) - 1 and width(ln, "R", L.body) < 0.92 * R_TEXT_W
            pg.justified(0, body_line(), ln, "R", L.body, R_TEXT_W, stretch=not last_short)

    def section_education():
        eds = data.get("education") or []
        if not eds:
            return
        title(SECTION_TITLES["education"])
        for i, ed in enumerate(eds):
            entry_row([(ed["degree"], "B", L.body), (" | ", "B", L.body), (ed["institution"], "BI", L.body),
                       (" | ", "R", L.small), (ed.get("location") or "", "I", L.small)], None, ed.get("dates") or "")
            if ed.get("coursework"):
                hanging(COURSE_COLS[min(i, 1)], [("Coursework:", "I", L.small)], ", ".join(ed["coursework"]), "I", L.small)

    def section_skills():
        rows = [r for r in data.get("skills") or [] if (r.get("items") or "").strip()]
        if not rows:
            return
        title(SECTION_TITLES["skills"])
        for row in rows:
            hanging(SKILL_COL, [(row["label"].rstrip(":"), "B", L.body)], row["items"], "R", L.body)

    def section_experience():
        exps = data.get("experience") or []
        if not exps:
            return
        title(SECTION_TITLES["experience"])
        for ex in exps:
            entry_row([(ex["company"], "B", L.body), (" | ", "R", L.small), (ex.get("location") or "", "I", L.small)],
                      (ex["title"], "BI", L.body), ex.get("dates") or "")
            bullets(ex.get("bullets") or [])

    def section_projects():
        prs = data.get("projects") or []
        if not prs:
            return
        title(SECTION_TITLES["projects"])
        for pr in prs:
            entry_row([(pr["title"], "B", L.body), (" | ", "R", L.body), (pr.get("stack") or "", "I", L.body)], None,
                      pr.get("dates") or "")
            bullets(pr.get("bullets") or [])

    def section_leadership():
        items = [i for i in data.get("leadership") or [] if (i.get("text") or "").strip()]
        if not items:                              # nothing to show: no empty LEADERSHIP & ACHIEVEMENTS heading
            return
        title(SECTION_TITLES["leadership"])
        for item in items:
            label = item["label"].rstrip(":") + ": "
            indent = width(label, "B", L.body)
            date = (item.get("date") or "").strip()
            room = R_TEXT_W - indent - (width(date, "B", L.body) + 12 if date else 0)
            for j, ln in enumerate(wrap(item["text"], "R", L.body, room)):
                b = body_line()
                pg.runs(0 if j == 0 else indent, b, ([(label, "B", L.body)] if j == 0 else []) + [(ln, "R", L.body)])
                if j == 0 and date:
                    pg.right(b, date, "B", L.body)      # bold, flush right, like every other date

    sections = {"summary": section_summary, "education": section_education, "skills": section_skills,
                "experience": section_experience, "projects": section_projects, "leadership": section_leadership}
    for key in tpl["order"]:
        sections[key]()
    return y


def resume_fits(data: dict, L: Layout) -> tuple[bool, float]:
    """(fits on one A4 page, blank space left at the bottom in pt) using exact font metrics."""
    used = _resume_pass(None, data, L)
    gap = PAGE_H - R_BOTTOM - used
    return gap >= 0, gap


def build_resume(data: dict, path: str, L: Layout = DEFAULT_LAYOUT, title_meta: str = "") -> None:
    _fonts()
    c = canvas.Canvas(path, pagesize=A4, pageCompression=1)
    c.setTitle(title_meta)
    c.setAuthor(data["header"]["name"].title())
    c.setSubject("Resume")
    _resume_pass(c, data, L)
    c.showPage()
    c.save()


# ----------------------------------------------------------------------------- cover letter (X.6)

def _cover_pass(c, header: dict, letter: dict, body: float, spacing: float, after: float) -> float:
    side, top = 54.0, 54.0                 # 0.75" margins
    text_w = PAGE_W - 2 * side
    pg = _Page(c, top, side, text_w)
    y = top + 26
    name = header["name"].upper()
    pg.runs((text_w - width(name, "B", 14)) / 2, y - 7, [(name, "B", 14)])
    for line in (header["line2"], header["line3"]):
        y += 13.8
        parts = []
        for i, item in enumerate(line):
            if i:
                parts.append((" | ", "", ""))
            parts.append((item["text"], item.get("url") or "", ""))
        total = sum(width(t, "R", 11) for t, _, _ in parts)
        x = (text_w - total) / 2
        pg.runs(x, y - 3, [(t, "R", 11) for t, _, _ in parts])
        for t, url, _ in parts:
            w = width(t, "R", 11)
            pg.link(x, y - 3, w, 11, url)
            x += w
    lead = body * spacing

    def para(text: str, before: float = 0.0, after_pt: float = 0.0, face: str = "R"):
        nonlocal y
        y += before
        for ln in wrap(text, face, body, text_w) if text else [""]:
            y += lead
            pg.runs(0, y - lead * 0.25, [(ln, face, body)])
        y += after_pt

    para(letter["date"], before=14, after_pt=after)
    for i, ln in enumerate(letter["recipient_lines"]):
        para(ln, after_pt=after if i == len(letter["recipient_lines"]) - 1 else 0)
    para(letter["re_line"], after_pt=after, face="B")
    para(letter["greeting"], after_pt=after)
    for p in letter["paragraphs"]:
        para(p, after_pt=after)
    para(letter["closing"], before=2)
    para("")
    para(letter["signature"])
    return y


def cover_fits(header: dict, letter: dict, body: float, spacing: float, after: float) -> tuple[bool, float]:
    used = _cover_pass(None, header, letter, body, spacing, after)
    gap = PAGE_H - 54.0 - used
    return gap >= 0, gap


def build_cover(header: dict, letter: dict, path: str, body: float = 11.5, spacing: float = 1.2, after: float = 9, title_meta: str = "") -> None:
    _fonts()
    c = canvas.Canvas(path, pagesize=A4, pageCompression=1)
    c.setTitle(title_meta)
    c.setAuthor(header["name"].title())
    c.setSubject("Cover letter")
    _cover_pass(c, header, letter, body, spacing, after)
    c.showPage()
    c.save()
