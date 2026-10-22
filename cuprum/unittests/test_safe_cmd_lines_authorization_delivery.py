"""Deadline and delivery cleanup deferred until ``lines()`` is pulled.

The deadline half of #543: a stream prepared in one scope must be bounded by
the scope that consumes it, and a denial must not leave the bounded-delivery
worker the preparation started parked until the interpreter exits.
"""

from __future__ import annotations

import asyncio
import io
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped
from cuprum.unittests._safe_cmd_lines_authorization_support import (
    _line_stream_delivery,
    _wait_for_delivery_threads_to_stop,
)
from tests.helpers.catalogue import build_python_catalogue_env
from tests.helpers.timeouts import child_argv

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum._bounded_delivery import _DeliveryDestination
    from cuprum.echo_events import EchoStream
    from cuprum.lines import LineEvent
    from cuprum.unittests._safe_cmd_lines_authorization_support import (
        _SpawnRecorder,
    )


def test_lines_stale_preparation_cannot_outlive_the_consuming_deadline(
    tmp_path: Path,
    spawn_recorder: _SpawnRecorder,
) -> None:
    """A stream prepared with no deadline cannot run unbounded when consumed.

    This is the ticket's timeout half end to end. The stream is built in a
    permissive scope with no timeout, then iterated in a short-deadline scope
    whose child blocks indefinitely. The run must end by that deadline, and
    the error must report it — had the prepared record been used, the run
    would have had no deadline at all and the test could only fail by hanging.
    """
    from cuprum.sh import TimeoutExpired

    runtime = build_python_catalogue_env()
    command = runtime.builder(*child_argv(tmp_path / "child-ready"))

    async def consume() -> float:
        """Iterate under a short deadline and return the reported timeout."""
        stream = command.lines()
        with (
            scoped(
                ScopeConfig(allowlist=frozenset([runtime.program]), timeout=0.05),
            ),
            pytest.raises(TimeoutExpired) as expiry,
        ):
            async for _event in stream:
                pass
        return expiry.value.timeout

    with scoped(ScopeConfig(allowlist=frozenset([runtime.program]))):
        reported = asyncio.run(consume())

    assert reported == pytest.approx(0.05), (
        "the timeout report must carry the consuming scope's deadline, got "
        f"{reported!r}"
    )
    assert len(spawn_recorder.executions) == 1, (
        "a timed-out run must still have spawned exactly once"
    )
    assert spawn_recorder.executions[0].timeout == pytest.approx(0.05), (
        "the spawn must receive the consuming scope's deadline, got "
        f"{spawn_recorder.executions[0].timeout!r}"
    )


def test_lines_denial_closes_a_prepared_delivery_worker(
    tmp_path: Path,
    spawn_recorder: _SpawnRecorder,
) -> None:
    """A denial tears down the delivery worker the preparation already built.

    Preparing an execution with ``BoundedDelivery`` starts a daemon thread
    before the stream is pulled. A denial happens before any coordinator
    exists to close it, so the refusal path itself must — otherwise every
    denied pull parks a thread until the interpreter exits.
    """
    from cuprum.context import ForbiddenProgramError

    runtime = build_python_catalogue_env()
    command = runtime.builder(*child_argv(tmp_path / "child-ready"))
    output, context = _line_stream_delivery(io.StringIO())

    async def consume() -> None:
        """Pull a bounded-delivery stream inside a deny-all scope."""
        stream = command.lines(output=output, context=context)
        with (
            scoped(ScopeConfig(allowlist=frozenset())),
            pytest.raises(ForbiddenProgramError),
        ):
            async for _event in stream:
                pass
        assert await _wait_for_delivery_threads_to_stop(), (
            "a denied stream must close the delivery worker it prepared"
        )

    with scoped(ScopeConfig(allowlist=frozenset([runtime.program]))):
        asyncio.run(consume())

    assert not spawn_recorder.executions, (
        "the delivery worker must have been closed before any spawn, got "
        f"{spawn_recorder.executions!r}"
    )


def test_lines_delivery_worker_survives_an_allowed_run(
    tmp_path: Path,
    spawn_recorder: _SpawnRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An allowed run keeps its worker long enough to deliver, then closes it.

    The mirror of the denial case: the cleanup hook must not fire on a run
    that is still going to spawn. The worker is observed through the
    delivery module's own constructor, so the test sees the instance the run
    actually used rather than one it built itself.
    """
    import cuprum._bounded_delivery as bounded_delivery

    runtime = build_python_catalogue_env()
    command = runtime.builder("-c", "print('delivered line')")
    sink = io.StringIO()
    output, context = _line_stream_delivery(sink)
    workers: list[bounded_delivery._DeliveryWorker] = []
    original_worker = bounded_delivery._DeliveryWorker

    def record_worker(
        destinations: dict[EchoStream, _DeliveryDestination],
        max_queued_bytes: int,
    ) -> bounded_delivery._DeliveryWorker:
        """Record the created worker while preserving its behaviour."""
        worker = original_worker(destinations, max_queued_bytes)
        workers.append(worker)
        return worker

    monkeypatch.setattr(bounded_delivery, "_DeliveryWorker", record_worker)

    async def collect() -> list[LineEvent]:
        """Consume the stream to completion and return its lines."""
        stream = command.lines(output=output, context=context)
        return [event async for event in stream]

    with scoped(ScopeConfig(allowlist=frozenset([runtime.program]))):
        events = asyncio.run(collect())

    assert [event.text for event in events] == ["delivered line"], (
        f"the allowed run must deliver its line, got {events!r}"
    )
    assert len(workers) == 1, (
        f"an allowed run must create exactly one delivery worker, got {len(workers)}"
    )
    assert not workers[0]._thread.is_alive(), (
        "completing iteration must stop the delivery worker it created"
    )
    assert len(spawn_recorder.executions) == 1, (
        "the allowed run must reach the spawn exactly once"
    )
    assert sink.getvalue() == "delivered line\n", (
        f"the worker must have delivered the echoed line, got {sink.getvalue()!r}"
    )
