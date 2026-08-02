"""The CI guard that turns a silently-skipped extension into a failure.

`CUPRUM_REQUIRE_RUST_EXTENSION` exists because the extension-gated modules skip
when the native extension is absent, so a CI job that never built it reports a
green run indistinguishable from one that exercised the whole Python/Rust
boundary. A guard whose own failure path went untested would reintroduce
exactly that problem, so these tests cover the decision directly and then prove
the `pytest_configure` hook acts on it.
"""

from __future__ import annotations

import dataclasses
import pathlib
import typing as typ

import pytest

from cuprum import _rust_backend
from tests.helpers.extension_requirement import (
    REQUIRE_EXTENSION_ENV,
    missing_extension_message,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from types import ModuleType


@pytest.mark.parametrize(
    ("required", "available"),
    [
        pytest.param(False, False, id="not-required-and-absent"),
        pytest.param(False, True, id="not-required-and-present"),
        pytest.param(True, True, id="required-and-present"),
    ],
)
def test_the_run_proceeds_unless_a_required_extension_is_absent(
    *,
    required: bool,
    available: bool,
) -> None:
    """Only the required-but-absent combination is a failure.

    The guard must stay silent locally, where contributors routinely run the
    suite without building the extension.
    """
    assert missing_extension_message(required=required, available=available) is None, (
        f"required={required}, available={available} must not fail the run"
    )


def test_a_required_but_absent_extension_reports_how_to_fix_it() -> None:
    """The one failing combination names the variable and the remedy.

    A bare "extension missing" would leave a CI reader guessing whether the
    build step or the variable was at fault.
    """
    message = missing_extension_message(required=True, available=False)

    assert message is not None, "a required, unavailable extension must fail the run"
    assert REQUIRE_EXTENSION_ENV in message, (
        f"the message must name the variable that caused the failure: {message!r}"
    )
    assert "make develop" in message, (
        f"the message must name the command that builds the extension: {message!r}"
    )


def test_the_message_does_not_claim_the_import_failed() -> None:
    """`is_available` covers more than an `ImportError`.

    The extension can import and still report itself unusable, so wording that
    asserts an import failure would misdescribe that case.
    """
    message = missing_extension_message(required=True, available=False)

    assert message is not None, (
        "a required, unavailable extension must produce a message to inspect"
    )
    assert "could not be imported" not in message, (
        f"the message must not attribute the failure to an import: {message!r}"
    )
    assert "unavailable" in message, (
        f"the message must describe the extension as unavailable: {message!r}"
    )


@pytest.fixture(name="root_conftest")
def fixture_root_conftest(pytestconfig: pytest.Config) -> ModuleType:
    """Return the repository-root ``conftest`` module that applies the guard.

    A plain ``import conftest`` reaches ``cuprum/unittests/conftest.py``, which
    shadows the root one, so the module is located through the plugin manager
    that already loaded it.
    """
    root = pathlib.Path(str(pytestconfig.rootpath)) / "conftest.py"
    for plugin in pytestconfig.pluginmanager.get_plugins():
        module_file = getattr(plugin, "__file__", None)
        if module_file and pathlib.Path(module_file) == root:
            return typ.cast("ModuleType", plugin)
    pytest.fail(f"the root conftest at {root} is not a registered plugin")


@pytest.fixture(name="declare_extension")
def fixture_declare_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> cabc.Callable[..., None]:
    """Return a helper that fixes what the backend reports about itself."""

    def declare(*, available: bool) -> None:
        """Make the backend report `available` for the rest of the test."""
        monkeypatch.setattr(_rust_backend, "is_available", lambda: available)

    return declare


def test_the_hook_aborts_the_session_when_the_extension_is_required(
    monkeypatch: pytest.MonkeyPatch,
    declare_extension: cabc.Callable[..., None],
    root_conftest: ModuleType,
) -> None:
    """`pytest_configure` raises, rather than merely computing a message.

    `UsageError` is the failure that stops the session before collection, so
    the guard cannot be reduced to a warning nobody reads.
    """
    monkeypatch.setenv(REQUIRE_EXTENSION_ENV, "1")
    declare_extension(available=False)

    with pytest.raises(pytest.UsageError, match=REQUIRE_EXTENSION_ENV):
        root_conftest.pytest_configure(typ.cast("pytest.Config", None))


@dataclasses.dataclass(frozen=True, slots=True)
class _SilentCase:
    """A variable/availability pair the guard must not act on.

    Attributes
    ----------
    env_value : str or None
        The value of ``CUPRUM_REQUIRE_RUST_EXTENSION``; ``None`` leaves it
        unset.
    available : bool
        What the backend reports about the native extension.
    """

    env_value: str | None
    available: bool


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(_SilentCase(None, available=False), id="unset-and-absent"),
        pytest.param(_SilentCase("", available=False), id="empty-and-absent"),
        pytest.param(_SilentCase("1", available=True), id="set-and-present"),
    ],
)
def test_the_hook_stays_silent_otherwise(
    monkeypatch: pytest.MonkeyPatch,
    declare_extension: cabc.Callable[..., None],
    root_conftest: ModuleType,
    case: _SilentCase,
) -> None:
    """An unset or empty variable leaves the run alone.

    The empty case matters: a workflow that sets the variable to `''` must not
    fail a job that never intended to require the extension.
    """
    monkeypatch.delenv(REQUIRE_EXTENSION_ENV, raising=False)
    if case.env_value is not None:
        monkeypatch.setenv(REQUIRE_EXTENSION_ENV, case.env_value)
    declare_extension(available=case.available)

    root_conftest.pytest_configure(typ.cast("pytest.Config", None))
