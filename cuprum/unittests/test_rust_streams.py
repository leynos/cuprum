"""Unit tests for the Rust stream pump and consumer.

These tests validate the optional Rust-backed pump and consume behaviour and
error handling.

Example
-------
pytest cuprum/unittests/test_rust_streams.py
"""

from __future__ import annotations

import ast
import contextlib
import errno
import os
import pathlib
import typing as typ

import pytest

from tests.helpers.stream_pipes import _pipe_pair, _read_all, _safe_close

if typ.TYPE_CHECKING:
    from types import ModuleType


def _pump_payload(
    streams: ModuleType,
    payload: bytes,
    *,
    buffer_size: int | None = None,
) -> tuple[bytes, int]:
    """Pump payload through the Rust stream and return output and count."""
    with _pipe_pair() as (in_read, in_write, out_read, out_write):
        view = memoryview(payload)
        while view:
            written = os.write(in_write, view)
            assert written > 0, "expected os.write to make progress"
            view = view[written:]
        _safe_close(in_write)

        kwargs: dict[str, int] = {}
        if buffer_size is not None:
            kwargs["buffer_size"] = buffer_size
        transferred = streams.rust_pump_stream(in_read, out_write, **kwargs)

        _safe_close(out_write)
        output = _read_all(out_read)

    return output, transferred


def _consume_payload(
    streams: ModuleType,
    payload: bytes,
    **kwargs: object,
) -> str:
    """Consume payload through the Rust stream and return decoded output."""
    with contextlib.ExitStack() as stack:
        read_fd, write_fd = os.pipe()
        stack.callback(_safe_close, read_fd)
        stack.callback(_safe_close, write_fd)
        view = memoryview(payload)
        while view:
            written = os.write(write_fd, view)
            assert written > 0, "expected os.write to make progress"
            view = view[written:]
        _safe_close(write_fd)
        forwarded_kwargs = dict(kwargs)
        if forwarded_kwargs.get("buffer_size") is None:
            forwarded_kwargs.pop("buffer_size", None)

        return typ.cast(
            "str",
            streams.rust_consume_stream(read_fd, **forwarded_kwargs),
        )


