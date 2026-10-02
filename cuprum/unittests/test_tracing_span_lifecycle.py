"""Span-lifecycle and attribute-contract tests for ``TracingHook``.

A span is opened by ``start``. These tests cover what happens in between and
what happens when ``settled`` never comes: the ancillary ``stdin_error`` /
``timeout`` / ``teardown_error`` phases record a span event and deliberately
leave the span open, while ``settled`` closes a remaining span with its bounded
terminal category. A ``teardown_error`` arriving after ``exit`` finds no entry
and is dropped.

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

import typing as typ

import pytest

from cuprum.events import TerminalOutcome, new_exec_id
from cuprum.unittests._adapter_test_support import (
    Traced,
    _cat_overrides,
    _make_exec_event,
    tracing_hook,
)

if typ.TYPE_CHECKING:
    from cuprum.events import ExecPhase

__all__ = ["tracing_hook"]


class TestTracingSpanLifecycle:
    """Span-lifecycle and attribute-contract tests for ``TracingHook``."""

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
        tracing_hook: Traced,
        phase: ExecPhase,
        extra_fields: dict[str, object],
        expected_attributes: dict[str, object],
    ) -> None:
        """Ancillary phases become span events and leave the span open.

        ``stdin_error``, ``timeout``, and ``teardown_error`` are diagnostics that
        accompany rather than conclude an execution. Each becomes a
        ``cuprum.<phase>`` span event carrying the stable attributes in
        ``expected_attributes`` — ``operation`` and ``error_type`` for every
        phase, plus ``timeout_s`` / ``timeout_mode`` for ``timeout`` — while the
        span stays open for the subsequent ``settled``.
        """
        tracer, hook = tracing_hook

        exec_id = new_exec_id()
        base = _cat_overrides(exec_id)
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

    def test_teardown_error_after_settled_is_dropped(
        self, tracing_hook: Traced
    ) -> None:
        """A late ``teardown_error`` must not disturb a concluded execution.

        The drain runs after the process has been reaped, so its failure can be
        reported once ``settled`` has already closed the span. The hook keys on
        ``exec_id``, and ``settled`` removes the entry, so the late event finds no
        open span and is dropped rather than reopening or re-ending one.
        """
        tracer, hook = tracing_hook

        exec_id = new_exec_id()
        base = _cat_overrides(exec_id)
        hook(_make_exec_event(phase="start", overrides=base))
        hook(
            _make_exec_event(
                phase="exit",
                overrides={**base, "exit_code": 0, "duration_s": 0.5},
            ),
        )
        hook(
            _make_exec_event(
                phase="settled",
                overrides={**base, "terminal_outcome": TerminalOutcome.EXIT_ZERO},
            ),
        )
        hook(
            _make_exec_event(
                phase="teardown_error",
                overrides={**base, "operation": "drain", "error_type": "ValueError"},
            ),
        )

        span = tracer.spans[0]
        assert span.ended is True, "the settled event must close the span"
        assert not any(name == "cuprum.teardown_error" for name, _ in span.events), (
            "a teardown_error arriving after exit must not be recorded on the "
            f"closed span, got {[name for name, _ in span.events]}"
        )

    @pytest.mark.parametrize(
        ("outcome", "status_ok"),
        [
            (TerminalOutcome.EXIT_ZERO, True),
            (TerminalOutcome.EXIT_NONZERO, False),
            (TerminalOutcome.TIMEOUT, False),
            (TerminalOutcome.CANCELLED, False),
            (TerminalOutcome.ERROR, False),
        ],
    )
    def test_settled_closes_span_from_category_only(
        self,
        tracing_hook: Traced,
        outcome: TerminalOutcome,
        status_ok: bool,
    ) -> None:
        """A missing exit is finalized without inventing status or details."""
        tracer, hook = tracing_hook
        exec_id = new_exec_id()
        hook(_make_exec_event(phase="start", overrides=_cat_overrides(exec_id)))
        hook(
            _make_exec_event(
                phase="settled",
                overrides={
                    **_cat_overrides(exec_id, pid=None),
                    "terminal_outcome": outcome,
                },
            )
        )

        span = tracer.spans[0]
        assert span.ended is True
        assert span.status_ok is status_ok
        assert span.attributes["cuprum.terminal_outcome"] == str(outcome)
        assert "cuprum.exit_code" not in span.attributes, (
            "settlement must not synthesize an exit code"
        )
        assert exec_id not in hook._active_spans

    def test_exit_records_child_status_until_settled_closes_span(
        self,
        tracing_hook: Traced,
    ) -> None:
        """The child exit stays visible while settlement decides span status."""
        tracer, hook = tracing_hook
        exec_id = new_exec_id()
        base = _cat_overrides(exec_id)
        hook(_make_exec_event(phase="start", overrides=base))
        hook(
            _make_exec_event(
                phase="exit",
                overrides={**base, "exit_code": 7, "duration_s": 0.5},
            )
        )

        span = tracer.spans[0]
        assert span.ended is False
        assert span.status_ok is None
        assert span.attributes["cuprum.exit_code"] == 7

        hook(
            _make_exec_event(
                phase="settled",
                overrides={
                    **base,
                    "terminal_outcome": TerminalOutcome.EXIT_NONZERO,
                },
            )
        )

        assert span.ended is True
        assert span.status_ok is False
        assert span.attributes["cuprum.terminal_outcome"] == "exit_nonzero"
