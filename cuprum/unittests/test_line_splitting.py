r"""Property-based tests for pure stream line-splitting helpers.

This module verifies the text-only helpers that support line callbacks in
``cuprum._streams``.  The helpers are deliberately tested without subprocesses,
asyncio streams, or backend dispatch so failures point at the line-splitting
contract rather than at process I/O.

``_split_complete_lines`` returns completed lines with their recognised line
endings removed plus the final partial line, if any. ``_strip_line_ending``
removes one trailing ``"\r\n"``, ``"\n"``, or ``"\r"`` sequence and leaves the
rest of the text untouched.  The Hypothesis tests exercise generated text with
mixed line endings to prove preservation, stable remainder handling, and
idempotent stripping.  The CrossHair contracts symbolically check bounded
versions of the same invariants; a confirmed result means the contract held for
the explored symbolic state space, while a failure should be treated as a
minimal counterexample for the pure helper rather than as stream-backend drift.

At import time this module probes CrossHair availability before collecting the
symbolic tests. Expected unavailability covers a missing CrossHair dependency
(``ImportError``) and tracer incompatibility reported as a
``TraceException``-named ``BaseException`` during import. Control-flow
exceptions are re-raised. Expected fallback binds the CrossHair-only names to
``None`` so the symbolic tests skip instead of failing test collection.
"""

from __future__ import annotations

import builtins
import importlib
import importlib.metadata
import sys
import types
import typing as typ
import warnings

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cuprum._testing import _split_complete_lines, _strip_line_ending

if typ.TYPE_CHECKING:
    import collections.abc as cabc


_CROSSHAIR_PROBE_EXCEPTIONS: tuple[type[BaseException], ...] = (BaseException,)
_MODULE_NAME: str = "cuprum.unittests.test_line_splitting"


def _crosshair_probe_failure_reason(error: BaseException) -> str:
    """Return a skip reason for expected probe failures and raise the rest."""
    if isinstance(error, KeyboardInterrupt | SystemExit | GeneratorExit):
        raise error
    is_expected = (
        isinstance(error, ImportError) or error.__class__.__name__ == "TraceException"
    )
    if not is_expected:
        raise error
    return f"CrossHair unavailable: {error.__class__.__name__}: {error}"


def _crosshair_unavailable_symbols(
    error: BaseException,
) -> tuple[str, typ.Any, typ.Any, typ.Any, typ.Any]:
    """Return fallback CrossHair symbols for an expected probe failure."""
    reason = _crosshair_probe_failure_reason(error)
    return (
        reason,
        typ.cast("typ.Any", None),
        typ.cast("typ.Any", None),
        typ.cast("typ.Any", None),
        typ.cast("typ.Any", None),
    )


def _warn_crosshair_unavailable(reason: str) -> None:
    """Warn that CrossHair symbolic tests are unavailable."""
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    try:
        crosshair_version = importlib.metadata.version("crosshair-tool")
    except importlib.metadata.PackageNotFoundError:
        crosshair_version = "not installed"
    opcode_context = (
        " opcode compatibility: CrossHair tracer does not support this "
        "interpreter opcode set."
        if "TraceException" in reason
        else ""
    )
    warnings.warn(
        (
            f"{reason}; Python {python_version}; "
            f"CrossHair status: unavailable; CrossHair version: "
            f"{crosshair_version}.{opcode_context}"
        ),
        RuntimeWarning,
        stacklevel=2,
    )


# CrossHair's C-level tracer must support every bytecode opcode the running
# interpreter emits. On a Python whose opcode set CrossHair does not yet
# handle, importing the integration raises ``crosshair.tracers.TraceException``
# (not ``ImportError``) — this was the ``CALL_KW`` gap on early 3.15 betas
# (issue #109). Probe for a usable CrossHair here and degrade to skipping the
# symbolic checks if it cannot trace this interpreter, rather than hard-coding a
# version gate that must be revised by hand each time CrossHair catches up. As
# of crosshair-tool 0.0.104, ``CALL_KW`` is supported, so this probe succeeds on
# the supported interpreters.
try:
    import crosshair.core_and_libs  # noqa: F401
    from crosshair.options import AnalysisKind, AnalysisOptionSet
    from crosshair.statespace import MessageType
    from crosshair.test_util import check_states
