"""Claude access with two interchangeable backends, both returning schema-validated JSON.

- api:        Anthropic Python SDK (needs ANTHROPIC_API_KEY or an `ant auth login` profile).
              The large profile prompt is prompt-cached, so repeated jobs pay ~10% for it.
- claude_cli: headless Claude Code (`claude -p --json-schema ...`), which runs on whatever
              account you logged into with `claude` -> /login (a Claude Pro, Max, Team, or Enterprise seat).
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .config import ANSWERS_FILE, DATA_DIR, PROMPTS_DIR, SYSTEM_PROMPT_FILE, env


class LLMError(RuntimeError):
    pass


class BackendUnavailable(LLMError):
    """Login/key/installation problem or a usage limit: stop the whole run instead of failing every job.

    retry_at (epoch seconds) is set for usage limits, so background work can pause and resume on its own.
    """

    def __init__(self, msg: str, retry_at: float | None = None):
        super().__init__(msg)
        self.retry_at = retry_at


NETWORK_RE = re.compile(r"can't reach the api|ENOTFOUND|ECONNREFUSED|ECONNRESET|ETIMEDOUT|EAI_AGAIN|network (is )?unreachable|"
                        r"getaddrinfo|socket hang up|fetch failed|connection (error|reset|refused)", re.I)
LIMIT_RE = re.compile(r"session limit|usage limit|hit your limit|rate limit|limit reached|out of (extra )?usage|quota", re.I)


def _reset_time(msg: str) -> float:
    """'resets 6am (America/New_York)' -> epoch of the next 6:00 AM local, plus a 2 minute buffer; else 30 min."""
    import datetime as dt
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", msg, re.I)
    now = dt.datetime.now()
    if not m:
        return time.time() + 30 * 60
    hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
    t = now.replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
    if t <= now:
        t += dt.timedelta(days=1)
    return t.timestamp() + 120


def system_prompt() -> str:
    """Candidate master prompt (from file, never hard-coded) + confirmed answers + app adapter.

    Order matters for caching: the big, stable profile prompt goes first.
    """
    if not SYSTEM_PROMPT_FILE.exists():
        raise LLMError(f"Missing {SYSTEM_PROMPT_FILE}. Put your application system prompt there.")
    parts = [SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")]
    if ANSWERS_FILE.exists() and ANSWERS_FILE.read_text(encoding="utf-8").strip():
        parts.append(
            "=" * 80 + "\nCANDIDATE-CONFIRMED FACTS (added from the dashboard; treat as part of Section I and "
            "as confirmed by the candidate)\n" + ANSWERS_FILE.read_text(encoding="utf-8")
        )
    parts.append((PROMPTS_DIR / "app_adapter.md").read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def prompt_version() -> str:
    """When the master prompt was last saved (local time), shown next to the documents it produced."""
    from datetime import datetime
    return datetime.fromtimestamp(SYSTEM_PROMPT_FILE.stat().st_mtime).isoformat(timespec="seconds") if SYSTEM_PROMPT_FILE.exists() else ""


def find_claude_cli() -> str | None:
    if env("CLAUDE_CLI_PATH") and Path(env("CLAUDE_CLI_PATH")).exists():
        return env("CLAUDE_CLI_PATH")
    for c in (shutil.which("claude"), str(Path.home() / ".local/bin/claude"), str(Path.home() / ".local/bin/claude.exe"),
              str(Path.home() / ".claude/local/claude")):
        if c and Path(c).exists():
            return c
    # Binary bundled with the Claude desktop app (newest version first).
    bundled = sorted(glob.glob(str(Path.home() / "Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude")))
    return bundled[-1] if bundled else None


def backend() -> str:
    b = env("LLM_BACKEND", "auto").lower()
    if b != "auto":
        return b
    if env("ANTHROPIC_API_KEY"):
        return "api"
    if find_claude_cli():
        return "claude_cli"
    raise BackendUnavailable(
        "No Claude backend available. Either set ANTHROPIC_API_KEY in .env, or install Claude Code and run "
        "`claude` once to /login with your Claude account (Pro, Max, Team, or Enterprise)."
    )


_slots: threading.BoundedSemaphore | None = None
_slots_lock = threading.Lock()


def _semaphore() -> threading.BoundedSemaphore:
    """Caps simultaneous Claude calls across all background threads (seat/API rate limits)."""
    global _slots
    with _slots_lock:
        if _slots is None:
            from .config import env_int
            _slots = threading.BoundedSemaphore(max(1, env_int("PARALLEL_JOBS", 3)))
        return _slots


# ----------------------------------------------------------------------------- plan usage (Claude Code seat)

USAGE: dict = {"limits": None, "checked": 0.0, "error": ""}   # last reading, shown on the Background panel
_usage_lock = threading.Lock()
USAGE_TTL = 90                                                  # seconds between readings (each takes a few seconds)


def read_usage(cli: str | None = None) -> list[dict] | None:
    """The seat's plan limits from Claude Code's own `/usage` report (a local command: no tokens used).
    Returns [{'kind': 'session', 'percent': 66, 'resets_at': '…', 'scope': None, 'is_active': True}, …], or None
    when unavailable (API-key backend, older CLI, not a subscription login)."""
    cli = cli or find_claude_cli()
    if not cli:
        return None
    envv = dict(os.environ)
    envv.pop("ANTHROPIC_API_KEY", None)
    try:
        p = subprocess.run([cli, "-p", "/usage", "--output-format", "stream-json", "--verbose", "--tools", ""],
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, env=envv,
                           cwd=str(DATA_DIR))
    except (subprocess.TimeoutExpired, OSError):
        return None
    for line in p.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        rl = (d.get("usage_report") or {}).get("rate_limits") if isinstance(d, dict) else None
        if rl and isinstance(rl.get("limits"), list):
            return rl["limits"]
    return None


def usage_now(force: bool = False) -> dict:
    """Cached reading: {'limits': [...] | None, 'checked': epoch, 'worst': {...} | None}. Never raises."""
    with _usage_lock:
        if force or time.time() - USAGE["checked"] > USAGE_TTL:
            try:
                limits = read_usage() if backend() == "claude_cli" else None
                USAGE.update(limits=limits, error="")
            except Exception as e:
                USAGE.update(limits=None, error=str(e)[:200])
            USAGE["checked"] = time.time()
        overall = [l for l in (USAGE["limits"] or []) if not l.get("scope") and isinstance(l.get("percent"), (int, float))]
        USAGE["worst"] = max(overall, key=lambda l: l["percent"]) if overall else None
        return USAGE


def _iso_epoch(iso: str | None) -> float | None:
    from datetime import datetime as _dt
    try:
        return _dt.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None
    except ValueError:
        return None


def usage_guard() -> None:
    """Background work stops at USAGE_PAUSE_AT percent (default 95) of the session or weekly limit, and waits for
    that limit to reset, so the seat always has room left for you. Raises BackendUnavailable(retry_at=…)."""
    from .config import env_int
    at = env_int("USAGE_PAUSE_AT", 95)
    if at >= 100:
        return
    w = usage_now().get("worst")
    if w and w["percent"] >= at:
        names = {"session": "5-hour session", "weekly_all": "weekly", "weekly": "weekly"}
        reset = _iso_epoch(w.get("resets_at")) or time.time() + 1800
        raise BackendUnavailable(f"Claude {names.get(w.get('kind'), w.get('kind') or 'usage')} limit at {w['percent']}% "
                                 f"(pausing at {at}%)", retry_at=reset + 120)


class _NoLimit:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def structured(*, user: str, schema: dict, model: str, effort: str = "high", max_tokens: int = 32000,
               on_start=None, label: str = "", interactive: bool = False, system: str | None = None,
               web_search: bool = False) -> dict:
    """One Claude call that must return JSON matching `schema`. `on_start` fires once a slot is free.

    interactive=True (form autofill while the candidate waits) skips the background-work slot limit.
    system: a short task-specific system prompt instead of the candidate's master prompt.
    web_search: let Claude search the public web (used only for the sponsor lookup).
    """
    system = system or system_prompt()
    b = backend()
    if not interactive and b == "claude_cli":
        usage_guard()                       # near the plan limit: pause background work until it resets
    with (_NoLimit() if interactive else _semaphore()):
        if on_start:
            on_start()
        t0 = time.time()
        if b == "api":
            out = _via_api(system, user, schema, model, effort, max_tokens, web_search)
        elif b == "claude_cli":
            out = _via_cli(system, user, schema, model, effort, web_search)
        else:
            raise LLMError(f"Unknown LLM_BACKEND '{b}'")
        _log_line({"label": label, "backend": b, "model": model, "effort": effort, "secs": round(time.time() - t0, 1)})
        return out


def _via_api(system: str, user: str, schema: dict, model: str, effort: str, max_tokens: int, web_search: bool = False) -> dict:
    import anthropic

    client = anthropic.Anthropic(max_retries=4, timeout=900)
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    if web_search:
        kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}]
    try:
        if env("CLAUDE_FALLBACKS", "on") == "on" and model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-1"):
            # If a safety classifier declines, the API re-runs the request on a fallback model.
            with client.beta.messages.stream(**kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default") as s:
                msg = s.get_final_message()
        else:
            with client.messages.stream(**kwargs) as s:
                msg = s.get_final_message()
    except anthropic.AuthenticationError as e:
        raise BackendUnavailable(f"Anthropic API key rejected: {e.message}") from e
    except anthropic.RateLimitError as e:
        raise BackendUnavailable("Rate limited by the Anthropic API; pausing and retrying shortly.",
                                 retry_at=time.time() + int(e.response.headers.get("retry-after", "120"))) from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise BackendUnavailable("No internet connection to the Anthropic API; retrying in 5 minutes",
                                 retry_at=time.time() + 300) from e

    if msg.stop_reason == "refusal":
        raise LLMError("Claude declined this request (refusal).")
    if msg.stop_reason == "max_tokens":
        raise LLMError("Output hit max_tokens before finishing; raise max_tokens.")
    text = next((b.text for b in reversed(msg.content) if b.type == "text"), "")
    _log_usage(model, getattr(msg, "usage", None))
    return json.loads(text)


def _via_cli(system: str, user: str, schema: dict, model: str, effort: str, web_search: bool = False) -> dict:
    cli = find_claude_cli()
    if not cli:
        raise BackendUnavailable("Claude Code CLI not found. Install it or set CLAUDE_CLI_PATH.")
    # The system prompt is ~180KB: pass it as a file, keyed by content hash so it is written once.
    cache = DATA_DIR / "cache"
    cache.mkdir(exist_ok=True)
    sp = cache / f"system_{hashlib.sha256(system.encode()).hexdigest()[:16]}.md"
    if not sp.exists():
        sp.write_text(system, encoding="utf-8")
    cmd = [
        cli, "-p",
        "--output-format", "json",
        "--model", model,
        # Pure text-in / JSON-out, no file or shell access. The sponsor lookup may also search the web.
        *(["--tools", "WebSearch", "--allowedTools", "WebSearch"] if web_search else ["--tools", ""]),
        "--system-prompt-file", str(sp),
        "--json-schema", json.dumps(schema),
        "--effort", effort,
    ]
    envv = dict(os.environ)
    envv.pop("ANTHROPIC_API_KEY", None)      # make sure the CLI uses the logged-in seat, not a key
    try:
        p = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=1800, cwd=cache, env=envv)
    except subprocess.TimeoutExpired as e:
        raise LLMError("Claude CLI timed out after 30 minutes.") from e
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise LLMError(f"Claude CLI returned non-JSON (exit {p.returncode}): {p.stdout[:300]} {p.stderr[:300]}") from e
    u = out.get("usage") or {}
    _log_line({"cli_duration_s": round((out.get("duration_ms") or 0) / 1000, 1), "output_tokens": u.get("output_tokens"),
               "input_tokens": u.get("input_tokens"), "cache_read": u.get("cache_read_input_tokens"),
               "cache_write": u.get("cache_creation_input_tokens"), "turns": out.get("num_turns")})
    if out.get("is_error"):
        msg = str(out.get("result", "unknown error"))
        low = msg.lower()
        if "login" in low or "logged in" in low:
            raise BackendUnavailable(f"Claude CLI is not logged in ({msg}). Run `claude` in a terminal and use /login with your Claude account.")
        if LIMIT_RE.search(msg):
            raise BackendUnavailable(f"Claude usage limit reached: {msg}", retry_at=_reset_time(msg))
        if NETWORK_RE.search(msg):           # Wi-Fi dropped or DNS failed: wait and retry, don't fail the job
            raise BackendUnavailable(f"No internet connection ({msg[:120]}); retrying in 5 minutes", retry_at=time.time() + 300)
        raise LLMError(f"Claude CLI error: {msg}")
    if isinstance(out.get("structured_output"), dict):
        return out["structured_output"]
    res = out.get("result", "")
    try:
        return json.loads(res)
    except (json.JSONDecodeError, TypeError):
        start, end = res.find("{"), res.rfind("}")
        if start >= 0 and end > start:
            return json.loads(res[start: end + 1])
        raise LLMError(f"Claude CLI result was not JSON: {res[:300]}")


def _log_line(d: dict) -> None:
    with open(DATA_DIR / "usage.jsonl", "a") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **d}) + "\n")


def _log_usage(model: str, usage) -> None:
    if not usage:
        return
    line = {
        "model": model,
        "input": getattr(usage, "input_tokens", 0),
        "cache_read": getattr(usage, "cache_read_input_tokens", 0),
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0),
        "output": getattr(usage, "output_tokens", 0),
    }
    with open(DATA_DIR / "usage.jsonl", "a") as f:
        f.write(json.dumps(line) + "\n")
