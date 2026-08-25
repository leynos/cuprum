"""Span-lifecycle and attribute-contract tests for ``TracingHook``.

A span is opened by ``start`` and closed by ``exit``. These tests cover what
happens in between, and what happens when that closing event never comes: the
ancillary ``stdin_error`` / ``timeout`` / ``teardown_error`` phases record a
span event and deliberately leave the span open; a ``teardown_error`` arriving
after ``exit`` finds no entry and is dropped; and an execution that never
emits ``exit`` at all is bounded by the registry's least-recently-active
eviction rather than leaking an entry for the life of the hook.

The documented span-attribute contract is checked here too, so the prose and
the attributes the hook can actually emit cannot drift apart.

The ``exec_id`` correlation rules — that a recycled PID must not cross one
execution's events onto another execution's span — live in
``test_tracing_exec_id_correlation``.

Events are built with the shared :func:`_make_exec_event` factory; each call
passes its ``pid``, ``exec_id``, and phase-specific fields through
``overrides``.
"""

from __future__ import annotations

import logging
import typing as typ
from pathlib import Path

import pytest

from cuprum.adapters.tracing_adapter import (
    _MAX_ACTIVE_SPANS,
    InMemoryTracer,
    TracingHook,
)
from cuprum.events import ExecEvent, new_exec_id
from cuprum.program import Program
from cuprum.unittests._adapter_test_support import _make_exec_event

if typ.TYPE_CHECKING:
    from cuprum.events import ExecId, ExecPhase

# Single source of truth for the span-attribute contract documented on
# ``TracingHook``. Both the documentation check and the emitted-attribute
# check derive from this constant, so the contract is defined in exactly one
# place and cannot drift between prose and code.
DOCUMENTED_SPAN_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "cuprum.program",
        "cuprum.argv",
        "cuprum.pid",
        "cuprum.cwd",
        "cuprum.exit_code",
        "cuprum.duration_s",
        "cuprum.project",
        "cuprum.pipeline_stage_index",
        "cuprum.pipeline_stages",
    },
)


def _users_guide_text() -> str:
    """Return the users' guide markdown for documentation-contract checks."""
    repo_root = Path(__file__).resolve().parents[2]
    return (repo_root / "docs" / "users-guide.md").read_text(encoding="utf-8")


