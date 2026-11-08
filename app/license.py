"""Licensing / revocation gate for the AI features.

The online dashboard (Supabase) is the authority on whether an account may use the AI features — screening,
tailoring, and document generation. On each sync the app asks the server for its standing and caches the answer
here. A **revoked** account has its AI features switched off, even offline, because the last answer is kept.

Accounts that never connect to a dashboard have nothing to check, so they are never gated: a plain local install
keeps working. This is ordinary license enforcement on the operator's own service — the operator decides who is
revoked, in the open, the same rule for everyone. The app is open source, so the check can be removed by editing
the code; it is a gate the operator controls, not an unbreakable lock.
"""
from __future__ import annotations

import json

from .config import DATA_DIR
from . import db

CACHE = DATA_DIR / "license.json"


def _read() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def status() -> str:
    """'ok', 'revoked', or 'unknown' (not checked yet)."""
    s = _read().get("status")
    return s if s in ("ok", "revoked") else "unknown"


def revoked() -> bool:
    return status() == "revoked"


def reason() -> str:
    return _read().get("reason") or ""


def message() -> str:
    r = reason()
    return ("This copy of Job Autopilot has been deactivated by the site operator"
            + (f": {r}" if r else ".")
            + " Its online access is closed and the AI features are turned off. Contact the operator to restore access.")


def record(result: dict | None) -> None:
    """Store the server's answer ({'status': 'ok'|'revoked', 'reason': ...}). A None or unexpected value (offline
    or a transient error) keeps the last answer, so a revoked install stays revoked until the server says otherwise."""
    if not isinstance(result, dict) or result.get("status") not in ("ok", "revoked"):
        return
    try:
        CACHE.write_text(json.dumps({"status": result["status"], "reason": result.get("reason") or "",
                                     "at": db.now()}, indent=2), encoding="utf-8")
    except OSError:
        pass


def clear() -> None:
    """Forget any cached standing (used when the computer disconnects from the dashboard)."""
    try:
        CACHE.unlink()
    except OSError:
        pass
