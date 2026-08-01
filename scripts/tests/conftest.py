"""Shared fixtures for the spelling-policy script tests.

The rollout tests are split into a dictionary/render module and a
refresh/redirect-policy module; the import scaffolding both need lives here so
neither module imports the other.
"""

from __future__ import annotations

import importlib
import typing as typ
import urllib.request
from pathlib import Path

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types

SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1]


@pytest.fixture(name="rollout_modules")
def rollout_modules_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[types.ModuleType, types.ModuleType, types.ModuleType]:
    """Import the scripts through the same top-level module path used at runtime."""
    monkeypatch.syspath_prepend(str(SCRIPT_DIRECTORY))
    names = ("typos_rollout_cache", "typos_rollout", "generate_typos_config")
    importlib.invalidate_caches()
    cache, rollout, generator = (importlib.import_module(name) for name in names)
    return cache, rollout, generator


@pytest.fixture(name="refresh_module")
def refresh_module_fixture(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> types.ModuleType:
    """Import the cache-refresh module through the runtime module path.

    Returns
    -------
    types.ModuleType
        The ``typos_rollout_refresh`` module.
    """
    _ = rollout_modules  # Ensures the script directory is on ``sys.path``.
    module = importlib.import_module("typos_rollout_refresh")
    # The module is imported once per session, so its process-wide degradation
    # counters persist across tests unless each test starts from a known zero.
    module.reset_degradations()
    return module


def _patch_https_opener(
    monkeypatch: pytest.MonkeyPatch,
    open_response: cabc.Callable[..., object],
) -> None:
    """Route HTTPS fetches through *open_response*, asserting the redirect policy.

    The refresh module builds its own opener so the HTTPS-only redirect handler
    applies, so tests patch ``build_opener`` rather than ``urlopen``.
    """
    refresh = importlib.import_module("typos_rollout_refresh")

    class _StubOpener:
        """Minimal stand-in for ``urllib.request.OpenerDirector``."""

        def open(self, *args: object, **kwargs: object) -> object:
            """Delegate to the supplied response factory."""
            return open_response(*args, **kwargs)

    def build_opener(*handlers: object) -> _StubOpener:
        """Return the stub opener once the redirect policy is confirmed."""
        assert any(
            isinstance(handler, refresh._HttpsOnlyRedirectHandler)
            for handler in handlers
        ), "HTTPS refresh must install the HTTPS-only redirect handler"
        return _StubOpener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)


def _dictionary_text(stem: str = "organ") -> str:
    """Return a minimal valid shared-dictionary document."""
    return (
        'schema = 1\n\n[oxford]\nstems = ["'
        + stem
        + '"]\n\n[words]\naccepted = []\n\n[words.corrections]\n\n'
        + "[patterns]\nignore = []\n\n[files]\nexclude = []\n"
    )
