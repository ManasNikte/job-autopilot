"""Import the USCIS H-1B Employer Data Hub export to back up the 'track record of sponsoring' claim with real numbers.

Download CSVs from https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub (one per fiscal year)
and import them on the Sources page or with `python -m app.cli import-h1b file1.csv file2.csv`.
Column names differ between years, so columns are detected by keyword.
"""
from __future__ import annotations

import csv
import io
import re

from . import db

SUFFIXES = r"\b(incorporated|inc|llc|l\.l\.c|corp|corporation|co|company|ltd|limited|lp|llp|plc|pbc|holdings|group|technologies|technology|labs|usa|us|na)\b"


def norm(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    s = re.sub(SUFFIXES, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _int(v) -> int:
    try:
        return int(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0


def import_csv(raw: bytes) -> int:
    text = raw.decode("utf-8-sig", errors="replace")
    sample = text[:5000]
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    cols = reader.fieldnames or []
    name_col = next((c for c in cols if "employer" in c.lower() or "petitioner name" in c.lower()), None)
    year_col = next((c for c in cols if "fiscal" in c.lower() or c.lower().strip() == "year"), None)
    appr = [c for c in cols if "approval" in c.lower()]
    deny = [c for c in cols if "denial" in c.lower()]
    if not name_col or not appr:
        raise ValueError(f"Unrecognized CSV columns: {cols[:12]}")
    agg: dict[str, list] = {}
    for row in reader:
        n = norm(row.get(name_col, ""))
        if not n:
            continue
        a = agg.setdefault(n, [row[name_col].strip(), 0, 0, set()])
        a[1] += sum(_int(row.get(c)) for c in appr)
        a[2] += sum(_int(row.get(c)) for c in deny)
        if year_col and row.get(year_col):
            a[3].add(str(row[year_col]).strip())
    for n, (name, ap, de, years) in agg.items():
        old = db.fetchone("SELECT approvals, denials, fiscal_years FROM h1b_employers WHERE name_norm=?", (n,))
        if old:
            yrs = set(filter(None, (old["fiscal_years"] or "").split(","))) | years
            # Re-importing the same year should not double count.
            if years and years <= set(filter(None, (old["fiscal_years"] or "").split(","))):
                continue
            ap, de = ap + old["approvals"], de + old["denials"]
            years = yrs
        db.execute("INSERT OR REPLACE INTO h1b_employers VALUES (?,?,?,?,?)", (n, name, ap, de, ",".join(sorted(years))))
    return len(agg)


def lookup(company: str) -> str | None:
    """'1,234 approvals / 12 denials (FY2023,FY2024)' summed over employer names that start with the company name."""
    n = norm(company)
    if not n:
        return None
    rows = db.fetchall("SELECT * FROM h1b_employers WHERE name_norm=? OR name_norm LIKE ?", (n, n + " %"))
    if not rows:
        return None
    ap = sum(r["approvals"] for r in rows)
    de = sum(r["denials"] for r in rows)
    yrs = sorted({y for r in rows for y in (r["fiscal_years"] or "").split(",") if y})
    return f"USCIS data hub: {ap:,} approvals / {de:,} denials" + (f" (FY {', '.join(yrs)})" if yrs else "")
