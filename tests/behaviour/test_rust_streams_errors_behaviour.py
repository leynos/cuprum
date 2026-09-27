"""Behavioural tests for native stream exception categories.

The pump and consume entry points classify every failure before it becomes a
Python exception: a malformed request is a ``ValueError`` and a failing stream
is an ``OSError``. These scenarios pin that split at the real compiled
boundary, so a change that misroutes a category fails here rather than in
message-matching assertions elsewhere.

They live apart from ``test_rust_streams_behaviour.py`` to keep both modules
inside the 400-line code-file limit, and because the subject is the failure
contract rather than the transfer behaviour that module covers.

Example
-------
pytest tests/behaviour/test_rust_streams_errors_behaviour.py
"""

from __future__ import annotations

import contextlib
import os
import typing as typ

import pytest
from pytest_bdd import given, parsers, scenario, then, when

from tests.helpers.stream_pipes import _safe_close

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path
    from types import ModuleType


def _capture(
    entry_point: cabc.Callable[..., object],
    args: tuple[int, ...],
    kwargs: dict[str, int],
) -> BaseException:
    """Call ``entry_point`` and return the exception it raised.

    The scenario asserts the exception *class* in its ``Then`` step, so this
    ``When`` step must not narrow it: capturing only ``ValueError`` would turn a
    regression that raised the wrong category into an error inside the capture
    rather than a reported mismatch. ``BaseException`` is therefore correct
    here, and the class is checked against the ``Examples`` row immediately
    afterwards.

    Parameters
    ----------
    entry_point : collections.abc.Callable[..., object]
        The native helper to invoke.
    args : tuple[int, ...]
        Positional descriptors to pass.
    kwargs : dict[str, int]
        Keyword arguments, notably ``buffer_size``.

    Returns
    -------
    BaseException
        The exception the call raised.
    """
    with pytest.raises(BaseException) as excinfo:  # ruff: ignore[pytest-raises-too-broad] - the raised class is asserted by the Then step.
        entry_point(*args, **kwargs)
    return excinfo.value


@scenario(
    "../features/rust_streams.feature",
    "Preserve native stream exception categories",
)
def test_native_stream_exception_categories() -> None:
    """Validate each native failure raises the documented exception class.

    The outline runs this binding once per ``Examples`` row, binding the row's
    ``operation``, ``failure`` and ``exception`` columns as step parameters.
    Assertions live in the steps; this function is the pytest entry point.
    """


@given("the compiled Rust backend is required", target_fixture="required_backend")
def given_compiled_backend_required(rust_streams: ModuleType) -> ModuleType:
    """Return the compiled module, refusing to fall back to the shim.

    ``rust_streams`` already skips when the extension is absent. This step
    exists so the scenario states the precondition it actually depends on: the
    exception classes asserted below are produced by the compiled boundary, and
    a shim-only run would prove nothing about it.

    Parameters
    ----------
    rust_streams : ModuleType
        The compiled Rust streams module fixture.

    Returns
    -------
    ModuleType
        The compiled native module.
    """
    assert hasattr(rust_streams, "rust_pump_stream"), (
        "the compiled Rust backend must expose the native entry points"
    )
    return rust_streams


@when(
    parsers.parse("the {operation} native helper receives {failure}"),
    target_fixture="raised_exception",
)
def when_native_helper_receives(
    required_backend: ModuleType,
    tmp_path: Path,
    operation: str,
    failure: str,
) -> BaseException:
    """Call the named entry point so it fails in the stated way.

    Parameters
    ----------
    required_backend : ModuleType
        The compiled native module.
    tmp_path : Path
        Pytest-provided temporary directory holding the unreadable file the
        ``a fatal reader error`` row passes as its reader.
    operation : str
        ``pump`` or ``consume``, naming the entry point to call.
    failure : str
        ``a zero buffer size`` or ``a fatal reader error``.

    Returns
    -------
    BaseException
        The exception the entry point raised.

    Raises
    ------
    AssertionError
        If the row names an operation or failure the scenario does not define.
    """
    calls = {
        "pump": required_backend.rust_pump_stream,
        "consume": required_backend.rust_consume_stream,
    }
    entry_point = calls.get(operation)
    assert entry_point is not None, f"unknown operation {operation!r}"

    match failure:
        case "a zero buffer size":
            # The reader is never dereferenced — buffer validation runs first —
            # but it must still be one the wrapper can prepare. ``-1`` is not:
            # on Windows the wrapper resolves the reader through
            # ``msvcrt.get_osfhandle``, which raises ``OSError(EBADF)`` for it
            # before the native buffer check can run, so the row would observe
            # the wrapper's descriptor failure rather than the native category
            # it pins. An open read end survives that preparation on every
            # platform.
            with contextlib.ExitStack() as stack:
                reader = os.open(os.devnull, os.O_RDONLY)
                stack.callback(_safe_close, reader)
                args: tuple[int, ...] = (reader,)
                if operation == "pump":
                    # A separate write end, handed over rather than closed
                    # here: the wrapper closes a writer that never reached the
                    # native ownership boundary, so closing it here as well
                    # would be the double close this suite avoids. It is never
                    # written to, because the buffer check raises first.
                    writer = os.open(os.devnull, os.O_WRONLY)
                    args = (reader, writer)
                kwargs = {"buffer_size": 0}
                return _capture(entry_point, args, kwargs)
        case "a fatal reader error":
            # The reader must be *open but unreadable*, not closed. A closed
            # descriptor never reaches Rust on Windows: the wrapper resolves it
            # through ``msvcrt.get_osfhandle`` during preparation, which raises
            # ``OSError(EBADF)`` there, so the row would pass on the wrapper's
            # descriptor failure rather than on the native read path it pins —
            # the same vacuous pass the row above avoids. A file opened
            # ``O_WRONLY`` is open on every platform yet fails the first read,
            # which is also the device ``test_rust_errno_windows.py`` uses to
            # put a native code on the error the conversion must retain.
            with contextlib.ExitStack() as stack:
                unreadable = os.open(
                    tmp_path / "write-only.bin", os.O_WRONLY | os.O_CREAT
                )
                stack.callback(_safe_close, unreadable)
                args: tuple[int, ...] = (unreadable,)
                if operation == "pump":
                    # A separate write end, handed over rather than closed
                    # here: the pump adopts and closes it, so closing it here as
                    # well would be the double close this suite avoids.
                    writer = os.open(os.devnull, os.O_WRONLY)
                    args = (unreadable, writer)
                return _capture(entry_point, args, {})
        case _:
            msg = f"unknown failure {failure!r}"
            raise AssertionError(msg)


@then(parsers.parse("it raises {exception}"))
def then_it_raises(raised_exception: BaseException, exception: str) -> None:
    """Assert the raised class is exactly the documented category.

    Parameters
    ----------
    raised_exception : BaseException
        The exception captured by the ``When`` step.
    exception : str
        The expected class name from the ``Examples`` row.
    """
    expected = {"ValueError": ValueError, "OSError": OSError}.get(exception)
    assert expected is not None, f"unknown exception {exception!r}"
    assert isinstance(raised_exception, expected), (
        f"expected {exception}, found {type(raised_exception).__name__}: "
        f"{raised_exception}"
    )
