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
        run()
    elif cmd == "fetch":
        from .pipeline import fetch_all
        fetch_all()
    elif cmd == "process":
        from .pipeline import run
        run(fetch=False)
    elif cmd == "check":
        check()
    elif cmd == "import-h1b":
        from .h1b import import_csv
        for p in argv[1:]:
            with open(p, "rb") as f:
                print(f"{p}: {import_csv(f.read()):,} employers")
    else:
        print(__doc__)


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
        data = json.loads((ROOT / "tests/fixtures/sample_tailor.json").read_text())
        out = Path(tempfile.mkdtemp(dir=OUTPUT_DIR)) / "check.pdf"
        pdfgen.build_resume(data, str(out))
        print(f"PDF generation OK: {out}")
    except Exception as e:
        print(f"✗ PDF generation failed: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
