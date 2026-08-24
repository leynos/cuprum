"""Shared CrossHair availability-probe helpers for symbolic test modules.

Every module that runs CrossHair contracts needs the same import-time probe:
CrossHair's C-level tracer must support each bytecode opcode the running
interpreter emits, so importing the integration can fail on an interpreter
whose opcode set it does not yet handle. These helpers classify such a failure,
build the fallback bindings that make the symbolic tests skip rather than break
collection, and report the reason.

The import-time ``try``/``except`` itself stays in each consuming module,
because it binds that module's own CrossHair names. Only the classification and
reporting are shared, which is what the consumers would otherwise duplicate.

Consumers: `test_line_splitting.py`,
`test_subprocess_timeout_reducers_crosshair.py`.
"""

from __future__ import annotations

import importlib.metadata
import sys
import typing as typ
import warnings

# The four symbol slots are ``AnalysisOptionSet``, ``AnalysisKind``,
# ``MessageType``, and ``check_states``. On the success path each is a real
# CrossHair class or function; on the fallback path each is ``None``. A tighter
# alias would need the real CrossHair types imported for annotations, which
# this module must not require — it exists precisely for when CrossHair is
# absent — so the symbol slots stay ``typ.Any``.
type CrossHairSymbols = tuple[str, typ.Any, typ.Any, typ.Any, typ.Any]


def _crosshair_probe_failure_reason(error: BaseException) -> str:
    """Return a skip reason for expected probe failures and raise the rest."""
    # Expected unavailability is a missing CrossHair dependency
    # (``ImportError``) or tracer incompatibility, which surfaces as a
    # ``TraceException``-named ``BaseException``. Control-flow exceptions and
    # anything else are re-raised so an unexpected CrossHair break is never
    # downgraded to a skip.
    if isinstance(error, KeyboardInterrupt | SystemExit | GeneratorExit):
        raise error
    is_expected = (
        isinstance(error, ImportError) or error.__class__.__name__ == "TraceException"
    )
    if not is_expected:
        raise error
    return f"CrossHair unavailable: {error.__class__.__name__}: {error}"


def _crosshair_unavailable_symbols(error: BaseException) -> CrossHairSymbols:
    """Return fallback CrossHair symbols for an expected probe failure."""
    # The four ``None`` bindings stand in for ``AnalysisOptionSet``,
    # ``AnalysisKind``, ``MessageType``, and ``check_states`` so a consuming
    # module can skip its symbolic tests instead of failing collection.
    reason = _crosshair_probe_failure_reason(error)
    return (reason, None, None, None, None)


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