except _CROSSHAIR_PROBE_EXCEPTIONS as _crosshair_exc:
    # ``crosshair.tracers.TraceException`` subclasses ``BaseException`` (not
    # ``Exception``) and is raised while importing the tracer module itself,
    # so it cannot be named here without re-triggering the failing import.
    # Re-raise genuine control-flow exceptions and any unexpected failure so
    # the probe only degrades for known CrossHair compatibility cases.
    (
        _CROSSHAIR_UNAVAILABLE_REASON,
        AnalysisOptionSet,
        AnalysisKind,
        MessageType,
        check_states,
    ) = _crosshair_unavailable_symbols(_crosshair_exc)
    _warn_crosshair_unavailable(_CROSSHAIR_UNAVAILABLE_REASON)
else:
    _CROSSHAIR_UNAVAILABLE_REASON = "CrossHair is not installed"

_LINE_ENDINGS: tuple[str, str, str] = ("\r\n", "\n", "\r")
_PYTHON_LINE_BOUNDARIES: str = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
_PROPERTY_SETTINGS: settings = settings(
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    # Keep the issue-requested bound; CrossHair covers the symbolic edge cases.
    max_examples=30,
)


def _normalise_line_endings(text: str) -> str:
    """Normalize recognized line endings to line-feed characters."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _rebuild_normalised_text(lines: list[str], remainder: str) -> str:
    """Rebuild text from split output using normalized line endings."""
    return "".join(f"{line}\n" for line in lines) + remainder


def _line_ending_suffix(line: str) -> str:
    """Return the single trailing line-ending sequence, if present."""
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return ""


def _split_preserves_normalised_text(text: str) -> bool:
    """Return whether split output accounts for all input text."""
    lines, remainder = _split_complete_lines(text)
    return len(_normalise_line_endings(text)) == (
        sum(len(line) + 1 for line in lines) + len(remainder)
    )


class _UnexpectedProbeFailure(BaseException):
    """Test double for unexpected CrossHair probe failures."""


def _trace_exception(message: str) -> BaseException:
    """Build a test double named like CrossHair's tracer exception."""
    trace_exception_type = type("TraceException", (BaseException,), {})
    return trace_exception_type(message)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ImportError("crosshair missing"), id="import_error"),
        pytest.param(_trace_exception("unsupported opcode"), id="trace_exception"),
    ],
)
def test_crosshair_probe_accepts_expected_unavailability(
    error: BaseException,
) -> None:
    """Probe helper classifies known CrossHair availability failures."""
    reason = _crosshair_probe_failure_reason(error)

    assert error.__class__.__name__ in reason
    assert str(error) in reason


def test_crosshair_probe_rejects_unexpected_failures() -> None:
    """Probe helper leaves unexpected import failures visible."""
    failure = _UnexpectedProbeFailure("unexpected probe failure")

    with pytest.raises(_UnexpectedProbeFailure) as exc_info:
        _crosshair_probe_failure_reason(failure)

    assert exc_info.value is failure


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ImportError("crosshair missing"), id="import_error"),
        pytest.param(_trace_exception("unsupported opcode"), id="trace_exception"),
    ],
)
def test_crosshair_probe_reason_names_failure(error: BaseException) -> None:
    """Probe diagnostic records the expected failure class and message."""
    reason = _crosshair_probe_failure_reason(error)

    assert error.__class__.__name__ in reason
    assert str(error) in reason


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ImportError("crosshair missing"), id="import_error"),
        pytest.param(_trace_exception("unsupported opcode"), id="trace_exception"),
    ],
)
def test_crosshair_probe_fallback_disables_symbolic_checks(
    error: BaseException,
) -> None:
    """Probe fallback records expected failures and clears CrossHair bindings."""
    reason, options, kind, message_type, state_checker = _crosshair_unavailable_symbols(
        error
    )

    assert error.__class__.__name__ in reason
    assert str(error) in reason
    assert options is None
    assert kind is None
    assert message_type is None
    assert state_checker is None


