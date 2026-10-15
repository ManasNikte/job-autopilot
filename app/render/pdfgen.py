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

FONT_DIRS = ["/System/Library/Fonts/Supplemental", "/Library/Fonts", "/usr/share/fonts/truetype/msttcorefonts",
             "/usr/share/fonts/truetype/liberation"]
FACES = {
    "R": ["Times New Roman.ttf", "LiberationSerif-Regular.ttf"],
    "B": ["Times New Roman Bold.ttf", "LiberationSerif-Bold.ttf"],
    "I": ["Times New Roman Italic.ttf", "LiberationSerif-Italic.ttf"],
    "BI": ["Times New Roman Bold Italic.ttf", "LiberationSerif-BoldItalic.ttf"],
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


LAYOUTS = [Layout(10.5, 13.2, 10), Layout(10.5, 13.5, 12), Layout(10.5, 14.0, 13), Layout(10.5, 14.5, 14)]
DEFAULT_LAYOUT = LAYOUTS[1]


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
        if stretch and gaps and natural < w:
            t.setWordSpace((w - natural) / gaps)
        t.textOut(text)
        t.setWordSpace(0)
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


def _resume_pass(c, data: dict, L: Layout) -> float:
    """Draw (or measure when c is None). Returns the y of the last baseline + descent."""
    pg = _Page(c, R_TOP, R_SIDE, R_TEXT_W)
    y = R_TOP
    h = data["header"]

    # Header (IX-V.3): name line exactly 26pt, contact lines 13.8pt, centered.
    y += 26
    name = h["name"].upper()
    nx = (R_TEXT_W - width(name, "B", 14)) / 2
    pg.runs(nx, y - 7, [(name, "B", 14)])
    for line in (h["line2"], h["line3"]):
        y += 13.8
        parts = []
        for i, item in enumerate(line):
            if i:
                parts.append((" | ", "R", 11, ""))
            parts.append((item["text"], "R", 11, item.get("url") or ""))
        total = sum(width(t, f, s) for t, f, s, _ in parts)
        x = (R_TEXT_W - total) / 2
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
        page_break_if_needed(L.before_title + 13.8 + L.line)
        y += L.before_title + 13.8
        base = y - 3.5
        pg.runs(0, base, [(text.upper(), "B", 12)])
        if c:
            c.setLineWidth(0.75)
            c.line(R_SIDE, PAGE_H - (base + 3.2), R_SIDE + R_TEXT_W, PAGE_H - (base + 3.2))
        y += 3

    def body_line(extra_before: float = 0) -> float:
        nonlocal y
        page_break_if_needed(extra_before + L.line)
        y += extra_before + L.line
        return y - L.line * 0.27

    # Summary (title from the package, e.g. "SUMMARY"): distributed, both lines flush (IX-V.4)
    from .pdf import summary_text, summary_title
    title(summary_title(data))
    obj_lines = wrap(summary_text(data), "R", L.body, R_TEXT_W)
    for j, ln in enumerate(obj_lines):
        last_short = j == len(obj_lines) - 1 and width(ln, "R", L.body) < 0.92 * R_TEXT_W
        pg.justified(0, body_line(), ln, "R", L.body, R_TEXT_W, stretch=not last_short)

    # Education
    title("Education")
    for i, ed in enumerate(data["education"]):
        b = body_line(3)
        pg.runs(0, b, [(ed["degree"], "B", L.body), (" | ", "B", L.body), (ed["institution"], "BI", L.body),
                       (" | ", "R", L.small), (ed["location"], "I", L.small)])
        pg.right(b, ed["dates"], "B", L.body)
        if ed.get("coursework"):
            b = body_line()
            pg.runs(0, b, [("Coursework:", "I", L.small)])
            pg.runs(COURSE_COLS[min(i, 1)], b, [(", ".join(ed["coursework"]), "I", L.small)])

    # Technical skills (4 rows)
    title("Technical Skills")
    for row in data["skills"]:
        b = body_line()
        pg.runs(0, b, [(row["label"].rstrip(":"), "B", L.body)])
        pg.runs(SKILL_COL, b, [(row["items"], "R", L.body)])

    def bullets(items: list[str]):
        for text in items:
            text = text.strip().rstrip(".")
            parts = wrap(text, "R", L.body, BULLET_W)
            for j, ln in enumerate(parts):
                b = body_line()
                if j == 0:
                    pg.runs(BULLET_X, b, [("•", "R", L.body)])
                # Distributed alignment for full lines; the tail of a (flagged) wrapped bullet stays left aligned.
                pg.justified(BULLET_TEXT_X, b, ln, "R", L.body, BULLET_W, stretch=(j < len(parts) - 1 or len(parts) == 1))

    # Professional experience
    title("Professional Experience")
    for ex in data["experience"]:
        b = body_line(3)
        pg.runs(0, b, [(ex["company"], "B", L.body), (" | ", "R", L.small), (ex["location"], "I", L.small)])
        pg.runs(ROLE_COL, b, [(ex["title"], "BI", L.body)])
        pg.right(b, ex["dates"], "B", L.body)
        bullets(ex["bullets"])

    # Projects
    title("Projects")
    for pr in data["projects"]:
        b = body_line(3)
        pg.runs(0, b, [(pr["title"], "B", L.body), (" | ", "R", L.body), (pr["stack"], "I", L.body)])
        pg.right(b, pr["dates"], "B", L.body)
        bullets(pr["bullets"])

    # Leadership & achievements
    title("Leadership & Achievements")
    for item in data["leadership"]:
        label = item["label"].rstrip(":") + ": "
        indent = width(label, "B", L.body)
        date = (item.get("date") or "").strip()
        room = R_TEXT_W - indent - (width(date, "B", L.body) + 12 if date else 0)
        for j, ln in enumerate(wrap(item["text"], "R", L.body, room)):
            b = body_line()
            pg.runs(0 if j == 0 else indent, b, ([(label, "B", L.body)] if j == 0 else []) + [(ln, "R", L.body)])
            if j == 0 and date:
                pg.right(b, date, "B", L.body)          # bold, flush right, like every other date
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
