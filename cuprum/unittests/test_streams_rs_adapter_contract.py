"""Contract tests for the Python pump and consume adapters.

``rust_pump_stream`` and ``rust_consume_stream`` share one argument policy but
differ in ownership: the pump transfers a writer, while consume borrows its
reader (and duplicates it on Windows). These tests pin the policy without the
compiled extension, so each table row isolates one decision — which argument
is reported, which exception class and message appear, and whether a platform
step ran at all. The native module and the platform adapters are doubles here;
real descriptor and native-boundary behaviour lives in the extension-gated
suites.

Example
-------
pytest cuprum/unittests/test_streams_rs_adapter_contract.py
"""

from __future__ import annotations

import contextlib
import platform
import typing as typ
from types import SimpleNamespace
from unittest import mock

import pytest

from cuprum import _streams_rs

if typ.TYPE_CHECKING:
    import collections.abc as cabc


_I32_MAX = (1 << 31) - 1
_I64_MIN = -(1 << 63)
_I64_MAX = (1 << 63) - 1
_MAX_BUFFER_SIZE = 1 << 30
_BUFFER_SIZE = 65_536
_BUFFER_MESSAGES = {
    "zero": "buffer_size must be greater than zero",
    "negative": "buffer_size must be greater than zero",
    "cap": "buffer_size exceeds the maximum permitted size",
    "i64": "Python int too large to convert to C long",
}
_READER_MESSAGES = {
    "negative": "file descriptor must be non-negative",
    "wide": "file descriptor out of range",
    "i64": "Python int too large to convert to C long",
}
_CPYTHON_ONLY = pytest.mark.skipif(
    platform.python_implementation() != "CPython",
    reason="index conversion counts are a CPython implementation detail",
)


class _IndexLike:
    """Expose an integer only through ``__index__`` and count conversions."""

    def __init__(self, value: int) -> None:
        self.value = value
        self.calls = 0

    def __index__(self) -> int:
        """Return the wrapped integer, recording the conversion."""
        self.calls += 1
        return self.value


class _Wrapper(typ.Protocol):
    """A wrapper invoked with one reader and one buffer size."""

    def __call__(self, reader: int, buffer_size: int) -> object:
        """Invoke the wrapper and return its result."""
        ...


def _pump(reader: int, buffer_size: int) -> object:
    """Call the pump with a fixed writer that never reaches the native call."""
    return _streams_rs.rust_pump_stream(reader, 9001, buffer_size=buffer_size)


def _consume(reader: int, buffer_size: int) -> object:
    """Call consume with the supplied reader and buffer size."""
    return _streams_rs.rust_consume_stream(reader, buffer_size=buffer_size)


_WRAPPERS = [
    pytest.param(_pump, id="pump"),
    pytest.param(_consume, id="consume"),
]


def _platform_os(name: str) -> SimpleNamespace:
    """Model the two ``os`` attributes ``_streams_rs`` consults."""
    return SimpleNamespace(name=name, close=mock.Mock())