def test_warn_crosshair_unavailable_emits_runtime_warning() -> None:
    """Warning helper reports the CrossHair fallback reason."""
    reason = "CrossHair unavailable: TraceException: unsupported opcode"

    with pytest.warns(RuntimeWarning) as warning_info:
        _warn_crosshair_unavailable(reason)

    message = str(warning_info[0].message)
    assert reason in message
    assert f"Python {sys.version_info.major}.{sys.version_info.minor}." in message
    assert "CrossHair status: unavailable" in message
    assert "CrossHair version:" in message


def test_warn_crosshair_unavailable_reports_opcode_context() -> None:
    """Tracer fallback warning names opcode compatibility context."""
    reason = "CrossHair unavailable: TraceException: CALL_KW"

    with pytest.warns(RuntimeWarning) as warning_info:
        _warn_crosshair_unavailable(reason)

    message = str(warning_info[0].message)
    assert reason in message
    assert "opcode compatibility" in message
    assert "interpreter opcode set" in message


class _CrossHairImportFailure:
    """Import hook for exercising the module-level CrossHair probe."""

    def __init__(self, failure: BaseException) -> None:
        """Store the targeted import failure."""
        self._failure = failure
        self._import = builtins.__import__

    def __call__(
        self,
        name: str,
        globals_: dict[str, object] | None = None,
        locals_: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> types.ModuleType:
        """Import fake CrossHair modules or delegate to the real importer."""
        if name == "crosshair.core_and_libs":
            raise self._failure
        return self._import(name, globals_, locals_, fromlist, level)


class _FakeCrossHairOptions(types.ModuleType):
    """Fake ``crosshair.options`` module with imported attributes."""

    AnalysisKind: object
    AnalysisOptionSet: object


class _FakeCrossHairStatespace(types.ModuleType):
    """Fake ``crosshair.statespace`` module with imported attributes."""

    MessageType: object


class _FakeCrossHairTestUtil(types.ModuleType):
    """Fake ``crosshair.test_util`` module with imported attributes."""

    check_states: object


def _fake_crosshair_modules() -> dict[str, types.ModuleType]:
    """Build deterministic CrossHair modules for import-probe tests."""
    crosshair = types.ModuleType("crosshair")
    core_and_libs = types.ModuleType("crosshair.core_and_libs")
    options = _FakeCrossHairOptions("crosshair.options")
    statespace = _FakeCrossHairStatespace("crosshair.statespace")
    test_util = _FakeCrossHairTestUtil("crosshair.test_util")
    options.AnalysisKind = object()
    options.AnalysisOptionSet = object()
    statespace.MessageType = object()
    test_util.check_states = object()
    return {
        "crosshair": crosshair,
        "crosshair.core_and_libs": core_and_libs,
        "crosshair.options": options,
        "crosshair.statespace": statespace,
        "crosshair.test_util": test_util,
    }


def _import_module_with_crosshair_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> types.ModuleType:
    """Import this module with a deterministic CrossHair probe failure."""
    executing_module = sys.modules[__name__]
    monkeypatch.setattr(builtins, "__import__", _CrossHairImportFailure(failure))
    for module_name, module in _fake_crosshair_modules().items():
        monkeypatch.setitem(sys.modules, module_name, module)
    if _MODULE_NAME in sys.modules:
        monkeypatch.delitem(sys.modules, _MODULE_NAME)
    try:
        return importlib.import_module(_MODULE_NAME)
    finally:
        monkeypatch.setitem(sys.modules, __name__, executing_module)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(ImportError("crosshair missing"), id="import_error"),
        pytest.param(_trace_exception("unsupported opcode"), id="trace_exception"),
    ],
)
def test_crosshair_import_probe_binds_fallback_symbols(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    """Import-time probe binds fallback symbols for expected failures."""
    with pytest.warns(RuntimeWarning):
        module = _import_module_with_crosshair_failure(monkeypatch, failure)

    expected_reason = f"CrossHair unavailable: {failure.__class__.__name__}: {failure}"
    assert expected_reason == module._CROSSHAIR_UNAVAILABLE_REASON
    assert module.AnalysisOptionSet is None, "AnalysisOptionSet fallback"
    assert module.AnalysisKind is None, "AnalysisKind fallback"
    assert module.MessageType is None, "MessageType fallback"
    assert module.check_states is None, "check_states fallback"


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ImportError("crosshair missing"), id="import_error"),
        pytest.param(_trace_exception("unsupported opcode"), id="trace_exception"),
    ],
)
def test_crosshair_unavailable_symbols_clear_all_bindings(
    error: BaseException,
) -> None:
    """Module-level fallback clears every CrossHair symbol binding."""
    _reason, options, kind, message_type, state_checker = (
        _crosshair_unavailable_symbols(error)
    )

    assert options is None
    assert kind is None
    assert message_type is None
    assert state_checker is None