class TestTracingSpanLifecycle:
    """Span-lifecycle and attribute-contract tests for ``TracingHook``."""

    @staticmethod
    def _cat_overrides(exec_id: ExecId, pid: int = 4321) -> dict[str, object]:
        """Return the identifying overrides for a traced ``cat`` execution.

        The span-lifecycle tests care about which execution an event belongs
        to, not what it ran, so they all share one program and argv and vary
        only the correlation token and pid.

        Returns
        -------
        dict[str, object]
            Overrides naming the program, argv, pid, and correlation token.
        """
        return {"program": "cat", "argv": ("cat",), "pid": pid, "exec_id": exec_id}

    @pytest.mark.parametrize(
        ("phase", "extra_fields", "expected_attributes"),
        [
            pytest.param(
                "stdin_error",
                {
                    "operation": "write",
                    "error_type": "OSError",
                    "note": "OSError: broken pipe",
                },
                {"operation": "write", "error_type": "OSError"},
                id="stdin_error",
            ),
            pytest.param(
                "timeout",
                {
                    "operation": "wait",
                    "error_type": "TimeoutError",
                    "timeout_s": 1.5,
                    "timeout_mode": "elapsed_deadline",
                },
                {
                    "operation": "wait",
                    "error_type": "TimeoutError",
                    "timeout_s": 1.5,
                    "timeout_mode": "elapsed_deadline",
                },
                id="timeout",
            ),
            pytest.param(
                "teardown_error",
                {
                    "operation": "drain",
                    "error_type": "ValueError",
                    "note": "consumer drain failed: ValueError",
                },
                {"operation": "drain", "error_type": "ValueError"},
                id="teardown_error",
            ),
        ],
    )
    def test_records_ancillary_event_without_ending_span(
        self,
        phase: ExecPhase,
        extra_fields: dict[str, object],
        expected_attributes: dict[str, object],
    ) -> None:
        """Ancillary phases become span events and leave the span open.

        ``stdin_error``, ``timeout``, and ``teardown_error`` are all diagnostics
        that accompany rather than conclude an execution, so each must be
        recorded as a ``cuprum.<phase>`` span event carrying the stable
        attributes in ``expected_attributes`` — ``operation`` and
        ``error_type`` for every phase, plus ``timeout_s`` / ``timeout_mode``
        for ``timeout`` — while the span stays open for the subsequent
        ``exit``.
        """
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)

        exec_id = new_exec_id()
        base = self._cat_overrides(exec_id)
        hook(_make_exec_event(phase="start", overrides=base))
        hook(
            _make_exec_event(
                phase=phase,
                overrides={**base, **extra_fields},
            ),
        )

        span = tracer.spans[0]
        event_name = f"cuprum.{phase}"
        attrs = next(
            (attrs for name, attrs in span.events if name == event_name),
            None,
        )
        assert attrs is not None, (
            f"the tracing hook should surface {phase} as a {event_name} span event, "
            f"but recorded {[name for name, _ in span.events]}"
        )
        for key, want in expected_attributes.items():
            assert attrs.get(key) == want, (
                f"the {phase} span event should carry {key}={want!r}, "
                f"got {attrs.get(key)!r}"
            )
        assert span.ended is False, (
            f"an ancillary {phase} event must not end the execution span"
        )

    def test_teardown_error_after_exit_is_dropped(self) -> None:
        """A late ``teardown_error`` must not disturb a concluded execution.

        The drain runs after the process has been reaped, so its failure can be
        reported once ``exit`` has already closed the span. The hook keys on
        ``exec_id``, and ``exit`` removes the entry, so the late event finds no
        open span and is dropped rather than reopening or re-ending one.
        """
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)

        exec_id = new_exec_id()
        base = self._cat_overrides(exec_id)
        hook(_make_exec_event(phase="start", overrides=base))
        hook(
            _make_exec_event(
                phase="exit",
                overrides={**base, "exit_code": 0, "duration_s": 0.5},
            ),
        )
        hook(
            _make_exec_event(
                phase="teardown_error",
                overrides={**base, "operation": "drain", "error_type": "ValueError"},
            ),
        )

        span = tracer.spans[0]
        assert span.ended is True, "the exit event must still have closed the span"
        assert not any(name == "cuprum.teardown_error" for name, _ in span.events), (
            "a teardown_error arriving after exit must not be recorded on the "
            f"closed span, got {[name for name, _ in span.events]}"
        )

    def test_abandoned_spans_are_evicted_once_the_registry_fills(self) -> None:
        """An execution that never emits ``exit`` must not accumulate forever.

        Cleanup also runs on external cancellation and on a stdin-writer
        failure, and on those paths the original exception propagates with no
        ``exit`` — so a ``teardown_error`` can be the last event an execution
        emits. Those spans have nothing left to close them, so the registry is
        bounded and evicts the oldest, ending it as failed.
        """
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)

        abandoned = new_exec_id()
        abandoned_overrides = self._cat_overrides(abandoned, pid=1)
        hook(_make_exec_event(phase="start", overrides=abandoned_overrides))
        hook(
            _make_exec_event(
                phase="teardown_error",
                overrides={
                    **abandoned_overrides,
                    "operation": "drain",
                    "error_type": "ValueError",
                },
            ),
        )
        assert tracer.spans[0].ended is False, (
            "the abandoned span should still be open before the registry fills"
        )

        for index in range(_MAX_ACTIVE_SPANS):
            hook(
                _make_exec_event(
                    phase="start",
                    overrides=self._cat_overrides(new_exec_id(), pid=index + 2),
                ),
            )

        assert tracer.spans[0].ended is True, (
            "the oldest abandoned span must be ended once the cap is exceeded, "
            "otherwise a run that never emits exit leaks one entry per execution"
        )
        assert len(hook._active_spans) == _MAX_ACTIVE_SPANS, (
            f"the registry must stay bounded, got {len(hook._active_spans)}"
        )

    def test_eviction_is_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        """Dropping a span must not be silent.

        An evicted span is ended as failed while its execution may still be
        running, so its trace is lost — and without a signal that loss is
        undiagnosable: the trace is simply missing and nothing says why.
        """
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)

        with caplog.at_level(logging.WARNING, logger="cuprum.adapters"):
            for index in range(_MAX_ACTIVE_SPANS + 1):
                hook(
                    _make_exec_event(
                        phase="start",
                        overrides=self._cat_overrides(new_exec_id(), pid=index + 1),
                    ),
                )

        overflow = [r for r in caplog.records if "span_registry_overflow" in r.msg]
        assert len(overflow) == 1, f"one eviction, one record; got {caplog.records}"
        counts = vars(overflow[0])
        assert (counts["cuprum_spans_evicted"], counts["cuprum_spans_active"]) == (
            1,
            _MAX_ACTIVE_SPANS,
        ), f"the record must count what was dropped and what remains, got {counts}"

    def test_eviction_spares_the_span_that_is_still_active(self) -> None:
        """A span still receiving events outlives one that went quiet earlier.

        Eviction order is recency of activity, not of arrival. Were it arrival
        order, the execution that started first would be finalized as failed
        even while it was demonstrably still producing output, and its real
        ``exit`` would then find nothing to close.
        """
        tracer = InMemoryTracer()
        hook = TracingHook(tracer, record_output=True)

        busy, quiet = new_exec_id(), new_exec_id()
        hook(
            _make_exec_event(phase="start", overrides=self._cat_overrides(busy, pid=1))
        )
        busy_span = tracer.spans[0]
        hook(
            _make_exec_event(phase="start", overrides=self._cat_overrides(quiet, pid=2))
        )
        quiet_span = tracer.spans[1]

        # The older execution is the one still doing work.
        hook(
            _make_exec_event(
                phase="stdout",
                overrides={**self._cat_overrides(busy, pid=1), "line": "still here"},
            ),
        )

        # One short of the cap, so exactly one of the two above is evicted and
        # the assertions below can say which.
        for index in range(_MAX_ACTIVE_SPANS - 1):
            hook(
                _make_exec_event(
                    phase="start",
                    overrides=self._cat_overrides(new_exec_id(), pid=index + 3),
                ),
            )

        assert quiet_span.ended is True, (
            "the execution that went quiet is the one that should be evicted"
        )
        assert busy_span.ended is False, (
            "an execution that was still emitting events must not be finalized "
            "ahead of one that fell silent earlier"
        )

    def test_emitted_attributes_match_documented_contract(self) -> None:
        """The attributes the hook emits equal the documented contract.

        Every attribute ``_build_attributes`` produces for a fully-populated
        start event, plus the exit-time attributes, must equal
        ``DOCUMENTED_SPAN_ATTRIBUTES`` — guarding the omission of ``cuprum.cwd`` /
        ``cuprum.pipeline_stages`` that motivated issue #122.
        """
        start_event = ExecEvent(
            phase="start",
            program=Program("cat"),
            argv=("cat",),
            cwd=Path("/srv/work"),
            env=None,
            pid=4321,
            timestamp=0.0,
            line=None,
            exit_code=None,
            duration_s=None,
            tags={
                "project": "doc-lockstep",
                "pipeline_stage_index": 0,
                "pipeline_stages": 2,
            },
        )
        emitted = set(TracingHook._build_attributes(start_event))
        # exit_code and duration_s are attached later, in _handle_exit.
        emitted |= {"cuprum.exit_code", "cuprum.duration_s"}

        contract = set(DOCUMENTED_SPAN_ATTRIBUTES)
        assert emitted == contract, (
            "emitted attributes must match the documented contract; "
            f"emitted-only={emitted - contract}, contract-only={contract - emitted}"
        )

    def test_docstring_documents_each_contract_attribute(self) -> None:
        """The ``TracingHook`` docstring names every attribute in the contract.

        Checks substring membership of each documented name rather than parsing
        ``__doc__``, so whitespace or formatting edits to the docstring cannot
        change the test outcome.
        """
        doc = TracingHook.__doc__ or ""
        missing = sorted(
            attr for attr in DOCUMENTED_SPAN_ATTRIBUTES if f"``{attr}``" not in doc
        )
        assert not missing, (
            f"TracingHook docstring omits documented attributes: {missing}"
        )

    def test_users_guide_lists_every_tracing_attribute(self) -> None:
        """The users' guide tracing section names every attribute in the contract.

        Uses inline-code substring membership rather than parsing the list, so the
        guide's markdown formatting is not itself part of the assertion. Locks in
        ``cuprum.cwd`` and ``cuprum.pipeline_stages`` alongside the rest.
        """
        guide = _users_guide_text()
        missing = sorted(
            attr for attr in DOCUMENTED_SPAN_ATTRIBUTES if f"`{attr}`" not in guide
        )
        assert not missing, f"users' guide omits tracing attributes: {missing}"

    def test_users_guide_names_record_output_option(self) -> None:
        """The users' guide documents ``record_output``, not the obsolete name."""
        guide = _users_guide_text()
        assert "record_output" in guide, (
            "users' guide must name the record_output option"
        )
        assert "record_io" not in guide, (
            "users' guide must not use the obsolete record_io option name"
        )
