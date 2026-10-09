"""Shared fixtures for capture tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from evalshift.config import reset_config

CaptureReader = Callable[[str], list[dict[str, Any]]]


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset the process-wide capture config around every test (global state must not leak).

    The hygiene knobs now default from env vars, so clear them first: the test-suite baseline must
    be the built-in defaults (dedup on, max_captures=200) regardless of the developer's shell env.
    Tests that exercise a specific env value ``monkeypatch.setenv(...)`` then ``reset_config()``.
    """
    for var in (
        "EVALSHIFT_MAX_CAPTURES",
        "EVALSHIFT_CAPTURE_TTL",
        "EVALSHIFT_DEDUP",
        "EVALSHIFT_SAMPLE_RATE",
        "EVALSHIFT_SINK",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_config()
    yield
    reset_config()


@pytest.fixture
def capturing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Enable the capture gate and route writes into ``tmp_path``. Returns the capture base dir."""
    monkeypatch.setenv("EVALSHIFT_CAPTURE", "1")
    monkeypatch.setenv("EVALSHIFT_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def read_captures(tmp_path: Path) -> CaptureReader:
    """Return a reader: ``read_captures(suite)`` -> the parsed capture dicts for that suite."""

    def _read(suite: str) -> list[dict[str, Any]]:
        suite_dir = tmp_path / "captures" / suite
        if not suite_dir.exists():
            return []
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(suite_dir.glob("*.json"))]

    return _read
