"""Compare the Rust consume boundary with Cuprum's Python stream consumer."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import enum
import functools
import importlib
import io
import os
import sys
import tempfile
import typing as typ
from pathlib import Path

import cyclopts

from cuprum._stream_config import _StreamConfig
from cuprum._streams import _consume_stream
from cuprum.capture_policy import FullCapture

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _BoundaryFailure(enum.StrEnum):
    """Name a bounded failure from the independent stream comparison."""

    PYTHON_CAPTURE_NOT_TEXT = enum.auto()
    CONSUMER_STOPPED_BEFORE_EOF = enum.auto()
    NATIVE_RESULT_NOT_TEXT = enum.auto()
    NATIVE_MODULE_OUTSIDE_ALLOWED_ROOT = enum.auto()
    PARITY_MISMATCH = enum.auto()


class BoundaryOracleError(RuntimeError):
    """Report a typed, bounded failure at the test-only consume boundary."""

    def __init__(
        self,
        failure: _BoundaryFailure,
        context: object | None = None,
    ) -> None:
        """Keep the failure kind and small diagnostic context."""
        self.failure = failure
        self.context = context
        super().__init__(self._message())

    def _message(self) -> str:
        """Render details held by the failure variant."""
        match self.failure:
            case _BoundaryFailure.PYTHON_CAPTURE_NOT_TEXT:
                return "Python consumer did not return captured text"
            case _BoundaryFailure.CONSUMER_STOPPED_BEFORE_EOF:
                return f"consumer stopped before EOF for {self.context} input bytes"
            case _BoundaryFailure.NATIVE_RESULT_NOT_TEXT:
                return "native consumer did not return text"
            case _BoundaryFailure.NATIVE_MODULE_OUTSIDE_ALLOWED_ROOT:
                return (
                    "native module is outside the candidate package and active "
                    f"project environment: {self.context}"
                )
            case _BoundaryFailure.PARITY_MISMATCH:
                label, payload_size, buffer_size, index, expected, actual = typ.cast(
                    "tuple[str, int, int, int, str, str]",
                    self.context,
                )
                return (
                    f"{label}: payload_bytes={payload_size} "
                    f"buffer_size={buffer_size} "
                    f"first_mismatch_char={index} "
                    f"expected={expected!r} actual={actual!r}"
                )


class _StreamsRust(typ.Protocol):
    """Describe the wrapper operations used by the oracle."""

    def _convert_fd_for_platform(self, reader_fd: int) -> int:
        """Convert a Python descriptor for the native extension."""
        ...

    def rust_consume_stream(
        self,
        reader_fd: int,
        *,
        buffer_size: int = 65536,
    ) -> str:
        """Consume bytes through the Python extension wrapper."""
        ...


class _NativeExtension(typ.Protocol):
    """Describe the direct native consumer used by the oracle."""

    def rust_consume_stream(
        self,
        reader_fd: int,
        *,
        buffer_size: int = 65536,
    ) -> str:
        """Consume bytes through the native extension API."""
        ...


@dc.dataclass(frozen=True, slots=True)
class _NativeModules:
    """Hold the imported wrapper, extension, and resolved extension path."""

    streams_rs: _StreamsRust
    native: _NativeExtension
    native_path: Path


@dc.dataclass(frozen=True, slots=True)
class _PayloadCase:
    """Hold one expected decoded payload and its tested buffer size."""

    payload_size: int
    buffer_size: int
    expected: str


@dc.dataclass(frozen=True, slots=True)
class _OracleMetadata:
    """Report interpreter and extension provenance to the Rust parent."""

    python_executable: str
    native_module_path: str


def _capture_python(payload: bytes) -> str:
    """Capture through the independent Python stream consumer."""

    async def consume() -> str:
        """Drain the in-memory payload on its owning event loop."""
        reader = asyncio.StreamReader()
        reader.feed_data(payload)
        reader.feed_eof()
        captured = await _consume_stream(
            reader,
            _StreamConfig(
                capture=FullCapture(),
                echo_output=False,
                sink=io.StringIO(),
                encoding="utf-8",
                errors="replace",
            ),
        )
        if not isinstance(captured.payload, str):
            raise BoundaryOracleError(_BoundaryFailure.PYTHON_CAPTURE_NOT_TEXT)
        return captured.payload

    return asyncio.run(consume())


def _consume_file(
    payload: bytes,
    consume: cabc.Callable[[int], object],
) -> str:
    """Consume one independent file and confirm its borrowed descriptor stays open."""
    with tempfile.TemporaryFile(mode="w+b") as reader:
        reader.write(payload)
        reader.flush()
        reader.seek(0)
        result = consume(reader.fileno())
        os.fstat(reader.fileno())
        if reader.read(1):
            raise BoundaryOracleError(
                _BoundaryFailure.CONSUMER_STOPPED_BEFORE_EOF,
                len(payload),
            )
    if not isinstance(result, str):
        raise BoundaryOracleError(_BoundaryFailure.NATIVE_RESULT_NOT_TEXT)
    return result


def _first_mismatch(expected: str, actual: str) -> tuple[int, str, str]:
    """Return a compact code-point difference, including a length boundary."""
    for index, (expected_char, actual_char) in enumerate(
        zip(expected, actual, strict=False)
    ):
        if expected_char != actual_char:
            return index, f"U+{ord(expected_char):04X}", f"U+{ord(actual_char):04X}"
    if len(expected) != len(actual):
        index = min(len(expected), len(actual))
        expected_char = expected[index] if index < len(expected) else "<EOF>"
        actual_char = actual[index] if index < len(actual) else "<EOF>"
        return index, expected_char, actual_char
    return -1, "", ""


def _assert_equal(label: str, actual: str, case: _PayloadCase) -> None:
    """Raise one compact mismatch through its typed error variant."""
    if actual == case.expected:
        return
    index, expected_char, actual_char = _first_mismatch(case.expected, actual)
    raise BoundaryOracleError(
        _BoundaryFailure.PARITY_MISMATCH,
        (
            label,
            case.payload_size,
            case.buffer_size,
            index,
            expected_char,
            actual_char,
        ),
    )


def _load_native_modules() -> _NativeModules:
    """Import the wrapper and extension and reject an unrelated installation."""
    import cuprum

    streams_rs = typ.cast(
        "_StreamsRust",
        importlib.import_module("cuprum._streams_rs"),
    )
    native_module = importlib.import_module("cuprum._rust_backend_native")
    native_path = Path(typ.cast("str", native_module.__file__)).resolve()
    native = typ.cast("_NativeExtension", native_module)
    package_root = Path(cuprum.__file__).resolve().parent
    virtual_environment = Path(sys.prefix).resolve()
    if not (
        native_path.is_relative_to(package_root)
        or native_path.is_relative_to(virtual_environment)
    ):
        raise BoundaryOracleError(
            _BoundaryFailure.NATIVE_MODULE_OUTSIDE_ALLOWED_ROOT,
            native_path,
        )
    return _NativeModules(streams_rs, native, native_path)


def _consume_direct(
    modules: _NativeModules,
    reader_fd: int,
    *,
    buffer_size: int | None = None,
) -> str:
    """Call the extension directly after platform-specific descriptor conversion."""
    native_fd = modules.streams_rs._convert_fd_for_platform(reader_fd)
    if buffer_size is None:
        return modules.native.rust_consume_stream(native_fd)
    return modules.native.rust_consume_stream(native_fd, buffer_size=buffer_size)


def _native_consumers(
    modules: _NativeModules,
    buffer_size: int,
) -> tuple[tuple[str, cabc.Callable[[int], object]], ...]:
    """Return independently callable wrapper and direct-native variants."""
    return (
        (
            "wrapper omitted buffer",
            functools.partial(modules.streams_rs.rust_consume_stream),
        ),
        (
            "wrapper explicit default",
            functools.partial(
                modules.streams_rs.rust_consume_stream, buffer_size=65536
            ),
        ),
        (
            "wrapper generated buffer",
            functools.partial(
                modules.streams_rs.rust_consume_stream, buffer_size=buffer_size
            ),
        ),
        ("direct native omitted buffer", functools.partial(_consume_direct, modules)),
        (
            "direct native explicit default",
            functools.partial(_consume_direct, modules, buffer_size=65536),
        ),
    )


def check_payload(payload: bytes, buffer_size: int) -> _OracleMetadata:
    """Compare the independent Python oracle and five native consume calls.

    Parameters
    ----------
    payload : bytes
        Input bytes sent to every independent consumer.
    buffer_size : int
        Positive read size exercised through the Python wrapper.

    Returns
    -------
    _OracleMetadata
        The active interpreter and extension paths reported to the Rust parent.
    """
    case = _PayloadCase(
        payload_size=len(payload),
        buffer_size=buffer_size,
        expected=payload.decode("utf-8", errors="replace"),
    )
    _assert_equal("Python consumer", _capture_python(payload), case)
    modules = _load_native_modules()
    for label, consume in _native_consumers(modules, buffer_size):
        _assert_equal(label, _consume_file(payload, consume), case)
    return _OracleMetadata(sys.executable, str(modules.native_path))


app = cyclopts.App()


@app.default
def run_boundary_oracle(buffer_size: int) -> None:
    """Read raw stdin bytes, compare consumers, and emit bounded metadata."""
    payload = sys.stdin.buffer.read()
    metadata = check_payload(payload, buffer_size)
    print(f"PASS payload_bytes={len(payload)} buffer_size={buffer_size}")
    print(f"PYTHON_EXECUTABLE={metadata.python_executable}")
    print(f"NATIVE_MODULE={metadata.native_module_path}")


if __name__ == "__main__":
    app()
