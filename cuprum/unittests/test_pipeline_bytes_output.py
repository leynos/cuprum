"""Unit tests for the byte-exact ``Pipeline.run_bytes()`` path.

A pipeline relays one stage's stdout into the next stage's stdin, so the
question a byte-exact pipeline has to answer is not only "are the captures
bytes" but "did the payload reach the far end untouched". The stages here
thread a payload no decoder can round-trip — every byte value plus an invalid
UTF-8 sequence — from a producer, through a relay stage, to the capture, and
every assertion checks the bytes that survived the trip.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import Program, ScopeConfig, TimeoutExpired, scoped, sh
from cuprum.sh import (
    BytesCommandResult,
    BytesPipelineResult,
    ExecutionContext,
    Pipeline,
    PipelineResult,
    RunOutputOptions,
)
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent

# Every byte value, then a lone continuation byte, a byte no UTF-8 sequence
# starts with, and a NUL. A pipeline that decoded anywhere along the way —
# relaying, capturing, or fanning out to a sink — would replace the invalid
# ones, so each stream's check can tell the modes apart.
_BYTE_RANGE = bytes(range(256))
_INVALID_TAIL = b"\xff\x00\xfe\x80"


def _payload_for(tag: bytes) -> bytes:
    """Build a distinct byte-exact payload for one stream of one stage."""
    return tag + _BYTE_RANGE + _INVALID_TAIL


_PRODUCER_STDOUT = _payload_for(b"producer-stdout")
_PRODUCER_STDERR = _payload_for(b"producer-stderr")
_CONSUMER_STDERR = _payload_for(b"consumer-stderr")

_PRODUCER = (
    "import sys;"
    f"sys.stdout.buffer.write({_PRODUCER_STDOUT!r});"
    f"sys.stderr.buffer.write({_PRODUCER_STDERR!r})"
)
_CONSUMER = (
    "import sys;"
    "sys.stdout.buffer.write(sys.stdin.buffer.read());"
    f"sys.stderr.buffer.write({_CONSUMER_STDERR!r})"
)


def _relay_pipeline() -> tuple[Pipeline, frozenset[Program]]:
    """Build a two-stage pipeline relaying binary stdout to the capture."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    return (
        python("-c", _PRODUCER) | python("-c", _CONSUMER),
        frozenset([python_program]),
    )


def _binary_pipeline() -> tuple[Pipeline, frozenset[Program]]:
    """Build a two-stage pipeline writing one binary payload per stream."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    producer = python("-c", _PRODUCER)
    consumer = python("-c", "import sys; sys.stdin.buffer.read()")
    return producer | consumer, frozenset([python_program])


def test_pipeline_run_bytes_relays_binary_stdout_to_the_capture() -> None:
    """The final capture is the producer's bytes, relayed undecoded."""
    pipeline, allowlist = _relay_pipeline()

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = pipeline.run_bytes_sync()

    assert isinstance(result, BytesPipelineResult), (
        f"run_bytes must report a BytesPipelineResult, got {type(result).__name__}"
    )
    assert result.ok is True, "every stage should exit cleanly"
    assert result.stdout == _PRODUCER_STDOUT, (
        "the relayed final stdout must be the producer's bytes unchanged, got "
        f"{result.stdout!r}"
    )
    assert result.stages[0].stdout is None, (
        "an intermediate stage's stdout is consumed by the next stage rather "
        f"than captured, got {result.stages[0].stdout!r}"
    )


def test_pipeline_run_bytes_captures_each_stage_stderr_as_bytes() -> None:
    """Every stage's stderr is captured in its own mode, per stage."""
    pipeline, allowlist = _binary_pipeline()

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = pipeline.run_bytes_sync()

    assert result.stages[0].stderr == _PRODUCER_STDERR, (
        f"the first stage's stderr must be its own bytes, got "
        f"{result.stages[0].stderr!r}"
    )
    assert result.stages[1].stderr == b"", (
        f"a quiet stage's captured stderr is empty bytes, got "
        f"{result.stages[1].stderr!r}"
    )
    assert all(isinstance(stage, BytesCommandResult) for stage in result.stages), (
        f"every stage must be a BytesCommandResult, got {result.stages!r}"
    )


def test_pipeline_run_bytes_matches_the_async_entry_point() -> None:
    """The synchronous entry point performs the same run as the asynchronous one."""
    pipeline, allowlist = _relay_pipeline()

    with scoped(ScopeConfig(allowlist=allowlist)):
        sync_result = pipeline.run_bytes_sync()
        async_result = asyncio.run(pipeline.run_bytes())

    assert sync_result.stdout == async_result.stdout == _PRODUCER_STDOUT, (
        "both entry points must relay the same bytes, got "
        f"sync={sync_result.stdout!r} async={async_result.stdout!r}"
    )


def test_pipeline_text_mode_is_unchanged_beside_the_byte_exact_entry_point() -> None:
    """``run()`` keeps decoding and keeps returning the text result classes."""
    pipeline, allowlist = _relay_pipeline()

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = pipeline.run_sync()

    assert type(result) is PipelineResult, (
        f"run() must keep reporting a PipelineResult, got {type(result).__name__}"
    )
    assert type(result.stages[0]) is not BytesCommandResult, (
        "a text-mode pipeline must not report byte-exact stages"
    )
    assert result.stdout == _PRODUCER_STDOUT.decode("utf-8", errors="replace"), (
        f"run() must keep decoding the relayed payload, got {result.stdout!r}"
    )


