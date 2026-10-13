"""Telemetry witnesses for the capture mode a run reports.

A byte-exact run and a text run of the same command are identical in every
counter, histogram, span, and log record unless the mode travels with them, so
each adapter is exercised against a *real* run of each kind. The probes read
what the adapter published rather than what the event carried, because the
projection is the part that could silently drop the field.

The second half of the contract is what the diagnostics must *not* carry. The
refusal is raised on the caller's side of the spawn and the invariant violation
is raised while a result is being assembled, so neither is reachable from a
run; both are driven directly, and each probe asserts the child's payload is
absent from the emitted record.
"""

from __future__ import annotations

import logging
import typing as typ

import pytest

from cuprum import sh
from cuprum._bytes_run import _validate_bytes_output
from cuprum._pipeline_types import _ExecutionInvariantError
from cuprum._result_assembly import _require_bytes, _require_text
from cuprum.adapters.logging_adapter import structured_logging_hook
from cuprum.adapters.metrics_adapter import MetricsHook
from cuprum.adapters.tracing_adapter import InMemoryTracer, TracingHook
from cuprum.context import ScopeConfig, scoped
from cuprum.events import CaptureMode
from cuprum.sh import RunOutputOptions
from cuprum.unittests._adapter_test_support import (
    _LabelRecordingCollector,
    capturing_logger,
)
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# A payload no codec can round-trip: every byte value, then an invalid UTF-8
# sequence and a NUL. Both modes run the same child, so any difference between
# their telemetry is the mode and nothing else; the payload's tail is what the
# payload-privacy probes search for, because a codec could in principle emit
# some of the earlier bytes.
_PAYLOAD = bytes(range(256)) + b"\xff\x00\xfe\x80"
# The last four bytes as they would read inside a rendered payload, in the two
# forms a leak could take: a Latin-1 decode and a backslash escape.
_PAYLOAD_TAIL_TEXT = _PAYLOAD[-4:].decode("latin-1")
_PAYLOAD_TAIL_REPR = repr(_PAYLOAD[-4:])
_WRITE_PAYLOAD = f"import sys; sys.stdout.buffer.write({_PAYLOAD!r})"


@pytest.fixture
def mode_run() -> cabc.Callable[[CaptureMode], None]:
    """Return a runner executing the shared command in a named capture mode.

    The command and the allowlist admitting it come from one catalogue, since a
    command built from a second catalogue would be refused before the run every
    probe observes. The probe asks only for bytes mode's presence, so it
    deliberately ignores the two result classes rather than pinning them here,
    where the subject is the telemetry the run published.

    Returns
    -------
    cabc.Callable[[CaptureMode], None]
        A runner that executes the shared command in the named mode.
    """

    def run(mode: CaptureMode) -> None:
        """Run the command, asking for bytes only when the mode says so."""
        catalogue, program = python_catalogue()
        cmd = sh.make(program, catalogue=catalogue)("-c", _WRITE_PAYLOAD)
        with scoped(ScopeConfig(allowlist=catalogue.allowlist)):
            if mode is CaptureMode.BYTES:
                cmd.run_bytes_sync()
            else:
                cmd.run_sync()

    return run


def _mode_of(extra: cabc.Mapping[str, object]) -> object:
    """Return the capture mode a logging record published, if any."""
    return extra.get("cuprum_capture_mode")


class TestCaptureModeInLoggingRecords:
    """A captured log record names the mode its run reported."""

    @pytest.mark.parametrize("mode", list(CaptureMode))
    def test_records_publish_their_mode(
        self,
        mode: CaptureMode,
        mode_run: cabc.Callable[[CaptureMode], None],
    ) -> None:
        """Each phase of each mode carries the mode under its own extra key."""
        with capturing_logger("cuprum.test.capture_mode") as capture:
            hook = structured_logging_hook(logger=capture.logger)
            with sh.observe(hook):
                mode_run(mode)

        records = capture.records
        assert records, "the observed run must produce log records"
        modes = {_mode_of(vars(record)) for record in records}
        assert modes == {str(mode)}, (
            f"every record must name mode {str(mode)!r}, got {modes!r}"
        )


class TestCaptureModeInMetricLabels:
    """Metric labels distinguish a byte run from a text run of one command."""

    @staticmethod
    def _execution_labels(collector: _LabelRecordingCollector) -> set[str]:
        """Return the ``capture_mode`` labels on the execution counter."""
        return {
            label
            for name, value, labels in collector.calls
            # One run increments the counter once, so the increment is exactly
            # one and the comparison is not a tolerance question.
            if name == "cuprum_executions_total" and value == 1.0  # ruff: ignore[float-equality-comparison] - exact counter increment
            for label in [labels.get("capture_mode")]
            if label is not None
        }

    def test_execution_counter_labels_distinguish_the_modes(
        self,
        mode_run: cabc.Callable[[CaptureMode], None],
    ) -> None:
        """The per-execution counters carry the mode the run resolved."""
        labels_by_mode: dict[CaptureMode, set[str]] = {}
        for mode in CaptureMode:
            collector = _LabelRecordingCollector()
            with sh.observe(MetricsHook(collector)):
                mode_run(mode)
            labels_by_mode[mode] = self._execution_labels(collector)

        assert labels_by_mode == {
            CaptureMode.TEXT: {"text"},
            CaptureMode.BYTES: {"bytes"},
        }, (
            "the execution counter must carry each run's own capture_mode label "
            f"rather than a shared one, got {labels_by_mode!r}"
        )


