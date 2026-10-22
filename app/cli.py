"""Command line entry points.

  python -m app.cli serve                 # dashboard at http://127.0.0.1:5055 (+ background worker)
  python -m app.cli run                   # fetch -> screen -> tailor once (good for cron/launchd)
  python -m app.cli fetch                 # fetch + prefilter only (no Claude usage)
  python -m app.cli check                 # verify the Claude backend and PDF conversion work
  python -m app.cli import-h1b a.csv ...  # load USCIS H-1B Employer Data Hub exports
"""
from __future__ import annotations

import sys

from . import db


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "serve"
    db.init()
    if cmd == "serve":
        from .web import serve
        serve()
    elif cmd == "run":
        from .pipeline import run
        lock = db.DB_PATH.parent / "headless.lock"         # tells a dashboard that starts meanwhile to leave these jobs alone
        lock.write_text(str(__import__("os").getpid()), encoding="utf-8")
        try:
            run()
        finally:
            lock.unlink(missing_ok=True)
    elif cmd == "fetch":
        from .pipeline import fetch_all
        fetch_all()
    elif cmd == "process":
        from .pipeline import run
        run(fetch=False)
    elif cmd == "cloud-poll":
        cloud_poll()
    elif cmd == "check":
        check()
    elif cmd == "preview-templates":
        preview_templates(argv[1] if len(argv) > 1 else None)
    elif cmd == "import-h1b":
        from .h1b import import_csv
        for p in argv[1:]:
            with open(p, "rb") as f:
                print(f"{p}: {import_csv(f.read()):,} employers")
    else:
        print(__doc__)


def preview_templates(source: str | None) -> None:
    """Build one PDF per resume template from a tailored-resume JSON (default: your newest tailored resume, else the
    built-in sample) into data/previews/, so you can compare them side by side."""
    import json
    from pathlib import Path
    from .config import DATA_DIR, ROOT
    from .render import pdfgen
    if source:
        data, label = json.loads(Path(source).read_text(encoding="utf-8")), source
    else:
        from . import db
        db.init()
        row = db.fetchone("SELECT tailor_json FROM jobs WHERE tailor_json IS NOT NULL ORDER BY updated_at DESC LIMIT 1")
        data = json.loads(row["tailor_json"]) if row else json.loads((ROOT / "tests/fixtures/sample_tailor.json").read_text(encoding="utf-8"))
        label = "your newest tailored resume" if row else "the built-in sample"
    out = DATA_DIR / "previews"
    out.mkdir(parents=True, exist_ok=True)
    print(f"Previews from {label}:")
    for key, t in pdfgen.TEMPLATES.items():
        d = {**data, "_template": key}
        fits = [L for L in pdfgen.LAYOUTS if pdfgen.resume_fits(d, L)[0]]
        path = out / f"resume-{key}.pdf"
        pdfgen.build_resume(d, str(path), fits[-1] if fits else pdfgen.LAYOUTS[0])
        print(f"  {t['label']:17} {path}  {'fits one page' if fits else 'OVERFLOWS one page'}  — {t['for']}")


def cloud_poll() -> None:
    """Run by the wake-up agent: keep the Mac awake, ask the dashboard to sync with the online dashboard, and if a
    request from your phone started work, stay awake until it finishes (up to 3 hours)."""
    import json as _json
    import os
    import subprocess
    import time
    import urllib.request
    if sys.platform == "darwin":
        subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])   # awake for as long as this runs
    from .config import env
    base = f"http://127.0.0.1:{env('PORT', '5055') or 5055}"
    try:
        req = urllib.request.Request(base + "/cloud/poll", method="POST", data=b"")
        res = _json.loads(urllib.request.urlopen(req, timeout=120).read() or b"{}")
    except Exception as e:
        print(f"cloud-poll: dashboard not reachable ({e})")
        return
    if not res.get("queued"):
        return
    print(f"cloud-poll: {res['queued']} request(s) from the online dashboard started; staying awake until done")
    deadline = time.time() + 3 * 3600
    time.sleep(20)
    while time.time() < deadline:
        try:
            st = _json.loads(urllib.request.urlopen(base + "/activity", timeout=30).read())["state"]
            if not st.get("active") and not st.get("queued"):
                break
        except Exception:
            break
        time.sleep(30)


def check() -> None:
    from . import llm
    from .schemas import obj, S

    try:
        b = llm.backend()
        print(f"Claude backend: {b}" + (f" ({llm.find_claude_cli()})" if b == "claude_cli" else ""))
        prompt_len = len(llm.system_prompt())
        print(f"System prompt loaded: {prompt_len:,} characters")
        from .config import env
        res = llm.structured(user="CALL TYPE: PING. Reply with the candidate's full name from Section I.",
                             schema=obj(name=S), model=env("MODEL_SCREEN", "claude-opus-5-5"), effort="low", max_tokens=2000)
        print(f"Claude replied: {res}")
    except Exception as e:
        print(f"✗ Claude check failed: {e}")

    import json
    import tempfile
    from pathlib import Path
    from .config import OUTPUT_DIR, ROOT
    from .render import pdfgen
    try:
        data = json.loads((ROOT / "tests/fixtures/sample_tailor.json").read_text(encoding="utf-8"))
        out = Path(tempfile.mkdtemp(dir=OUTPUT_DIR)) / "check.pdf"
        pdfgen.build_resume(data, str(out))
        print(f"PDF generation OK: {out}")
    except Exception as e:
        print(f"✗ PDF generation failed: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
