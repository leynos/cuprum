"""Generated pipeline slot models cover observation and settlement separately."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum._pipeline_stream_results import _bounded_payloads, _pipeline_payloads
from cuprum._stream_drain_finish import _CapturedDrain
from cuprum._subprocess_wait_types import _DrainContext
from cuprum.capture_policy import BoundedCapture, FileBackedCapture, FullCapture

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._subprocess_wait_types import _StreamConsumerTask
    from cuprum.capture_policy import CapturePolicy

_STATES = st.sampled_from(["absent", "done", "pending", "failed", "cancelled"])
_SETTLED = st.sampled_from(["payload", "missing", "failed", "cancelled"])
_POLICIES = st.sampled_from(["none", "full", "bounded", "file_backed"])


async def _reader(
    state: str,
    drain: _CapturedDrain,
    gate: asyncio.Event,
    failure: RuntimeError,
) -> _CapturedDrain:
    """Materialize one generated reader state without real I/O."""
    if state == "failed":
        raise failure
    if state == "pending":
        await gate.wait()
    return drain


@dc.dataclass
class _ReaderCase:
    """The generated readers and their independent expected outcomes."""

    states: list[str]
    tasks: list[_StreamConsumerTask | None] = dc.field(default_factory=list)
    drains: list[_CapturedDrain] = dc.field(default_factory=list)
    failures: list[RuntimeError] = dc.field(default_factory=list)
    gate: asyncio.Event = dc.field(default_factory=asyncio.Event)

    def populate(self, *, capture_bytes: bool) -> None:
        """Start every present reader in the generated state."""
        for index, state in enumerate(self.states):
            self.drains.append(
                _CapturedDrain(
                    payload=str(index).encode() if capture_bytes else str(index)
                )
            )
            self.failures.append(RuntimeError(f"reader-{index}"))
            task = (
                None
                if state == "absent"
                else asyncio.create_task(
                    _reader(state, self.drains[-1], self.gate, self.failures[-1]),
                )
            )
            if task is not None and state == "cancelled":
                task.cancel()
            self.tasks.append(task)

    async def close(self) -> None:
        """Reclaim every reader even after a model assertion fails."""
        present = [task for task in self.tasks if task is not None]
        for task in present:
            task.cancel()
        await asyncio.gather(*present, return_exceptions=True)


def _expected_slots(
    case: _ReaderCase, deadline_mode: str
) -> tuple[_CapturedDrain | None, ...]:
    """Model the observable slots from state labels, independently of tasks."""
    return tuple(
        None
        if state == "absent" or (state == "pending" and deadline_mode == "elapsed")
        else case.drains[index]
        for index, state in enumerate(case.states)
    )


async def _assert_reader_model(case: _ReaderCase, deadline_mode: str) -> None:
    """Compare an observation with either its first failure or its slot model."""
    deadline = {"none": None, "elapsed": 99.0, "remaining": 101.0}[deadline_mode]
    errors = [
        index
        for index, state in enumerate(case.states)
        if state in {"failed", "cancelled"}
    ]
    if errors:
        with pytest.raises((RuntimeError, asyncio.CancelledError)) as caught:
            await _bounded_payloads(
                case.tasks, deadline=deadline, monotonic_clock=lambda: 100.0
            )
        first = errors[0]
        if case.states[first] == "failed":
            assert caught.value is case.failures[first], (
                "the reader's own failure must survive"
            )
        else:
            assert isinstance(caught.value, asyncio.CancelledError)
        return
    observed, finished = await _bounded_payloads(
        case.tasks, deadline=deadline, monotonic_clock=lambda: 100.0
    )
    assert len(observed) == len(case.states), (
        "absent slots must not shift later readers"
    )
    assert all(
        actual is model
        for actual, model in zip(
            observed, _expected_slots(case, deadline_mode), strict=True
        )
    )
    assert finished is not ("pending" in case.states and deadline_mode == "elapsed")
    if not finished:
        assert all(task is None or not task.cancelled() for task in case.tasks), (
            "the deadline observes pending readers without cancelling them"
        )


@given(
    states=st.lists(_STATES, max_size=8),
    deadline_mode=st.sampled_from(["none", "elapsed", "remaining"]),
    capture_bytes=st.booleans(),
)
def test_bounded_payloads_matches_the_completed_slot_model(
    states: list[str],
    deadline_mode: str,
    *,
    capture_bytes: bool,
) -> None:
    """Only completed readers supply slots; expiry never cancels pending readers."""

    async def drive() -> None:
        """Compare a generated reader scenario and then reclaim its tasks."""
        case = _ReaderCase(states)
        case.populate(capture_bytes=capture_bytes)
        await asyncio.sleep(0)
        if deadline_mode != "elapsed":
            asyncio.get_running_loop().call_soon(case.gate.set)
        try:
            await _assert_reader_model(case, deadline_mode)
        finally:
            await case.close()

    asyncio.run(drive())


def _policy(tag: str) -> CapturePolicy | None:
    """Materialize the generated capture policy without opening a spill file."""
    return {
        "none": None,
        "full": FullCapture(),
        "bounded": BoundedCapture(max_bytes=8),
        "file_backed": FileBackedCapture(),
    }[tag]


@dc.dataclass
class _SettlementCase:
    """A task mapping and the model describing each expected positional result."""

    tasks: list[_StreamConsumerTask | None] = dc.field(default_factory=list)
    observed: list[_CapturedDrain | None] = dc.field(default_factory=list)
    settled: dict[_StreamConsumerTask, _CapturedDrain | BaseException | None] = (
        dc.field(default_factory=dict)
    )
    expected: list[_CapturedDrain | str | None] = dc.field(default_factory=list)


def _slot_drains(
    index: int, *, capture_bytes: bool
) -> tuple[_CapturedDrain, _CapturedDrain]:
    """Give observation and settlement distinct identifiable output records."""
    values = [f"observed-{index}", f"settled-{index}"]
    before, after = [
        _CapturedDrain(payload=value.encode() if capture_bytes else value)
        for value in values
    ]
    return before, after


def _settlement_case(
    slots: list[tuple[bool, bool, str]], *, capture_bytes: bool
) -> _SettlementCase:
    """Build a generated slot map while recording the independent expected source."""
    case = _SettlementCase()
    for index, (present, has_observed, state) in enumerate(slots):
        if not present:
            case.tasks.append(None)
            case.observed.append(None)
            case.expected.append(None)
            continue
        before, after = _slot_drains(index, capture_bytes=capture_bytes)
        task = asyncio.create_task(asyncio.sleep(0, result=after))
        case.tasks.append(task)
        case.observed.append(before if has_observed else None)
        case.settled[task] = {
            "payload": after,
            "missing": None,
            "failed": RuntimeError("reader failed"),
            "cancelled": asyncio.CancelledError(),
        }[state]
        case.expected.append(
            before if has_observed else after if state == "payload" else "fallback"
        )
    return case


def _assert_settlement_model(
    actual: cabc.Sequence[_CapturedDrain | None],
    expected: cabc.Sequence[_CapturedDrain | str | None],
    policies: tuple[str, str],
    *,
    capture_bytes: bool,
) -> None:
    """Check source identity and the stream's own fallback policy and mode."""
    for index, (drain, model) in enumerate(zip(actual, expected, strict=True)):
        if model != "fallback":
            assert drain is model, "observed or settled records must retain identity"
            continue
        policy = policies[0 if index == len(expected) - 1 else 1]
        assert drain is not None, "a present consumer always owes a drain result"
        empty = b"" if capture_bytes else ""
        assert drain.payload == (None if policy == "none" else empty)
        if policy == "none":
            assert drain.capture is None
        else:
            assert drain.capture is not None
            assert drain.capture.policy.value == policy
            assert drain.capture.stream == (
                "stdout" if index == len(expected) - 1 else "stderr"
            )


