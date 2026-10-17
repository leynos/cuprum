"""What the byte-exact result classes report about their captures.

The text result classes report a ``captures`` tuple; the byte-exact ones must
too, and for a sharper reason. A byte run hands the caller a payload with no
decoded form to compare against, so a bounded run's shortened payload and a
spilled run's file-read-back look exactly like a complete capture. Only the
record can say a policy discarded bytes, moved the stream to a file, or failed
to retain it at all — and the changelog promises that a caller of the
file-backed policy "cannot be handed anything less" without being told.

Every case therefore asserts on the record, not only on the payload, and each
pairs with the payload assertion that shows the record is describing the same
run. The child writes a payload shorter than the bound, so a bounded case that
asserted only "some bytes came back" would pass with the whole stream retained.
"""

from __future__ import annotations

import typing as typ

from cuprum import ScopeConfig, scoped, sh
from cuprum.capture_policy import BoundedCapture, FileBackedCapture
from cuprum.sh import (
    BytesCommandResult,
    BytesPipelineResult,
    Pipeline,
    RunOutputOptions,
)
from tests.helpers.catalogue import build_python_catalogue_env, python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from cuprum.sh import SafeCmd

# Longer than the bound below, and distinct per stream, so a run that reported
# one stream's record for the other fails rather than satisfying both.
_STDOUT_TEXT = "stdout-payload-0123456789"
_STDERR_TEXT = "stderr-payload-0123456789"
_BOUND = 8
_WRITE_BOTH = (
    f"import sys;sys.stdout.write({_STDOUT_TEXT!r});sys.stderr.write({_STDERR_TEXT!r})"
)


def _builder() -> cabc.Callable[..., SafeCmd]:
    """Return a builder for the interpreter this suite runs under."""
    return build_python_catalogue_env().builder


def _bounded_options() -> RunOutputOptions:
    """Bound stdout and capture stderr in full, so the two records differ."""
    return RunOutputOptions(
        capture_stdout=BoundedCapture(max_bytes=_BOUND),
        capture_stderr=True,
    )


def test_a_bounded_byte_run_reports_the_dropped_count_beside_the_tail() -> None:
    """The record is the only thing that says the byte payload was shortened.

    The retained payload is the stream's tail either way; a byte-exact run must
    additionally report how much went missing, in the same units the payload
    uses. Asserting the count as well as the tail is what keeps a record that
    merely named the policy from passing.
    """
    cmd = _builder()("-c", _WRITE_BOTH)

    result = cmd.run_bytes_sync(output=_bounded_options())

    assert isinstance(result, BytesCommandResult), (
        f"run_bytes must report a BytesCommandResult, got {type(result).__name__}"
    )
    assert result.stdout == _STDOUT_TEXT[-_BOUND:].encode(), (
        f"the payload must be the bounded tail; got {result.stdout!r}"
    )
    record = result.captures[0]
    assert record.stream == "stdout", (
        f"the first record must be stdout's; got {record.stream!r}"
    )
    assert record.total_bytes == len(_STDOUT_TEXT.encode()), (
        "the record must count every byte the child wrote, not only the tail; "
        f"got {record.total_bytes}"
    )
    assert record.dropped_bytes == len(_STDOUT_TEXT.encode()) - _BOUND, (
        f"the record must report the discarded bytes; got {record.dropped_bytes}"
    )
    assert record.truncated is True, (
        "a bounded capture that discarded bytes must report truncation"
    )
    assert record.storage == "memory", "a bounded capture retains in memory"


def test_a_full_stderr_record_sits_beside_the_bounded_stdout_record() -> None:
    """Two streams with two policies report two independent records."""
    cmd = _builder()("-c", _WRITE_BOTH)

    result = cmd.run_bytes_sync(output=_bounded_options())

    assert [record.stream for record in result.captures] == ["stdout", "stderr"], (
        "both captured streams must report a record, in stream order; got "
        f"{[record.stream for record in result.captures]!r}"
    )
    full = result.captures[1]
    assert full.policy == "full", f"stderr was captured in full; got {full.policy!r}"
    assert full.dropped_bytes == 0, (
        f"a full capture discards nothing; got {full.dropped_bytes}"
    )
    assert result.stderr == _STDERR_TEXT.encode(), (
        f"the full stream must arrive whole; got {result.stderr!r}"
    )


def test_a_spilled_byte_run_reports_the_file_it_read_the_bytes_from(
    tmp_path: pathlib.Path,
) -> None:
    """A byte run's spilled record names the file, and its payload came from it.

    The bytes arrive in ``stdout`` either way, so only ``storage`` and ``path``
    distinguish a spill from a heap capture. ``keep_file=True`` keeps the file
    readable so this case can compare the record's locator against the payload.
    """
    cmd = _builder()("-c", _WRITE_BOTH)
    options = RunOutputOptions(
        capture_stdout=FileBackedCapture(
            spill_threshold_bytes=1,
            directory=tmp_path,
            keep_file=True,
        ),
        capture_stderr=False,
    )

    result = cmd.run_bytes_sync(output=options)

    assert result.stdout == _STDOUT_TEXT.encode(), (
        f"a spilled byte capture must still return the bytes; got {result.stdout!r}"
    )
    record = result.captures[0]
    assert record.storage == "file", (
        f"a spilled capture must report file storage; got {record.storage!r}"
    )
    assert record.path is not None, "a spilled capture must name its file"
    assert record.path.read_bytes() == _STDOUT_TEXT.encode(), (
        "the reported path must be the file the bytes came from"
    )
    assert record.owned is False, (
        "a kept spill belongs to the caller, who must read it or remove it"
    )


def test_a_byte_pipeline_reports_the_final_stage_captures() -> None:
    """``BytesPipelineResult.captures`` reads the final stage, like ``stdout``.

    Interior stages relay their stdout rather than retaining it, so the
    aggregate's records must be the final stage's — the same stage its
    ``stdout`` property reads. A pipeline that reported an interior stage's
    records would describe a stream no caller can see.
    """
    catalogue, program = python_catalogue()
    python = sh.make(program, catalogue=catalogue)
    producer = python("-c", "pass")
    # The final stage both writes and reads, so its own policy is the one the
    # aggregate must report rather than the producer's relayed stream.
    pipeline: Pipeline = producer | python(
        "-c", "import sys; sys.stdin.read(); " + _WRITE_BOTH
    )

    with scoped(ScopeConfig(allowlist=frozenset({program}))):
        result = pipeline.run_bytes_sync(output=_bounded_options())

    assert isinstance(result, BytesPipelineResult), (
        f"run_bytes must report a BytesPipelineResult, got {type(result).__name__}"
    )
    assert result.captures == result.final.captures, (
        "the aggregate's records must be the final stage's, not a copy that "
        f"could drift; got {result.captures!r} vs {result.final.captures!r}"
    )
    assert [record.stream for record in result.captures], (
        "the final stage captured both streams, so it must report records"
    )
    assert any(record.dropped_bytes for record in result.captures), (
        "the bounded stdout policy must show in the final stage's records; got "
        f"{result.captures!r}"
    )


def test_a_byte_run_with_disabled_capture_reports_no_records() -> None:
    """A byte run that captured nothing reports nothing, as text mode does."""
    cmd = _builder()("-c", _WRITE_BOTH)

    result = cmd.run_bytes_sync(output=RunOutputOptions(capture=False))

    assert result.captures == (), (
        f"capture=False must report no records; got {result.captures!r}"
    )
