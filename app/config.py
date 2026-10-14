"""Paths and settings. Everything tunable lives in .env, config/*.json, or profile/*."""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CONFIG_DIR = ROOT / "config"
# Both can be pointed elsewhere (e.g. a demo database: JOB_AUTOPILOT_DATA=demo/data JOB_AUTOPILOT_PROFILE=demo/profile).
PROFILE_DIR = Path(os.environ.get("JOB_AUTOPILOT_PROFILE") or ROOT / "profile").expanduser().resolve()
PROMPTS_DIR = ROOT / "prompts"
DATA_DIR = Path(os.environ.get("JOB_AUTOPILOT_DATA") or ROOT / "data").expanduser().resolve()
OUTPUT_DIR = DATA_DIR / "output"
DB_PATH = DATA_DIR / "autopilot.db"

# The candidate's master prompt. Read fresh on every Claude call so edits apply immediately.
SYSTEM_PROMPT_FILE = PROFILE_DIR / "system_prompt.md"
# Facts the candidate confirms from the dashboard (answers to "missing info" questions).
ANSWERS_FILE = PROFILE_DIR / "answers.md"
# Contact details and standard form answers used only by the autofill helper.
APPLICANT_FILE = PROFILE_DIR / "applicant.json"

for d in (DATA_DIR, OUTPUT_DIR, PROFILE_DIR):
    d.mkdir(parents=True, exist_ok=True)

# First run: start from the templates in profile/examples/ (your real profile files are gitignored).
for _ex in (ROOT / "profile" / "examples").glob("*"):
    if _ex.is_file() and not (PROFILE_DIR / _ex.name).exists():
        (PROFILE_DIR / _ex.name).write_bytes(_ex.read_bytes())


SETTINGS_FILE = CONFIG_DIR / "settings.json"
_settings_cache: dict = {"mtime": None, "data": {}}


def settings() -> dict:
    """Values saved from the dashboard's Settings page (config/settings.json); they override .env."""
    try:
        m = SETTINGS_FILE.stat().st_mtime
    except OSError:
        return {}
    if _settings_cache["mtime"] != m:
        try:
            _settings_cache["data"] = load_json(SETTINGS_FILE)
        except (OSError, ValueError):
            _settings_cache["data"] = {}
        _settings_cache["mtime"] = m
    return _settings_cache["data"]


def save_settings(updates: dict) -> None:
    data = {**settings(), **updates}
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def env(name: str, default: str = "") -> str:
    s = settings()
    if name in s and s[name] not in (None, ""):
        return str(s[name]).strip()
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def companies() -> list[dict]:
    """Your curated list plus boards discovered from sponsoring jobs in the feeds (Sources page)."""
    listed = [c for c in load_json(CONFIG_DIR / "companies.json")["companies"] if c.get("enabled", True)]
    seen = {f"{c['ats']}:{c['slug']}" for c in listed}
    try:
        from . import db
        for b in db.fetchall("SELECT * FROM discovered_boards WHERE enabled=1"):
            if b["key"] not in seen:
                # Directory boards are read titles-first (light); their sponsor status comes from your H-1B data.
                listed.append({"name": b["name"], "ats": b["ats"], "slug": b["slug"], "h1b": b["h1b"], "discovered": True,
                               "directory": b["source"] == "directory", "light": b["source"] == "directory"})
    except Exception:
        pass
    return listed


def feeds_config() -> dict:
    p = CONFIG_DIR / "feeds.json"
    return load_json(p) if p.exists() else {}


def search_config() -> dict:
    return load_json(CONFIG_DIR / "search.json")


def applicant() -> dict:
    return load_json(APPLICANT_FILE) if APPLICANT_FILE.exists() else {}
