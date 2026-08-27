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

    class _GeneratorModule(typ.Protocol):
        """Describe the generator API used by the local-policy contract."""

        def render_config(self, repository: Path) -> str:
            """Render the generated spelling configuration for `repository`."""


SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1]


@pytest.fixture(name="rollout_modules")
def rollout_modules_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[types.ModuleType, types.ModuleType, _GeneratorModule]:
    """Import the scripts through the top-level module path used at runtime.

    Returns
    -------
    tuple[types.ModuleType, types.ModuleType, _GeneratorModule]
        The ``cache``, ``rollout``, and ``generator`` modules, in that order.
    """
    monkeypatch.syspath_prepend(str(SCRIPT_DIRECTORY))
    names = ("typos_rollout_cache", "typos_rollout", "generate_typos_config")
    importlib.invalidate_caches()
    cache, rollout, generator_module = (importlib.import_module(name) for name in names)
    generator = typ.cast("_GeneratorModule", generator_module)
    return cache, rollout, generator


@pytest.fixture(name="refresh_module")
def refresh_module_fixture(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, _GeneratorModule],
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


@pytest.fixture(name="patch_https_opener")
def patch_https_opener_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> cabc.Callable[[cabc.Callable[..., object]], None]:
    """Return a helper routing HTTPS fetches through a supplied response factory.

    Returns
    -------
    cabc.Callable[[cabc.Callable[..., object]], None]
        Callable installing the stub opener for one response factory.
    """

    def patch(open_response: cabc.Callable[..., object]) -> None:
        _patch_https_opener(monkeypatch, open_response)

    return patch


@pytest.fixture(name="dictionary_text")
def dictionary_text_fixture() -> cabc.Callable[..., str]:
    """Return the shared minimal-dictionary document builder.

    Returns
    -------
    cabc.Callable[..., str]
        Callable returning a valid shared-dictionary document.
    """
    return _dictionary_text


@pytest.fixture(name="script_directory")
def script_directory_fixture() -> Path:
    """Return the directory holding the rollout scripts.

    Returns
    -------
    Path
        The ``scripts`` directory.
    """
    return SCRIPT_DIRECTORY


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

        def open(self, *args: object, **kwargs: object) -> object:  # noqa: PLR6301 - opener protocol requires an instance method
            """Delegate to the supplied response factory."""
            return open_response(*args, **kwargs)

    def build_opener(*handlers: object) -> _StubOpener:
        """Return the stub opener once the redirect policy is confirmed."""
        assert any(  # noqa: S101 - fixture validates HTTPS redirect policy
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
