"""Freshness-policy tests for the spelling-configuration rollout scripts."""

from __future__ import annotations

import importlib
import typing as typ
from pathlib import Path

import pytest

if typ.TYPE_CHECKING:
    import types

SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1]


@pytest.fixture(name="rollout_modules")
def rollout_modules_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[types.ModuleType, types.ModuleType, types.ModuleType]:
    """Import the rollout modules through their runtime top-level path."""
    monkeypatch.syspath_prepend(str(SCRIPT_DIRECTORY))
    names = ("typos_rollout_cache", "typos_rollout", "generate_typos_config")
    importlib.invalidate_caches()
    cache, rollout, generator = (importlib.import_module(name) for name in names)
    return cache, rollout, generator


def test_remote_freshness_uses_dates_and_falls_back_on_invalid_values(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> None:
    """Last-Modified comparison remains conservative for malformed dates."""
    _, rollout, _ = rollout_modules

    assert rollout._remote_is_not_newer(
        {"last_modified": "Fri, 10 Jul 2026 08:00:00 GMT"},
        {"Last-Modified": "Fri, 10 Jul 2026 07:00:00 GMT"},
    )
    assert rollout._remote_is_not_newer(
        {"last_modified": "invalid"}, {"Last-Modified": "invalid"}
    )
    assert not rollout._remote_is_not_newer({}, {"Last-Modified": "invalid"})
