"""Claude access with two interchangeable backends, both returning schema-validated JSON.

- api:        Anthropic Python SDK (needs ANTHROPIC_API_KEY or an `ant auth login` profile).
              The large profile prompt is prompt-cached, so repeated jobs pay ~10% for it.
- claude_cli: headless Claude Code (`claude -p --json-schema ...`), which runs on whatever
              account you logged into with `claude` -> /login (e.g. a Claude Enterprise seat).
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


def find_claude_cli() -> str | None:
    if env("CLAUDE_CLI_PATH") and Path(env("CLAUDE_CLI_PATH")).exists():
        return env("CLAUDE_CLI_PATH")
    for c in (shutil.which("claude"), str(Path.home() / ".local/bin/claude"), str(Path.home() / ".claude/local/claude")):
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
        "`claude` once to /login with your Enterprise account."
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


class _NoLimit:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def structured(*, user: str, schema: dict, model: str, effort: str = "high", max_tokens: int = 32000,
               on_start=None, label: str = "", interactive: bool = False) -> dict:
    """One Claude call that must return JSON matching `schema`. `on_start` fires once a slot is free.

    interactive=True (form autofill while the candidate waits) skips the background-work slot limit.
    """
    system = system_prompt()
    b = backend()
    with (_NoLimit() if interactive else _semaphore()):
        if on_start:
            on_start()
        t0 = time.time()
        if b == "api":
            out = _via_api(system, user, schema, model, effort, max_tokens)
        elif b == "claude_cli":
            out = _via_cli(system, user, schema, model, effort)
        else:
            raise LLMError(f"Unknown LLM_BACKEND '{b}'")
        _log_line({"label": label, "backend": b, "model": model, "effort": effort, "secs": round(time.time() - t0, 1)})
        return out


def _via_api(system: str, user: str, schema: dict, model: str, effort: str, max_tokens: int) -> dict:
    import anthropic

    client = anthropic.Anthropic(max_retries=4, timeout=900)
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
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
        raise LLMError("Could not reach the Anthropic API (network).") from e

    if msg.stop_reason == "refusal":
        raise LLMError("Claude declined this request (refusal).")
    if msg.stop_reason == "max_tokens":
        raise LLMError("Output hit max_tokens before finishing; raise max_tokens.")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    _log_usage(model, getattr(msg, "usage", None))
    return json.loads(text)


def _via_cli(system: str, user: str, schema: dict, model: str, effort: str) -> dict:
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
        "--tools", "",                       # pure text-in / JSON-out, no file or shell access
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
            raise BackendUnavailable(f"Claude CLI is not logged in ({msg}). Run `claude` in a terminal and use /login with your Enterprise account.")
        if LIMIT_RE.search(msg):
            raise BackendUnavailable(f"Claude usage limit reached: {msg}", retry_at=_reset_time(msg))
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
