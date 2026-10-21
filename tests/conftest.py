"""Shared test setup."""
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_search_config() -> dict:
    """The default search settings the tests were written against. The user's own config/search.json (other role
    families, seniority, locations) must not change test results."""
    return json.loads((FIXTURES / "search.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _default_search_config(monkeypatch):
    import app.config as cfg
    import app.pipeline as pipeline
    monkeypatch.setattr(cfg, "search_config", fixture_search_config)
    monkeypatch.setattr(pipeline, "search_config", fixture_search_config)