@pytest.fixture(name="native", autouse=True)
def fixture_native(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Install a native double and a platform-neutral POSIX adapter set.

    The fixture is automatic because every test in this module supplies fake
    descriptors. Without the ``os`` double, the pump's pre-native cleanup would
    call the real ``os.close`` on a number that names no descriptor the test
    owns — and could close pytest's own standard streams.
    """
    backend = SimpleNamespace(
        rust_pump_stream=mock.Mock(return_value=7),
        rust_consume_stream=mock.Mock(return_value="decoded"),
    )
    monkeypatch.setattr(_streams_rs, "_load_native", lambda: backend)
    monkeypatch.setattr(_streams_rs, "os", _platform_os("posix"))
    monkeypatch.setattr(_streams_rs, "_convert_fd_for_platform", lambda fd: fd)
    monkeypatch.setattr(_streams_rs, "_transfer_writer_fd_for_platform", lambda fd: fd)
    return backend


def _assert_message(excinfo: pytest.ExceptionInfo[BaseException], message: str) -> None:
    """Require the exact wrapper message, not merely a matching prefix."""
    assert str(excinfo.value) == message, (
        f"expected the wrapper to report {message!r}, found {str(excinfo.value)!r}"
    )


@pytest.mark.parametrize("wrapper", _WRAPPERS)
@pytest.mark.parametrize(
    ("buffer_size", "error", "message"),
    [
        pytest.param(0, ValueError, _BUFFER_MESSAGES["zero"], id="zero"),
        pytest.param(-1, ValueError, _BUFFER_MESSAGES["negative"], id="negative"),
        pytest.param(
            _MAX_BUFFER_SIZE + 1,
            ValueError,
            _BUFFER_MESSAGES["cap"],
            id="above-cap",
        ),
        pytest.param(
            _I64_MAX + 1, OverflowError, _BUFFER_MESSAGES["i64"], id="i64-high"
        ),
        pytest.param(
            _I64_MIN - 1, OverflowError, _BUFFER_MESSAGES["i64"], id="i64-low"
        ),
    ],
)
def test_invalid_buffer_is_reported_before_any_descriptor_work(
    native: SimpleNamespace,
    wrapper: _Wrapper,
    buffer_size: int,
    error: type[Exception],
    message: str,
) -> None:
    """The buffer policy is identical on both entry points.

    The reader is valid and the writer is a plain integer, so only the buffer
    can produce the reported failure. The native call must not run, and the
    pump must close the writer Python still owns rather than transfer it.
    """
    with pytest.raises(error) as excinfo:
        wrapper(5, buffer_size)

    _assert_message(excinfo, message)
    native.rust_pump_stream.assert_not_called()
    native.rust_consume_stream.assert_not_called()


@pytest.mark.parametrize("wrapper", _WRAPPERS)
@pytest.mark.parametrize(
    ("reader", "error", "message"),
    [
        pytest.param(-1, ValueError, _READER_MESSAGES["negative"], id="negative"),
        pytest.param(
            _I32_MAX + 1, ValueError, _READER_MESSAGES["wide"], id="above-i32"
        ),
        pytest.param((1 << 63), OverflowError, _READER_MESSAGES["i64"], id="i64-high"),
        pytest.param(
            -((1 << 63) + 1), OverflowError, _READER_MESSAGES["i64"], id="i64-low"
        ),
    ],
)
def test_invalid_reader_is_reported_with_the_shared_policy(
    native: SimpleNamespace,
    wrapper: _Wrapper,
    reader: int,
    error: type[Exception],
    message: str,
) -> None:
    """Reader range classification matches across pump and consume.

    A valid buffer keeps the earlier check out of the way, so the wrapper's own
    descriptor policy is the only source of the failure.
    """
    with pytest.raises(error) as excinfo:
        wrapper(reader, 1024)

    _assert_message(excinfo, message)
    native.rust_pump_stream.assert_not_called()
    native.rust_consume_stream.assert_not_called()


@pytest.mark.parametrize("wrapper", _WRAPPERS)
def test_buffer_error_precedes_a_competing_reader_error(
    native: SimpleNamespace,
    wrapper: _Wrapper,
) -> None:
    """Two stream-domain failures always name the buffer first.

    This is the documented compatibility decision: callers that supplied both
    an unusable size and an unusable descriptor previously observed whichever
    check their platform reached first. The unified order makes the buffer the
    stable answer.
    """
    with pytest.raises(
        ValueError, match="buffer_size must be greater than zero"
    ) as excinfo:
        wrapper(-1, 0)

    assert "file descriptor" not in str(excinfo.value), (
        f"the buffer must be named before the descriptor; found {str(excinfo.value)!r}"
    )


@pytest.mark.parametrize("wrapper", _WRAPPERS)
def test_buffer_error_precedes_a_reader_type_error(
    native: SimpleNamespace,
    wrapper: _Wrapper,
) -> None:
    """A bad buffer wins even when the reader is not index-like.

    Extraction order is buffer first, so the wrapper never asks the reader for
    ``__index__`` when the size is already known to be invalid.
    """
    with pytest.raises(ValueError, match="buffer_size"):
        wrapper(typ.cast("int", "not-an-integer"), 0)

    native.rust_consume_stream.assert_not_called()
    native.rust_pump_stream.assert_not_called()


@pytest.mark.parametrize("wrapper", _WRAPPERS)
@pytest.mark.parametrize(
    "wrong",
    [
        pytest.param("not-an-integer", id="str"),
        pytest.param(None, id="none"),
        pytest.param(1.5, id="float"),
        pytest.param(object(), id="object"),
    ],
)
def test_non_index_arguments_raise_type_error(
    native: SimpleNamespace,
    wrapper: _Wrapper,
    wrong: object,
) -> None:
    """Both arguments reject objects without ``__index__`` as ``TypeError``."""
    with pytest.raises(TypeError):
        wrapper(typ.cast("int", wrong), 1024)

    with pytest.raises(TypeError):
        wrapper(5, typ.cast("int", wrong))

    native.rust_consume_stream.assert_not_called()
    native.rust_pump_stream.assert_not_called()


def test_index_protocol_arguments_reach_native_as_plain_integers(
    native: SimpleNamespace,
) -> None:
    """Accepted index-like values are normalised exactly once each."""
    reader = _IndexLike(3)
    writer = 4
    size = _IndexLike(1024)

    moved = _streams_rs.rust_pump_stream(
        typ.cast("int", reader),
        writer,
        buffer_size=typ.cast("int", size),
    )

    assert moved == 7, "the wrapper must return the native byte count as an int"
    native.rust_pump_stream.assert_called_once_with(3, 4, buffer_size=1024)


@_CPYTHON_ONLY
def test_index_protocol_conversions_happen_once_per_argument(
    native: SimpleNamespace,
) -> None:
    """Each index-like argument is converted once, so no double side effect."""
    reader = _IndexLike(3)
    size = _IndexLike(1024)

    _streams_rs.rust_consume_stream(
        typ.cast("int", reader),
        buffer_size=typ.cast("int", size),
    )

    assert (reader.calls, size.calls) == (1, 1), (
        "each accepted index-like argument must be converted exactly once; found "
        f"reader={reader.calls}, buffer_size={size.calls}"
    )


def test_pump_runs_validation_then_conversion_then_transfer(
    native: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented step order is observable and fixed."""
    events: list[str] = []
    monkeypatch.setattr(
        _streams_rs,
        "_convert_fd_for_platform",
        lambda fd: events.append(f"convert:{fd}") or fd,
    )
    monkeypatch.setattr(
        _streams_rs,
        "_transfer_writer_fd_for_platform",
        lambda fd: events.append(f"transfer:{fd}") or fd,
    )
    native.rust_pump_stream.side_effect = lambda *args, **kwargs: (
        events.append("native") or 7
    )

    assert _streams_rs.rust_pump_stream(3, 4, buffer_size=1024) == 7

    assert events == ["convert:3", "transfer:4", "native"], (
        f"the reader must be converted before the writer is transferred; got {events}"
    )


def test_pump_skips_conversion_and_transfer_when_the_buffer_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Buffer validation happens before either platform step or native load."""
    convert = mock.Mock()
    transfer = mock.Mock()
    monkeypatch.setattr(_streams_rs, "_convert_fd_for_platform", convert)
    monkeypatch.setattr(_streams_rs, "_transfer_writer_fd_for_platform", transfer)

    with pytest.raises(ValueError, match="buffer_size"):
        _streams_rs.rust_pump_stream(3, 4, buffer_size=0)

    convert.assert_not_called()
    transfer.assert_not_called()


def test_pump_skips_transfer_when_the_reader_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unusable reader is rejected while Python still owns the writer."""
    convert = mock.Mock()
    transfer = mock.Mock()
    monkeypatch.setattr(_streams_rs, "_convert_fd_for_platform", convert)
    monkeypatch.setattr(_streams_rs, "_transfer_writer_fd_for_platform", transfer)

    with pytest.raises(ValueError, match="file descriptor must be non-negative"):
        _streams_rs.rust_pump_stream(-1, 4, buffer_size=1024)

    convert.assert_not_called()
    transfer.assert_not_called()


def test_pump_closes_an_untransferred_writer_after_a_transfer_failure(
    native: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing platform transfer leaves no owned descriptor behind."""
    closed = mock.Mock()
    monkeypatch.setattr(_streams_rs, "os", SimpleNamespace(name="posix", close=closed))
    monkeypatch.setattr(
        _streams_rs,
        "_transfer_writer_fd_for_platform",
        mock.Mock(side_effect=OSError("injected transfer failure")),
    )

    with pytest.raises(OSError, match="injected transfer failure"):
        _streams_rs.rust_pump_stream(3, 4, buffer_size=1024)

    closed.assert_called_once_with(4)
    native.rust_pump_stream.assert_not_called()


def test_windows_consume_duplicates_the_validated_reader(
    native: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows consume duplicates after validation and closes the duplicate."""
    duplicated: list[int] = []
    exited = mock.Mock()

    @contextlib.contextmanager
    def duplicate(reader_fd: int) -> cabc.Iterator[int]:
        duplicated.append(reader_fd)
        try:
            yield 99
        finally:
            exited()

    monkeypatch.setattr(_streams_rs, "os", _platform_os("nt"))
    monkeypatch.setattr(_streams_rs, "_duplicate_binary_reader", duplicate)

    assert _streams_rs.rust_consume_stream(5, buffer_size=1024) == "decoded"

    assert duplicated == [5], "the caller's validated descriptor must be duplicated"
    exited.assert_called_once_with()
    native.rust_consume_stream.assert_called_once_with(99, buffer_size=1024)


@pytest.mark.parametrize(
    ("reader", "buffer_size", "error", "message"),
    [
        pytest.param(
            -1,
            1024,
            ValueError,
            "file descriptor must be non-negative",
            id="reader-negative",
        ),
        pytest.param(
            5,
            0,
            ValueError,
            "buffer_size must be greater than zero",
            id="buffer-zero",
        ),
        pytest.param(
            typ.cast("int", "not-an-integer"),
            1024,
            TypeError,
            None,
            id="reader-type",
        ),
    ],
)
def test_windows_consume_never_duplicates_an_invalid_argument(
    native: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    reader: int,
    buffer_size: int,
    error: type[Exception],
    message: str | None,
) -> None:
    """CRT duplication is downstream of both Python-side checks.

    ``os.dup`` and ``msvcrt.setmode`` are the operations that turn a descriptor
    error into ``OSError`` (or a process abort), so neither may see a value the
    wrapper has already rejected.
    """
    duplicate = mock.Mock(side_effect=AssertionError("duplication must not run"))
    monkeypatch.setattr(_streams_rs, "os", _platform_os("nt"))
    monkeypatch.setattr(_streams_rs, "_duplicate_binary_reader", duplicate)

    with pytest.raises(error) as excinfo:
        _streams_rs.rust_consume_stream(reader, buffer_size=buffer_size)

    if message is not None:
        _assert_message(excinfo, message)
    duplicate.assert_not_called()
    native.rust_consume_stream.assert_not_called()


def test_windows_consume_closes_the_duplicate_when_native_raises(
    native: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The duplicate's lifetime ends when the native call fails."""
    closed = mock.Mock()

    @contextlib.contextmanager
    def duplicate(_reader_fd: int) -> cabc.Iterator[int]:
        try:
            yield 99
        finally:
            closed()

    native.rust_consume_stream.side_effect = OSError("injected native failure")
    monkeypatch.setattr(_streams_rs, "os", _platform_os("nt"))
    monkeypatch.setattr(_streams_rs, "_duplicate_binary_reader", duplicate)

    with pytest.raises(OSError, match="injected native failure"):
        _streams_rs.rust_consume_stream(5)

    closed.assert_called_once_with()


def test_windows_consume_does_not_convert_the_borrowed_reader(
    native: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CRT descriptor is passed to ``_read`` through ``os.dup``, not a handle."""
    convert = mock.Mock()
    transfer = mock.Mock()

    @contextlib.contextmanager
    def duplicate(reader_fd: int) -> cabc.Iterator[int]:
        yield reader_fd + 100

    monkeypatch.setattr(_streams_rs, "os", _platform_os("nt"))
    monkeypatch.setattr(_streams_rs, "_convert_fd_for_platform", convert)
    monkeypatch.setattr(_streams_rs, "_transfer_writer_fd_for_platform", transfer)
    monkeypatch.setattr(_streams_rs, "_duplicate_binary_reader", duplicate)

    _streams_rs.rust_consume_stream(5)

    convert.assert_not_called()
    transfer.assert_not_called()
    native.rust_consume_stream.assert_called_once_with(105, buffer_size=_BUFFER_SIZE)