def test_crosshair_contracts_skip_when_crosshair_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract runner skips when the module-level CrossHair probe failed."""
    monkeypatch.setattr(sys.modules[__name__], "check_states", None)

    with pytest.raises(pytest.skip.Exception):
        test_crosshair_contracts(_split_no_text_loss_contract)


@pytest.mark.parametrize(
    "exc_type",
    [KeyboardInterrupt, SystemExit, GeneratorExit],
    ids=["keyboard_interrupt", "system_exit", "generator_exit"],
)
def test_crosshair_probe_reraises_control_flow_exceptions(
    exc_type: type[BaseException],
) -> None:
    """Probe re-raises control-flow exceptions unconditionally."""
    error = exc_type()

    with pytest.raises(exc_type):
        _crosshair_probe_failure_reason(error)


@st.composite
def _text_with_line_endings(draw: st.DrawFn) -> str:
    """Generate arbitrary text with embedded recognized line endings."""
    fragments = draw(
        st.lists(
            st.text(
                alphabet=st.characters(exclude_characters=_PYTHON_LINE_BOUNDARIES),
                max_size=8,
            ),
            min_size=1,
            max_size=8,
        ),
    )
    endings = draw(
        st.lists(
            st.sampled_from((*_LINE_ENDINGS, "")),
            min_size=len(fragments) - 1,
            max_size=len(fragments) - 1,
        ),
    )

    text = "".join(fragments[index] + ending for index, ending in enumerate(endings))
    return text + fragments[-1]


@st.composite
def _line_with_optional_ending(draw: st.DrawFn) -> str:
    """Generate one line that may have a recognized trailing line ending."""
    body = draw(
        st.text(alphabet=st.characters(exclude_characters=_PYTHON_LINE_BOUNDARIES)),
    )
    ending = draw(st.sampled_from((*_LINE_ENDINGS, "")))
    return body + ending


@_PROPERTY_SETTINGS
@given(text=_text_with_line_endings())
def test_split_complete_lines_preserves_all_text(text: str) -> None:
    """Property: splitting and rebuilding preserves normalized text.

    Parameters
    ----------
    text : str
        Generated text containing arbitrary recognised line endings.
    """
    lines, remainder = _split_complete_lines(text)

    assert _rebuild_normalised_text(lines, remainder) == _normalise_line_endings(
        text
    ), (
        "_split_complete_lines output rebuilt with _rebuild_normalised_text "
        "must match _normalise_line_endings input"
    )


@_PROPERTY_SETTINGS
@given(text=_text_with_line_endings())
def test_split_complete_lines_remainder_has_no_line_ending(text: str) -> None:
    """Property: the returned remainder is never a completed line.

    Parameters
    ----------
    text : str
        Generated text containing arbitrary recognised line endings.
    """
    _lines, remainder = _split_complete_lines(text)

    assert not remainder.endswith(("\n", "\r")), (
        "_split_complete_lines remainder must not end with a recognized line ending"
    )
    if text.endswith(_LINE_ENDINGS):
        assert remainder == "", (
            "_split_complete_lines remainder must be empty when input ends with a "
            "recognized line ending"
        )


@pytest.mark.parametrize("text", ["a\vb", "a\x85b", "a\u2028b"])
def test_split_complete_lines_handles_python_line_boundaries(text: str) -> None:
    """Example: Python-recognized line boundaries delimit completed lines."""
    split_lines = text.splitlines(keepends=True)
    expected_lines = [_strip_line_ending(line) for line in split_lines[:-1]]
    expected_remainder = split_lines[-1]

    lines, remainder = _split_complete_lines(text)

    assert lines == expected_lines
    assert remainder == expected_remainder


@_PROPERTY_SETTINGS
@given(line=_line_with_optional_ending())
def test_strip_line_ending_idempotent(line: str) -> None:
    """Property: stripping a line ending is idempotent.

    Parameters
    ----------
    line : str
        Generated line with an optional recognised line ending.
    """
    stripped = _strip_line_ending(line)

    assert _strip_line_ending(stripped) == stripped, (
        "_strip_line_ending must be idempotent after the first strip"
    )


@_PROPERTY_SETTINGS
@given(line=_line_with_optional_ending())
def test_strip_line_ending_removes_only_trailing(line: str) -> None:
    """Property: stripping removes exactly one trailing line-ending sequence.

    Parameters
    ----------
    line : str
        Generated line with an optional recognised line ending.
    """
    suffix = _line_ending_suffix(line)

    assert _strip_line_ending(line) == line.removesuffix(suffix), (
        "_strip_line_ending must remove only the suffix reported by _line_ending_suffix"
    )


def _split_no_text_loss_contract(text: str) -> None:
    r"""CrossHair contract for split text preservation.

    pre: len(text) <= 3
    pre: all(character not in "\v\f\x1c\x1d\x1e\x85\u2028\u2029" for character in text)
    post: _split_preserves_normalised_text(text)
    """


def _strip_line_ending_contract(line: str) -> None:
    """CrossHair contract for one trailing line-ending removal.

    pre: len(line) <= 4
    post: _strip_line_ending(line) == line.removesuffix(_line_ending_suffix(line))
    """


@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    "contract",
    [
        pytest.param(_split_no_text_loss_contract, id="split_no_text_loss"),
        pytest.param(_strip_line_ending_contract, id="strip_line_ending"),
    ],
)
@pytest.mark.skipif(check_states is None, reason=_CROSSHAIR_UNAVAILABLE_REASON)
def test_crosshair_contracts(contract: cabc.Callable[..., None]) -> None:
    """Property: CrossHair symbolically verifies line-splitting contracts.

    ``per_condition_timeout`` is a wall-clock budget. Confirming these
    contracts exhausts the bounded symbolic state space in a few CPU-seconds,
    but under the parallel ``-n auto`` test run the CrossHair worker competes
    for CPU and needs more wall-clock time, so an over-tight budget yields a
    flaky ``CANNOT_CONFIRM``. Use CrossHair's recommended confirmation budget
    (it returns as soon as the space is exhausted, so unloaded runs stay fast)
    and a per-test timeout above the global default to accommodate the slower
    worst case under load.
    """
    if check_states is None:
        pytest.skip(_CROSSHAIR_UNAVAILABLE_REASON)
    check_states(
        contract,
        MessageType.CONFIRMED,
        AnalysisOptionSet(
            analysis_kind=(AnalysisKind.PEP316,),
            per_condition_timeout=60,
        ),
    )
