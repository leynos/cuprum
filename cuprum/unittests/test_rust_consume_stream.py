"""Unit tests for the Rust-backed stream consumer.

These tests validate ``rust_consume_stream``'s decoding, buffering, descriptor
ownership, and error behaviour. The pump entry point is covered separately in
``test_rust_streams.py``, and the exact `errno` the conversion must preserve is
pinned in ``test_rust_errno.py``.

Example
-------
pytest cuprum/unittests/test_rust_consume_stream.py
"""

from __future__ import annotations

import contextlib
import errno
import os
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cuprum.unittests._rust_stream_test_support import (
    INVALID_FD_ERRNOS,
    INVALID_FD_MESSAGE_RE,
    _safe_close,
)

if typ.TYPE_CHECKING:
    from types import ModuleType


#: Cap generated payloads so each example stays cheap: every call opens a pipe.
_MAX_PAYLOAD_BYTES = 4096

# Mixing encoded text with raw byte runs generates well-formed multibyte
# sequences, lone continuation bytes, and truncated sequences in one strategy.
_DECODE_PAYLOADS = st.lists(
    st.one_of(
        st.text(max_size=8).map(lambda chunk: chunk.encode("utf-8")),
        st.binary(max_size=8),
    ),
    max_size=16,
).map(lambda chunks: b"".join(chunks)[:_MAX_PAYLOAD_BYTES])

# Small buffers force multibyte sequences to straddle read boundaries; ``None``
# exercises the default buffer size.
_DECODE_BUFFER_SIZES = st.one_of(
    st.none(),
    st.integers(min_value=1, max_value=8),
    st.integers(min_value=9, max_value=1 << 16),
)

_CONSUME_SETTINGS = settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
    max_examples=50,
)


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

    # Every case here asserts the same property — the extension's output equals
    # Python's own replacement decoding — so they are one parameterized test
    # rather than four near-identical bodies. A `buffer_size` of ``None`` omits
    # the argument, exercising the extension's default.
    @pytest.mark.parametrize(
        ("test_id", "payload", "buffer_size"),
        [
            ("ascii_explicit_default", b"rust-consume-stream", 65536),
            ("multibyte_split", b"snowman \xe2\x98\x83", 2),
            ("implicit_default_buffer", b"rust-consume-default", None),
            ("invalid_bytes", b"valid-\xff\xfe-end", 3),
            ("incomplete_sequence", b"trail-\xe2\x98", 2),
        ],
        ids=[
            "ascii_explicit_default",
            "multibyte_split",
            "implicit_default_buffer",
            "invalid_bytes",
            "incomplete_sequence",
        ],
    )
    def test_decodes_payload(
        self,
        rust_streams: ModuleType,
        test_id: str,
        payload: bytes,
        buffer_size: int | None,
    ) -> None:
        """Validate rust_consume_stream decodes UTF-8 payloads.

        Parameters
        ----------
        rust_streams : ModuleType
            The Rust streams module fixture.
        test_id : str
            Test case identifier for parameterization.
        payload : bytes
            The payload to consume.
        buffer_size : int | None
            The buffer size to consume with; ``None`` omits the argument so the
            extension applies its own default.

        """
        output = self._consume(rust_streams, payload, buffer_size=buffer_size)
        expected = payload.decode("utf-8", errors="replace")
        assert output == expected, (
            f"expected decoded output to match Python replace semantics ({test_id})"
        )

    @_CONSUME_SETTINGS
    @given(payload=_DECODE_PAYLOADS, buffer_size=_DECODE_BUFFER_SIZES)
    def test_decodes_payload_like_python(
        self,
        rust_streams: ModuleType,
        payload: bytes,
        buffer_size: int | None,
    ) -> None:
        """``rust_consume_stream`` matches Python's UTF-8 ``replace`` decoding.

        The rows above pin the named examples; this generalizes the same
        relation across arbitrary payloads and buffer sizes, so multibyte
        sequences straddle read boundaries at sizes no fixed row happens to
        choose.
        """
        output = self._consume(rust_streams, payload, buffer_size=buffer_size)
        assert output == payload.decode("utf-8", errors="replace"), (
            "decoded output must match Python's UTF-8 replace semantics"
        )

    @staticmethod
    def test_does_not_close_fd(
        rust_streams: ModuleType,
    ) -> None:
        """Ensure rust_consume_stream does not close the underlying FD.

        Parameters
        ----------
        rust_streams : ModuleType
            The Rust streams module fixture.

        Raises
        ------
        OSError
            Propagated if probing the descriptor after consumption fails for an
            unexpected I/O reason.
        """
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
        """Verify rust_consume_stream rejects invalid buffer sizes.

        Parameters
        ----------
        rust_streams : ModuleType
            The Rust streams module fixture.

        """
        with contextlib.ExitStack() as stack:
            read_fd, write_fd = os.pipe()
            stack.callback(_safe_close, read_fd)
            stack.callback(_safe_close, write_fd)
            _safe_close(write_fd)
            with pytest.raises(ValueError, match="buffer_size"):
                rust_streams.rust_consume_stream(read_fd, buffer_size=0)

    @staticmethod
    def test_propagates_io_errors(
        rust_streams: ModuleType,
    ) -> None:
        """Verify rust_consume_stream raises OSError on I/O failure.

        Parameters
        ----------
        rust_streams : ModuleType
            The Rust streams module fixture.

        Notes
        -----
        The consume-side counterpart to
        ``test_rust_pump_stream_propagates_io_errors``: a read that cannot be
        performed surfaces as an ``OSError`` whose ``errno`` names an unusable
        descriptor. Either ``EBADF`` or ``EINVAL`` is accepted, because Windows
        reports an invalid handle rather than a bad POSIX descriptor, so the
        assertion holds wherever the entry points build.

        The Rust-side tests over ``consume_stream_files`` exercise the
        read-and-decode loop below the PyO3 boundary and so cannot observe this
        translation at all. ``test_rust_errno.py`` pins the stricter conversion
        contract — the exact POSIX number, ``strerror``, and subclass selection
        — and skips on Windows for that reason; this test stays platform
        neutral and lives beside the other consume behaviour.
        """
        with contextlib.ExitStack() as stack:
            read_fd, write_fd = os.pipe()
            stack.callback(_safe_close, read_fd)
            stack.callback(_safe_close, write_fd)
            _safe_close(read_fd)
            with pytest.raises(
                OSError,
                match=INVALID_FD_MESSAGE_RE,
            ) as excinfo:
                rust_streams.rust_consume_stream(read_fd)
        assert excinfo.value.errno in INVALID_FD_ERRNOS, (
            "expected errno to indicate an invalid file descriptor/handle, "
            f"found {excinfo.value.errno!r}"
        )
