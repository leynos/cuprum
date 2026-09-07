"""Regression tests for Rust-pump descriptor ownership hand-off."""

from __future__ import annotations

import asyncio
import os
import typing as typ
from unittest import mock

import pytest

from cuprum import _pipeline_streams
from cuprum.unittests._pump_stream_dispatch_support import (
    _nonblocking_pipe_pair,
    _run_with_inline_executor_returning,
    bypass_reader_drain,
    clear_backend_caches,
    install_closing_rust_pump,
)

__all__ = ["clear_backend_caches"]

pytestmark = pytest.mark.usefixtures("clear_backend_caches")


def test_rust_pump_receives_a_duplicate_not_the_transport_fd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rust consumes a duplicate, leaving the transport descriptor intact.

    The double closes what it receives, as ``rust_pump_stream`` does, so
    handing over the transport's own descriptor would surface as ``EBADF``.
    """
    received = install_closing_rust_pump(monkeypatch)
    bypass_reader_drain(monkeypatch)

    with _nonblocking_pipe_pair() as (
        read_fd,
        read_write_fd,
        write_read_fd,
        write_fd,
    ):
        del read_write_fd, write_read_fd
        reader = typ.cast("asyncio.StreamReader", object())
        writer = mock.MagicMock(spec=asyncio.StreamWriter)
        writer.wait_closed = mock.AsyncMock()

        handled = asyncio.run(
            _run_with_inline_executor_returning(
                _pipeline_streams._run_rust_pump(
                    reader=reader,
                    writer=writer,
                    handoff=_pipeline_streams._RustPumpHandoff(
                        reader_fd=read_fd,
                        writer_fd=write_fd,
                        cleanup_grace_s=0.5,
                    ),
                )
            )
        )

        assert handled is True, "expected the native pump path to report success"
        assert received["writer_fd"] != write_fd, (
            "Rust must receive a duplicate, never the transport's descriptor"
        )
        try:  # the duplicate's close must not take the original with it
            os.fstat(write_fd)
        except OSError as exc:  # pragma: no cover - failure path only
            pytest.fail(
                f"transport writer FD must stay valid after the native "
                f"pump closed its duplicate, got {exc!r}"
            )
        assert writer.close.called, (
            "the asyncio writer must still be closed to signal EOF"
        )