class TestCaptureModeInSpanAttributes:
    """A span says which mode the execution it describes reported."""

    @pytest.mark.parametrize("mode", list(CaptureMode))
    def test_spans_publish_their_mode(
        self,
        mode: CaptureMode,
        mode_run: cabc.Callable[[CaptureMode], None],
    ) -> None:
        """The span for each mode carries the mode as an attribute."""
        tracer = InMemoryTracer()

        with sh.observe(TracingHook(tracer)):
            mode_run(mode)

        spans = tracer.spans
        assert spans, "the observed run must open a span"
        modes = {span.attributes.get("cuprum.capture_mode") for span in spans}
        assert modes == {str(mode)}, (
            f"every span must name mode {str(mode)!r}, got {modes!r}"
        )


class TestCaptureModeDiagnosticsCarryNoPayload:
    """The two mode-boundary diagnostics publish no captured output.

    Both sit on paths a run never reaches — the refusal is raised before the
    spawn and the invariant violation while a result is assembled — so they are
    driven directly. Each probe asserts the payload is nowhere in the record
    *and* that the fields which should be there are, because a search for an
    absent string passes against an empty or missing record too.
    """

    @staticmethod
    def _assert_payload_absent(message: str) -> None:
        """Assert *message* holds no spelling of the captured payload."""
        assert _PAYLOAD.decode("latin-1") not in message, (
            "the diagnostic must not render the captured payload"
        )
        for spelling in (_PAYLOAD_TAIL_TEXT, _PAYLOAD_TAIL_REPR):
            assert spelling not in message, (
                f"the diagnostic must not render the payload's tail as "
                f"{spelling!r}, got {message!r}"
            )

    def test_refused_line_observer_is_recorded_without_the_callback(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A refused ``on_line`` is named as bytes mode, and nothing else."""

        def observer(line: object) -> None:
            """Stand in for a caller's line observer; must never be rendered."""

        with (
            caplog.at_level(logging.WARNING, logger="cuprum._bytes_run"),
            pytest.raises(ValueError, match="on_line"),
        ):
            _validate_bytes_output(RunOutputOptions(on_line=observer))

        records = [
            record for record in caplog.records if record.name == "cuprum._bytes_run"
        ]
        assert [record.levelno for record in records] == [logging.WARNING], (
            f"the refusal must record exactly one warning, got {records!r}"
        )
        message = records[0].getMessage()
        # The positive half first: the mode the operator filters on has to be
        # in the record, or the search below proves nothing about a diagnostic
        # that was never emitted in a usable form.
        assert "capture_mode=bytes" in message, (
            f"the refusal must name the mode that was turned away, got {message!r}"
        )
        self._assert_payload_absent(message)
        # The message names *what* was refused, which is the diagnostic; the
        # callback object itself is what must stay out. Rendering it would put
        # a memory address and a caller-chosen name into a retained channel,
        # and the ``<function ...>`` opener is the part a bare ``str`` of a
        # callable always carries.
        assert "<function" not in message, (
            f"the refusal must not render the refused callback, got {message!r}"
        )

    def test_mode_payload_contradiction_is_recorded_without_the_payload(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An invariant violation names its types, never the bytes it refused."""
        # ``_require_bytes`` is handed a ``str`` and ``_require_text`` a
        # ``bytes``: each pair contradicts one mode in one direction.
        contradictions = (
            (_require_bytes, "decoded", CaptureMode.BYTES, "str"),
            (_require_text, _PAYLOAD, CaptureMode.TEXT, "bytes"),
        )
        with caplog.at_level(logging.ERROR, logger="cuprum._result_assembly"):
            # Only the catcher and its payload drive the call; the mode and the
            # type are asserted from the record below, so they are unpacked there.
            for narrow, payload, *_ in contradictions:
                with pytest.raises(_ExecutionInvariantError):
                    narrow(payload, "stdout")

        records = [
            record
            for record in caplog.records
            if record.name == "cuprum._result_assembly"
        ]
        assert [record.levelno for record in records] == [logging.ERROR] * len(
            contradictions
        ), f"each contradiction must record one error, got {records!r}"
        for record, (_, _, mode, actual) in zip(records, contradictions, strict=True):
            message = record.getMessage()
            # The record interpolates the mode's args, so the operator reads
            # the filterable values — "bytes", "str" — not the member's repr.
            assert f"capture_mode={mode}" in message, (
                f"the diagnostic must name mode {mode} in filterable form, got "
                f"{message!r}"
            )
            assert f"actual={actual}" in message, (
                f"the diagnostic must name the payload type it refused, got {message!r}"
            )
            self._assert_payload_absent(message)
