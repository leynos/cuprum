"""Ordering of descriptor acquisition, draining, and native submission.

The native hand-off acquires its worker descriptors before the first await so a
descriptor number cannot be reused underneath it, but blocking activation and
executor submission stay after the buffered drain. These tests pin that split.
"""

from __future__ import annotations

import asyncio
import os
import typing as typ

import pytest

from cuprum import (
    _pipeline_stream_fds,
    _pipeline_stream_native_cleanup,
    _pipeline_streams,
)
from cuprum.unittests._pump_stream_dispatch_support import (
    _run_with_inline_executor,
    clear_backend_caches,
)

__all__ = ["clear_backend_caches"]

pytestmark = pytest.mark.usefixtures("clear_backend_caches")


class TestAcquisitionOrder:
    """Ordering tests for the native pump hand-off."""

    def test_run_rust_pump_acquires_before_draining(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rust pumping should acquire descriptors before the first await."""
        call_order: list[str] = []

        def fake_pause_reader_transport(
            reader: asyncio.StreamReader,
        ) -> _pipeline_stream_fds._ReaderPause:
            """Record a pause and return a resume callback for the reader."""
            del reader
            call_order.append("pause")

            def _resume() -> None:
                """Record that the reader transport was resumed."""
                call_order.append("resume")

            return _pipeline_stream_fds._ReaderPause(
                resume=_resume,
            )

        original_open = _pipeline_stream_native_cleanup._open_native_pump_worker_fds

        def record_acquisition(
            **kwargs: int,
        ) -> _pipeline_stream_fds._NativePumpWorkerFds | None:
            """Record the synchronous acquisition before delegating."""
            call_order.append("acquire")
            return original_open(**kwargs)

        async def fake_drain_reader_buffer(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter | None,
        ) -> None:
            """Record that the reader buffer was drained."""
            del reader, writer
            await asyncio.sleep(0)
            call_order.append("drain")

        def record_engage(**_kwargs: object) -> tuple[bool, bool]:
            """Record blocking-mode activation of the acquired pair."""
            call_order.append("engage")
            return True, True

        monkeypatch.setattr(
            _pipeline_streams,
            "_pause_reader_transport",
            fake_pause_reader_transport,
        )
        monkeypatch.setattr(
            _pipeline_stream_native_cleanup,
            "_open_native_pump_worker_fds",
            record_acquisition,
        )
        monkeypatch.setattr(
            _pipeline_streams,
            "_drain_reader_buffer",
            fake_drain_reader_buffer,
        )
        monkeypatch.setattr(
            _pipeline_stream_fds,
            "_set_stream_fds_blocking",
            record_engage,
        )
        monkeypatch.setattr(
            _pipeline_stream_fds,
            "_restore_stream_fd_blocking",
            lambda **_: call_order.append("restore"),
        )

        import cuprum._streams_rs as streams_rs

        def consuming_rust_pump(reader_fd: int, writer_fd: int) -> int:
            """Consume the duplicate writer FD exactly as the Rust pump does."""
            del reader_fd
            call_order.append("submit")
            os.close(writer_fd)
            return 0

        monkeypatch.setattr(streams_rs, "rust_pump_stream", consuming_rust_pump)

        reader = typ.cast("asyncio.StreamReader", object())
        asyncio.run(
            _run_with_inline_executor(
                _pipeline_streams._run_rust_pump(
                    reader=reader,
                    writer=None,
                    handoff=_pipeline_streams._RustPumpHandoff(
                        reader_fd=1,
                        writer_fd=2,
                        cleanup_grace_s=0.5,
                    ),
                )
            )
        )

        assert call_order == [
            "pause",
            "acquire",
            "drain",
            "engage",
            "submit",
            "restore",
            "resume",
        ], (
            "acquisition must precede the first await, while blocking activation "
            "and submission stay after the buffered drain"
        )

    def test_suspended_drain_does_not_submit_native_work(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A drain held open must not have submitted native work yet."""
        drain_started = asyncio.Event()
        release_drain = asyncio.Event()
        submitted: list[int] = []

        async def held_drain(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter | None,
        ) -> None:
            """Hold the drain open until the test releases it."""
            del reader, writer
            drain_started.set()
            await release_drain.wait()

        def record_submission(reader_fd: int, writer_fd: int) -> int:
            """Record a native submission that must wait for the drain."""
            del reader_fd
            submitted.append(writer_fd)
            os.close(writer_fd)
            return 0

        monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", held_drain)

        import cuprum._streams_rs as streams_rs

        monkeypatch.setattr(streams_rs, "rust_pump_stream", record_submission)

        async def exercise() -> None:
            """Start the pump, observe the held drain, then release it."""
            task = asyncio.create_task(
                _run_with_inline_executor(
                    _pipeline_streams._run_rust_pump(
                        reader=typ.cast("asyncio.StreamReader", object()),
                        writer=None,
                        handoff=_pipeline_streams._RustPumpHandoff(
                            reader_fd=1,
                            writer_fd=2,
                            cleanup_grace_s=0.5,
                        ),
                    )
                )
            )
            await asyncio.wait_for(drain_started.wait(), timeout=1.0)
            assert not submitted, (
                "native work must not be submitted while the drain is suspended"
            )
            release_drain.set()
            await task

        asyncio.run(exercise())

        assert len(submitted) == 1, "the released drain must submit native work once"
