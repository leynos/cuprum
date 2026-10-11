"""Every stdio failure boundary, classified by a real run rather than a stub.

``cuprum.unittests.test_stdio_diagnostics`` pins the *emitter*: what a
``stdio_error`` diagnostic carries once a caller has already named the
boundary. That leaves the other half untested, and it is the half that matters.
The boundary is not passed by the handler in the interesting cases — the
streaming writer has to *derive* it, and an encoder failure and a genuine pipe
fault both reach the handler as a bare exception with nothing in its type to
separate them.

So these cases drive ``SafeCmd.run`` and assert what came out of it. Each
reaches one boundary through the public API and asserts the three things a
diagnosis owes an operator: the category, the correlation, and the metric. A
case that called ``_emit_stdio_error`` directly could not tell a derived
category from a passed one, which is exactly the distinction that was broken.

Reaching the *final* encode is worth explaining, because it is not obvious. An
encoder that rejects a chunk does so from ``encode(text, final=False)``, the
first call site; the final call sees an empty string and an encoder that has
already succeeded, so a strict ``utf-8`` never fails there and the second call
site would go uncovered. A codec that *buffers* is what makes it reachable, and
``idna`` is the deterministic one: it accumulates a label across calls, emits
nothing until it is flushed, and rejects an over-long label only from
``final=True``. That is the codec's documented DNS length rule, not a quirk.

Non-vacuity was measured rather than asserted. Each of these was applied to
cuprum's own source, the suite was run, and the mutations were restored:

- Marking the per-chunk encode but not the final flush failed exactly one case,
  ``test_a_label_that_fails_only_when_flushed_is_the_encoder``, which reported
  ``PIPE`` where ``ENCODER`` was required. That case exists *because* the
  mutation survives without it, and it is the only case that catches it.
- Removing ``mark_invalid_chunk``'s effect failed exactly one case,
  ``test_a_chunk_of_the_wrong_type_is_an_invalid_chunk``, which reported
  ``PIPE``. Without the marker a mistyped chunk is indistinguishable from a
  transport fault.
- Making the classifier ignore the mark and answer ``PIPE`` unconditionally
  failed the four marked cases. It is the mirror of the residual case and the
  reason both directions are needed: that mutation is caught by every case
  *except* the transport one, and the residual mutation below is caught by
  nothing else.
- Returning the wrong residual category from the classifier failed exactly one
  case, ``test_a_transport_failure_is_the_pipe``. This is the mutation that
  justifies that case existing: three encoder cases and a chunk case all assert
  a boundary something *recorded*, and every one of them passes if the
  unrecorded fallback is wrong.
- Removing the restore after a *successful* encode failed no case, because a
  non-chunk failure calls ``mark_invalid_chunk``, which overwrites the
  encoder's mark on every path the suite can reach. That restore is therefore
  defensive rather than load-bearing, and no docstring here claims otherwise.

The doubles and the cross-channel assertion live in
``cuprum.unittests._stdio_classification_support``; what follows is the list of
boundaries itself.
"""

from __future__ import annotations

import asyncio
import errno
import pathlib
import typing as typ

from cuprum.events import StdioFailureCategory
from cuprum.sh import (
    ExecutionContext,
    RunOutputOptions,
    StdinSourceError,
    StdinStream,
    StdioTarget,
)
from cuprum.unittests._stdio_classification_support import (
    NEVER_READS,
    OVERLONG_IDNA_LABEL,
    UnstartableProducer,
    assert_classified_once,
    borrowed_flush_target,
    chunks,
    mistyped_chunk,
    run_capturing,
)

if typ.TYPE_CHECKING:
    import pytest

# The observe phase a pre-spawn boundary never reaches: a run that fails before
# the fork records a plan, its diagnostic, and a settle, but starts no child.
_STARTING_PHASE = "start"


