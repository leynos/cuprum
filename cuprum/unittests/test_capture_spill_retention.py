"""Whether a caller-owned spill survives each returning or raising route.

The ``keep_file`` contract is an ownership transfer after the first spill has
been written and promoted. These cases cover completed runs with either exit
status, timeout, and external cancellation after that promotion point.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import TimeoutExpired
from cuprum.capture_events import CaptureEvent, CaptureEventCategory
from cuprum.capture_observation import observe_capture
from cuprum.unittests._capture_spill_run_support import (
    _PAYLOAD_BYTES,
    _RUN_TIMEOUT_S,
    _SPILL_THEN_BLOCK,
    _SPILL_THEN_EXIT,
    _THRESHOLD,
    _await_witness,
    _builder,
    _only_spill,
    _options,
    _reported_path,
)
from cuprum.unittests._capture_witness_support import _SpillWitness

if typ.TYPE_CHECKING:
    import pathlib


@pytest.mark.parametrize("exit_code", [0, 3], ids=["success", "non-zero"])
def test_a_kept_spill_survives_a_completed_run_whole(
    tmp_path: pathlib.Path,
    exit_code: int,
) -> None:
    """A kept file survives success and non-zero completion with all bytes."""
    witness = _SpillWitness(tmp_path)
    with observe_capture(witness):
        result = _builder()("-c", _SPILL_THEN_EXIT.format(code=exit_code)).run_sync(
            output=_options(tmp_path, keep_file=True)
        )
    witness.awaited()

    record = _only_spill(result.captures)
    path = _reported_path(record)
    assert result.exit_code == exit_code, "the child's status must reach the caller"
    assert path.exists(), "a kept spill file must outlive the run that made it"
    contents = path.read_bytes()
    assert contents == b"z" * _PAYLOAD_BYTES, (
        "a kept file must hold the child's whole stream; "
        f"got {len(contents)} of {_PAYLOAD_BYTES} bytes"
    )
    assert record.owned is False, (
        "a handed-over file reports unowned: this run is not the one to remove it"
    )


def test_a_kept_spill_survives_a_timed_out_run_whole(
    tmp_path: pathlib.Path,
) -> None:
    """The keep exemption holds on the raising route, not just the returning one.

    The timeout path tears down the child, readers, and process group, and must
    still leave the flushed prefix in the file it was told to hand over.
    """
    witness = _SpillWitness(tmp_path)
    with observe_capture(witness), pytest.raises(TimeoutExpired):
        _builder()("-c", _SPILL_THEN_BLOCK).run_sync(
            timeout=_RUN_TIMEOUT_S,
            output=_options(tmp_path, keep_file=True),
        )

    witnessed = witness.awaited()
    assert len(witnessed) == 1, (
        "one run must leave exactly one spill file to hand over; "
        f"the witness saw {witnessed!r}"
    )
    path = tmp_path / witnessed[0]
    assert path.exists(), "a kept spill file must survive the timeout's teardown"
    contents = path.read_bytes()
    assert contents == b"z" * _PAYLOAD_BYTES, (
        f"a kept file must hold the flushed prefix in full; got {len(contents)} bytes"
    )


def test_a_kept_spill_survives_external_cancellation_after_promotion(
    tmp_path: pathlib.Path,
) -> None:
    """Cancellation after the spill event leaves the promoted file readable."""
    witness = _SpillWitness(tmp_path)
    events: list[CaptureEvent] = []

    def record_event(event: CaptureEvent) -> None:
        """Keep the event payload beside the directory witness."""
        witness(event)
        events.append(event)

    async def cancel_after_spill() -> tuple[str, ...]:
        """Cancel the public run as soon as its promoted spill is witnessed."""
        with observe_capture(record_event):
            run = asyncio.create_task(
                _builder()("-c", _SPILL_THEN_BLOCK).run(
                    timeout=300,
                    output=_options(tmp_path, keep_file=True),
                )
            )
            await _await_witness(witness)
            names = witness.awaited()
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run
        return names

    names = asyncio.run(cancel_after_spill())
    assert len(names) == 1, f"one run must create one spill file, found {names!r}"
    spill_events = [
        event
        for event in events
        if event.category is CaptureEventCategory.SPILLED_TO_FILE
    ]
    assert len(spill_events) == 1, (
        f"the run must publish one spill transition, found {spill_events!r}"
    )
    spilled_bytes = spill_events[0].spilled_bytes
    assert spilled_bytes is not None, "the spill event must report its byte count"
    assert spilled_bytes > _THRESHOLD, (
        f"the transition must witness bytes beyond the threshold; got {spilled_bytes}"
    )

    path = tmp_path / names[0]
    assert path.exists(), "a promoted kept spill must survive external cancellation"
    contents = path.read_bytes()
    assert contents == b"z" * spilled_bytes, (
        "the kept file must contain the exact prefix accepted at promotion; "
        f"got {len(contents)} bytes, event reported {spilled_bytes}"
    )