@given(
    slots=st.lists(
        st.tuples(st.booleans(), st.booleans(), _SETTLED), min_size=1, max_size=9
    ),
    stdout_policy=_POLICIES,
    stderr_policy=_POLICIES,
    capture_bytes=st.booleans(),
)
def test_pipeline_payloads_preserves_observed_results_and_decodes_each_stream(
    slots: list[tuple[bool, bool, str]],
    stdout_policy: str,
    stderr_policy: str,
    *,
    capture_bytes: bool,
) -> None:
    """The positional model gives observed output precedence over settlement."""

    async def drive() -> None:
        """Compare every generated slot with its independent stream contract."""
        case = _settlement_case(slots, capture_bytes=capture_bytes)
        context = _DrainContext(
            captures=(_policy(stdout_policy), _policy(stderr_policy)),
            capture_bytes=capture_bytes,
        )
        try:
            stderr, stdout = _pipeline_payloads(
                case.observed, case.tasks, case.settled, context
            )
            assert len(stderr) == len(slots) - 1, "only the final slot indexes stdout"
            _assert_settlement_model(
                (*stderr, stdout),
                case.expected,
                (stdout_policy, stderr_policy),
                capture_bytes=capture_bytes,
            )
        finally:
            await asyncio.gather(*(task for task in case.tasks if task is not None))

    asyncio.run(drive())