class TestTheEncoderIsNamedForEncoding:
    """Every encoder failure is ``ENCODER``, whichever call site raised it."""

    def test_a_chunk_that_cannot_be_encoded_is_the_encoder(self) -> None:
        """An unencodable chunk at the per-chunk encode is an encoder failure.

        The strict ``ascii`` codec rejects ``café`` from ``encode()`` inside the
        writer's per-chunk call. The exception that reaches the handler is a
        ``UnicodeEncodeError``, which is not a pipe error at all — but every
        failure that is not an early close routes through the write-boundary
        classifier, and an unmarked one there takes the residual ``PIPE``
        category, reporting a pipe that was never at fault.
        """
        outcome = run_capturing(
            stdin=StdinStream(chunks("café")),
            context=ExecutionContext(encoding="ascii", errors="strict"),
        )

        assert_classified_once(
            outcome,
            category=StdioFailureCategory.ENCODER,
            operation="write",
            error_type="UnicodeEncodeError",
            pid_present=True,
            raised=StdinSourceError,
        )

    def test_a_label_that_fails_only_when_flushed_is_the_encoder(self) -> None:
        """An over-long label failing the *final* encode is an encoder failure.

        This is the second call site, reachable only through a codec that
        buffers, which the module docstring explains. Marking the per-chunk
        encode alone leaves this one unmarked; the measured mutation that drops
        this mark is caught by this case and by no other.
        """
        outcome = run_capturing(
            stdin=StdinStream(chunks(OVERLONG_IDNA_LABEL)),
            context=ExecutionContext(encoding="idna", errors="strict"),
        )

        assert_classified_once(
            outcome,
            category=StdioFailureCategory.ENCODER,
            operation="write",
            error_type="UnicodeEncodeError",
            pid_present=True,
            raised=StdinSourceError,
        )

    def test_a_codec_that_cannot_be_built_is_the_encoder(self) -> None:
        """A codec this build does not know is an encoder failure.

        The third call site, and the one that fails where the encoder is
        *built* rather than where it encodes. All three must land on the same
        category; a boundary that named itself from the exception would put a
        ``LookupError`` somewhere else entirely.
        """
        outcome = run_capturing(
            stdin=StdinStream(chunks("text")),
            context=ExecutionContext(encoding="no-such-codec", errors="strict"),
        )

        assert_classified_once(
            outcome,
            category=StdioFailureCategory.ENCODER,
            operation="write",
            error_type="LookupError",
            pid_present=True,
            raised=StdinSourceError,
        )