def _module_references_symbol(path: pathlib.Path, symbol: str) -> bool:
    """Return whether *path* contains executable references to *symbol*."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(_node_references_symbol(node, symbol) for node in ast.walk(tree))


def _node_references_symbol(node: ast.AST, symbol: str) -> bool:
    """Return whether an AST node references *symbol* as a Python symbol."""
    if isinstance(node, ast.Name):
        return node.id == symbol
    if isinstance(node, ast.Attribute):
        return node.attr == symbol
    if isinstance(node, ast.alias):
        return symbol in {node.name, node.asname}
    return False


@pytest.mark.parametrize(
    ("source", "expected_reference"),
    [
        ('"""rust_consume_stream is mentioned here."""\n', "ignored"),
        ("# rust_consume_stream is mentioned here.\n", "ignored"),
        ("rust_consume_stream(reader_fd)\n", "referenced"),
        ("streams.rust_consume_stream(reader_fd)\n", "referenced"),
        ("from cuprum._streams_rs import rust_consume_stream\n", "referenced"),
    ],
)
def test_module_references_symbol_ignores_non_code_text(
    tmp_path: pathlib.Path,
    source: str,
    expected_reference: typ.Literal["ignored", "referenced"],
) -> None:
    """The production-reference guard detects symbols, not raw text."""
    module_path = tmp_path / "candidate.py"
    module_path.write_text(source, encoding="utf-8")

    has_reference = expected_reference == "referenced"
    assert (
        _module_references_symbol(module_path, "rust_consume_stream") is has_reference
    )


@pytest.mark.parametrize(
    ("test_id", "payload", "buffer_size"),
    [
        ("basic_payload", b"cuprum-stream-payload", None),
        ("custom_buffer_size", bytes(range(256)) * 64, 1024),
        ("zero_bytes", b"", None),
    ],
    ids=["basic_payload", "custom_buffer_size", "zero_bytes"],
)
def test_rust_pump_stream_transfers_data(
    rust_streams: ModuleType,
    test_id: str,
    payload: bytes,
    buffer_size: int | None,
) -> None:
    """Validate rust_pump_stream transfers bytes between pipes.

    Parameterised test covering normal transfer, custom buffer sizes, and empty
    input.

    Parameters
    ----------
    rust_streams : ModuleType
        The Rust streams module fixture.
    test_id : str
        Test case identifier for parameterisation.
    payload : bytes
        The payload to transfer through the pump.
    buffer_size : int | None
        Optional buffer size parameter; None uses default.

    Returns
    -------
    None
    """
    output, transferred = _pump_payload(rust_streams, payload, buffer_size=buffer_size)

    assert output == payload, f"expected payload to round-trip through pump ({test_id})"
    assert transferred == len(payload), (
        f"expected transferred count to match payload ({test_id})"
    )


def test_rust_pump_stream_raises_on_invalid_buffer(
    rust_streams: ModuleType,
) -> None:
    """Verify rust_pump_stream rejects invalid buffer sizes.

    Parameters
    ----------
    rust_streams : ModuleType
        The Rust streams module fixture.

    Returns
    -------
    None
    """
    with contextlib.ExitStack() as stack:
        in_read, in_write = os.pipe()
        stack.callback(_safe_close, in_read)
        stack.callback(_safe_close, in_write)
        with pytest.raises(ValueError, match="buffer_size"):
            rust_streams.rust_pump_stream(in_read, in_write, buffer_size=0)


def test_rust_pump_stream_propagates_io_errors(
    rust_streams: ModuleType,
) -> None:
    """Verify rust_pump_stream raises OSError on I/O failure.

    Parameters
    ----------
    rust_streams : ModuleType
        The Rust streams module fixture.

    Returns
    -------
    None
    """
    with contextlib.ExitStack() as stack:
        read_fd, write_fd = os.pipe()
        stack.callback(_safe_close, read_fd)
        stack.callback(_safe_close, write_fd)
        _safe_close(read_fd)
        with pytest.raises(
            OSError,
            match=r"(?i)(Bad file descriptor|invalid handle|handle is invalid)",
        ) as excinfo:
            rust_streams.rust_pump_stream(read_fd, write_fd)
    assert excinfo.value.errno in {errno.EBADF, errno.EINVAL}, (
        "expected errno to indicate an invalid file descriptor/handle"
    )


def test_rust_pump_stream_ignores_broken_pipe(
    rust_streams: ModuleType,
) -> None:
    """Verify rust_pump_stream drains input even if the writer breaks.

    Parameters
    ----------
    rust_streams : ModuleType
        The Rust streams module fixture.

    Returns
    -------
    None
    """
    payload = b"x" * (64 * 1024)
    with _pipe_pair() as (in_read, in_write, out_read, out_write):
        _safe_close(out_read)
        view = memoryview(payload)
        while view:
            written = os.write(in_write, view)
            assert written > 0, "expected os.write to make progress"
            view = view[written:]
        _safe_close(in_write)

        try:
            transferred = rust_streams.rust_pump_stream(in_read, out_write)
        except OSError as exc:
            if getattr(exc, "errno", None) in {errno.EPIPE, errno.ECONNRESET}:
                pytest.fail(
                    "rust_pump_stream raised OSError for broken pipe/connection reset"
                )
            raise

        remaining = os.read(in_read, 4096)
        assert remaining == b"", "expected input pipe to be fully drained"

    assert isinstance(transferred, int), "expected transfer count to be integer"
    assert transferred <= len(payload), "expected transfer count to be bounded"


class TestRustConsumeStream:
    """Coverage for Rust-backed consume stream helpers."""

    @staticmethod
    def _consume(
        rust_streams: ModuleType,
        payload: bytes,
        **kwargs: object,
    ) -> str:
        """Consume payload via the Rust helper."""
        return _consume_payload(rust_streams, payload, **kwargs)

    @pytest.mark.parametrize(
        ("test_id", "payload", "buffer_size"),
        [
            ("ascii_explicit_default", b"rust-consume-stream", 65536),
            ("multibyte_split", b"snowman \xe2\x98\x83", 2),
        ],
        ids=["ascii_explicit_default", "multibyte_split"],
    )
    def test_decodes_payload(
        self,
        rust_streams: ModuleType,
        test_id: str,
        payload: bytes,
        buffer_size: int,
    ) -> None:
        """Validate rust_consume_stream decodes UTF-8 payloads."""
        output = self._consume(rust_streams, payload, buffer_size=buffer_size)
        expected = payload.decode("utf-8", errors="replace")
        assert output == expected, (
            f"expected decoded output to match Python replace semantics ({test_id})"
        )

    def test_uses_default_buffer_size(
        self,
        rust_streams: ModuleType,
    ) -> None:
        """Validate rust_consume_stream uses the default buffer size."""
        payload = b"rust-consume-default"
        output = self._consume(rust_streams, payload)
        expected = payload.decode("utf-8", errors="replace")
        assert output == expected, "expected default buffer size to decode payload"

    def test_replaces_invalid_bytes(
        self,
        rust_streams: ModuleType,
    ) -> None:
        """Ensure rust_consume_stream replaces invalid UTF-8 bytes."""
        payload = b"valid-\xff\xfe-end"
        output = self._consume(rust_streams, payload, buffer_size=3)
        expected = payload.decode("utf-8", errors="replace")
        assert output == expected, "expected invalid bytes to be replaced"

    def test_replaces_incomplete_sequence(
        self,
        rust_streams: ModuleType,
    ) -> None:
        """Ensure rust_consume_stream replaces incomplete UTF-8 sequences."""
        payload = b"trail-\xe2\x98"
        output = self._consume(rust_streams, payload, buffer_size=2)
        expected = payload.decode("utf-8", errors="replace")
        assert output == expected, "expected incomplete sequence to be replaced"

    @staticmethod
    def test_does_not_close_fd(
        rust_streams: ModuleType,
    ) -> None:
        """Ensure rust_consume_stream does not close the underlying FD."""
        with contextlib.ExitStack() as stack:
            read_fd, write_fd = os.pipe()
            stack.callback(_safe_close, read_fd)
            stack.callback(_safe_close, write_fd)
            os.write(write_fd, b"non-destructive")
            _safe_close(write_fd)
            output = rust_streams.rust_consume_stream(read_fd)
            assert output == "non-destructive"

            try:
                os.read(read_fd, 0)
            except OSError as exc:
                if exc.errno == errno.EBADF:
                    pytest.fail(
                        "rust_consume_stream must not close the file descriptor"
                    )
                raise

    @staticmethod
    def test_rejects_invalid_buffer(
        rust_streams: ModuleType,
    ) -> None:
        """Verify rust_consume_stream rejects invalid buffer sizes."""
        with contextlib.ExitStack() as stack:
            read_fd, write_fd = os.pipe()
            stack.callback(_safe_close, read_fd)
            stack.callback(_safe_close, write_fd)
            _safe_close(write_fd)
            with pytest.raises(ValueError, match="buffer_size"):
                rust_streams.rust_consume_stream(read_fd, buffer_size=0)


def test_rust_consume_stream_is_annotated_not_integrated() -> None:
    """``rust_consume_stream`` is documented as implemented-but-not-integrated.

    ADR-002 defers consume-side dispatch to evidence-gated Phase 2 work
    (issue #127). This guard keeps the annotation and reality in lockstep:
    the docstring must carry the not-integrated marker, and no production
    module may reference the symbol. Wiring a ``_consume_stream_dispatch``
    later must remove both the marker and this guard together.
    """
    from cuprum import _streams_rs

    docstring = _streams_rs.rust_consume_stream.__doc__ or ""
    assert "not yet integrated" in docstring, (
        "rust_consume_stream must declare its not-integrated status"
    )

    module_file = _streams_rs.__file__
    assert module_file is not None, "_streams_rs must be a file-backed module"
    package_root = pathlib.Path(module_file).parent
    referencing = sorted(
        path.name
        for path in package_root.rglob("*.py")
        if "unittests" not in path.parts
        and path.name != "_streams_rs.py"
        and _module_references_symbol(path, "rust_consume_stream")
    )
    assert referencing == [], (
        "production code now references rust_consume_stream; revisit the "
        f"ADR-002 Phase 2 decision and this guard: {referencing}"
    )