def test_pipeline_run_bytes_rejects_line_observation_before_spawning() -> None:
    """``on_line`` is refused up front, exactly as the command path refuses it."""
    pipeline, allowlist = _relay_pipeline()
    observed: list[typ.Any] = []

    with (
        scoped(ScopeConfig(allowlist=allowlist)),
        pytest.raises(ValueError, match="on_line"),
    ):
        pipeline.run_bytes_sync(output=RunOutputOptions(on_line=observed.append))

    assert not observed, "the rejection must happen before any line is observed"


def test_pipeline_run_bytes_reports_timeout_stderr_in_stage_order() -> None:
    """A timed-out run surfaces each stage's partial stderr, concatenated."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    first_stderr = b"first-stage\xff"
    second_stderr = b"second-stage\xfe"
    pipeline = python(
        "-c",
        "import sys, time;"
        f"sys.stderr.buffer.write({first_stderr!r}); sys.stderr.flush();"
        "time.sleep(30)",
    ) | python(
        "-c",
        "import sys, time;"
        f"sys.stderr.buffer.write({second_stderr!r}); sys.stderr.flush();"
        "sys.stdin.read(); time.sleep(30)",
    )

    with (
        scoped(ScopeConfig(allowlist=frozenset([python_program]))),
        pytest.raises(TimeoutExpired) as exc_info,
    ):
        pipeline.run_bytes_sync(timeout=2.0)

    expired = exc_info.value
    assert expired.stderr == first_stderr + second_stderr, (
        "the partial stderr must concatenate every stage's bytes in stage "
        f"order, got {expired.stderr!r}"
    )


def test_pipeline_run_bytes_leaves_streams_unset_without_capture() -> None:
    """``capture=False`` reports ``None`` for the pipeline's streams too."""
    pipeline, allowlist = _relay_pipeline()

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = pipeline.run_bytes_sync(output=RunOutputOptions(capture=False))

    assert result.stdout is None, f"stdout must stay unset, got {result.stdout!r}"
    assert result.stages[0].stderr is None, (
        f"a non-capturing stage's stderr must stay unset, got "
        f"{result.stages[0].stderr!r}"
    )


def test_pipeline_run_bytes_survives_an_observe_hook() -> None:
    """A registered observe hook must not cost a byte run its exactness.

    Observe hooks supply an internal line sink the caller never asked for, so
    the pipeline must still relay and capture the child's own bytes while the
    observer receives decoded lines — the two travel on separate channels.

    Both halves are asserted, not merely that some event arrived: truthiness
    would pass against a hook handed the wrong stream, or an undecoded one.
    The observer's lines are compared with an independently derived oracle, so
    the assertion fails if observation is dropped or fed the capture's bytes.
    """
    pipeline, allowlist = _relay_pipeline()
    observed: list[tuple[str, str]] = []

    def hook(event: ExecEvent) -> None:
        """Record the stream and decoded line the run publishes."""
        if event.line is not None:
            observed.append((event.phase, event.line))

    with scoped(ScopeConfig(allowlist=allowlist)), sh.observe(hook):
        result = pipeline.run_bytes_sync()

    assert result.stdout == _PRODUCER_STDOUT, (
        f"an observe hook must not decode the relayed payload, got {result.stdout!r}"
    )

    oracle = _PRODUCER_STDOUT.decode("utf-8", errors="replace").splitlines()
    assert [line for phase, line in observed if phase == "stdout"] == oracle, (
        "the observer must receive the producer's decoded stdout lines, got "
        f"{observed!r}"
    )


def test_pipeline_run_bytes_survives_a_strict_observer() -> None:
    """An observer's decode cannot end a strict byte run of a pipeline.

    The producer's payload ends in invalid UTF-8, so an observer decoder that
    honoured ``errors="strict"`` would raise from the read loop and cost the
    pipeline its capture. The observer renders a view, so the run must return
    the relayed bytes intact while the view still reaches the hook.
    """
    pipeline, allowlist = _relay_pipeline()
    observed: list[tuple[str, str]] = []

    def hook(event: ExecEvent) -> None:
        """Record each decoded line, which must not raise on the invalid tail."""
        if event.line is not None:
            observed.append((event.phase, event.line))

    with scoped(ScopeConfig(allowlist=allowlist)), sh.observe(hook):
        result = pipeline.run_bytes_sync(
            context=ExecutionContext(errors="strict"),
        )

    assert result.stdout == _PRODUCER_STDOUT, (
        "a strict pipeline byte run must survive an ambient observer, got "
        f"{result.stdout!r}"
    )

    oracle = _PRODUCER_STDOUT.decode("utf-8", errors="replace").splitlines()
    assert [line for phase, line in observed if phase == "stdout"] == oracle, (
        "the observer must render the replacement view under a strict capture "
        f"policy, got {observed!r}"
    )