class TestTheOtherBoundariesNameThemselves:
    """Each remaining boundary reports its own category, not a neighbour's."""

    def test_a_chunk_of_the_wrong_type_is_an_invalid_chunk(self) -> None:
        """A producer yielding a non-chunk is the chunk's fault.

        Without the marker this failure is a bare ``TypeError`` reaching the
        classifier, which has nothing to name it by and reports ``PIPE``
        instead. The measured mutation that drops the marker is caught by this
        case and by no other.
        """
        outcome = run_capturing(
            stdin=StdinStream(mistyped_chunk()),
            context=ExecutionContext(encoding="utf-8", errors="strict"),
        )

        assert_classified_once(
            outcome,
            category=StdioFailureCategory.INVALID_CHUNK,
            operation="write",
            error_type="TypeError",
            pid_present=True,
            raised=StdinSourceError,
        )

    def test_a_transport_failure_is_the_pipe(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fault in the write itself is the one boundary with no marker.

        The residual category, and the control for the marked cases above: they
        assert a boundary that something recorded, and a classifier that never
        read the record would satisfy them while reporting every unmarked
        failure as the last thing that happened to be written there. Here
        nothing in cuprum's own write path has failed — the bytes were well
        typed and encodable, and the transport was not — so ``PIPE`` is the
        only correct answer.

        The fault is injected at ``StreamWriter.drain``, which is the transport
        boundary itself and the exact call ``_write_chunk`` makes. ``EIO``
        rather than ``EPIPE`` is deliberate: a broken pipe is an early close,
        classified before the write boundary is consulted, and would never
        reach this classifier at all.
        """

        async def faulty_drain(  # ruff: ignore[unused-async] - mirrors an awaited coroutine method.
            self: asyncio.StreamWriter,
        ) -> None:
            """Fail the drain as a transport fault, not as a closed pipe."""
            raise OSError(errno.EIO, "simulated transport fault")

        monkeypatch.setattr(asyncio.StreamWriter, "drain", faulty_drain)

        outcome = run_capturing(stdin=StdinStream(chunks(b"payload")))

        assert_classified_once(
            outcome,
            category=StdioFailureCategory.PIPE,
            operation="write",
            error_type="OSError",
            pid_present=True,
            raised=StdinSourceError,
        )

    def test_a_failing_producer_is_the_producer(self) -> None:
        """A producer that cannot start is the producer, not the child.

        It raises ``BrokenPipeError`` from its own ``__aiter__``, which is the
        same class a child closing its end produces from a write. The child
        here never reads, so its pipe stays open and no write of cuprum's can
        have failed: the classification is decided by provenance rather than by
        type, and this is the case where the two are indistinguishable.
        """
        outcome = run_capturing(
            code=NEVER_READS,
            stdin=StdinStream(UnstartableProducer(BrokenPipeError(32, "Broken pipe"))),
        )

        assert_classified_once(
            outcome,
            category=StdioFailureCategory.PRODUCER,
            operation="produce",
            error_type="BrokenPipeError",
            pid_present=True,
            raised=StdinSourceError,
        )

    def test_an_unopenable_owned_path_names_no_child(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """An owned target that cannot be opened reports before the fork.

        The pre-spawn boundaries are the ones ``pid`` is ``None`` for, which is
        why the parameter is required rather than defaulted: the boundary has
        to state which side of the fork it is on. A defaulted ``None`` would
        let a post-spawn site omit it by accident and read as pre-spawn.
        """
        missing = tmp_path / "absent" / "out.txt"

        outcome = run_capturing(
            output=RunOutputOptions(
                capture=False,
                stdout=StdioTarget.path(missing),
            ),
        )

        assert_classified_once(
            outcome,
            category=StdioFailureCategory.OWNED_PATH_OPEN,
            operation="open",
            error_type="OSError",
            pid_present=False,
            raised=OSError,
        )
        assert not missing.exists(), (
            "a path cuprum could not open must not have been created"
        )

    def test_a_borrowed_flush_that_fails_names_no_child(self) -> None:
        """A borrowed object that cannot be flushed reports before the fork.

        The second pre-spawn boundary. Proceeding past a failed flush would
        start the child without the bytes the caller buffered, so the failure
        is reported *and* re-raised rather than swallowed.
        """
        outcome = run_capturing(
            output=RunOutputOptions(
                capture=False,
                stdout=borrowed_flush_target(),
            ),
        )

        assert_classified_once(
            outcome,
            category=StdioFailureCategory.BORROWED_FLUSH,
            operation="flush",
            error_type="OSError",
            pid_present=False,
            raised=OSError,
        )


def test_a_pre_spawn_boundary_records_no_start_event() -> None:
    """A run that fails before the fork reports a diagnostic and no child.

    The observed shape is pinned here rather than left implicit, because it is
    what a correlation check has to survive: this run's events are a plan, the
    diagnostic, and a settle, so an assertion anchored on the ``start`` event
    would find nothing to anchor to and an earlier draft of these assertions
    did exactly that. The correlation itself is checked inside
    ``assert_classified_once``, against the run's own events.
    """
    outcome = run_capturing(
        output=RunOutputOptions(
            capture=False,
            stdout=StdioTarget.path(pathlib.Path("/nonexistent-dir/out.txt")),
        ),
    )

    phases = [event.phase for event in outcome.events]
    assert _STARTING_PHASE not in phases, (
        f"a boundary reached before the fork must start no child; found phases {phases}"
    )
    assert_classified_once(
        outcome,
        category=StdioFailureCategory.OWNED_PATH_OPEN,
        operation="open",
        error_type="OSError",
        pid_present=False,
        raised=OSError,
    )


def test_the_boundaries_are_distinguishable_from_one_another() -> None:
    """The six categories are pairwise distinct, so none is a catch-all.

    A cheap structural guard beside the behavioural cases. Every encoder case
    asserts ``ENCODER``, and if ``ENCODER`` and ``PIPE`` shared a value those
    assertions would pass for the very boundary the marker exists to rule out.
    The member set is read from the enum so a category added later is covered
    without editing this test.
    """
    values = [str(member) for member in StdioFailureCategory]

    assert len(values) == len(set(values)), (
        f"every category must have a distinct value; found {values}"
    )
    assert set(values) == {
        "producer",
        "invalid_chunk",
        "encoder",
        "pipe",
        "owned_path_open",
        "borrowed_flush",
    }, f"the bounded category set changed, and its consumers were not updated: {values}"
