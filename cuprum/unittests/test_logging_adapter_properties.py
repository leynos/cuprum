"""Property tests for the structured logging hook.

`structured_logging_hook` is a pure per-event function: it maps a phase to a
level, builds an ``extra`` mapping, formats a message, and emits exactly one
record. It holds no active map, so a `RuleBasedStateMachine` would generate
interleavings that cannot distinguish any two implementations — the tracing and
metrics hooks earn one because they accumulate state across events, and this
one does not.

What can go wrong is per-event and shape-dependent, which is what these
properties cover: an extra key colliding with a reserved `LogRecord` attribute
(a `KeyError` raised inside the user's logging stack, not in cuprum), a phase
falling through the level map, and a value that `JsonLoggingFormatter` cannot
serialize. Each fails only for particular event shapes that hand-written cases
are unlikely to enumerate.
"""

from __future__ import annotations

import json
import logging
import typing as typ

from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum.adapters.logging_adapter import (
    JsonLoggingFormatter,
    LogLevels,
    _format_message,
    structured_logging_hook,
)
from cuprum.events import ExecEvent

if typ.TYPE_CHECKING:
    from cuprum.events import ExecPhase
    from cuprum.program import Program

_KNOWN_PHASES = (
    "plan",
    "start",
    "stdout",
    "stderr",
    "stdin",
    "stdin_error",
    "timeout",
    "teardown_error",
    "capture_eof_grace_expired",
    "exit",
    "pipeline_fail_fast",
)

# Reserved names live directly on every LogRecord. Passing any of them through
# `extra=` makes `Logger.makeRecord` raise, so the `cuprum_` prefix is what
# keeps the hook from breaking a caller's logging configuration.
_RESERVED_RECORD_ATTRIBUTES = frozenset(
    vars(logging.LogRecord("n", 0, "p", 0, "m", (), None))
)


class _CollectingHandler(logging.Handler):
    """Handler retaining every record it is given."""

    def __init__(self) -> None:
        """Start with no records collected."""
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Retain ``record`` for inspection."""
        self.records.append(record)


def _capturing_logger(name: str) -> tuple[logging.Logger, _CollectingHandler]:
    """Return a logger that captures everything, plus its handler."""
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = False
    # Avoid inheriting the root WARNING level; the hook checks isEnabledFor.
    logger.setLevel(logging.DEBUG)
    handler = _CollectingHandler()
    logger.addHandler(handler)
    return logger, handler


_awkward_text = st.text(
    # Control characters, quotes, and non-BMP scalars all have to survive the
    # JSON round trip, so generate them rather than plain ASCII.
    alphabet=st.characters(min_codepoint=1, max_codepoint=0x10FFFF),
    max_size=40,
)


class _Opaque:
    """A tag value with no JSON representation of its own."""

    def __repr__(self) -> str:
        """Render a stable placeholder for the coerced form."""
        return "<opaque>"


# `ExecEvent.tags` is typed `Mapping[str, object]`, so a caller may attach any
# object. Generating only strings would make the JSON property vacuous: it is
# precisely these values that `_json_serializable` and `default=str` exist for.
_tag_values = st.one_of(
    _awkward_text,
    st.integers(),
    st.booleans(),
    st.none(),
    st.builds(_Opaque),
    st.lists(st.builds(_Opaque), max_size=2),
    st.dictionaries(_awkward_text, st.builds(_Opaque), max_size=2),
)


@st.composite
def _events(draw: st.DrawFn) -> ExecEvent:
    """Draw an event with phase-appropriate fields and awkward text."""
    phase = draw(st.sampled_from([*_KNOWN_PHASES, "unheard-of"]))
    return ExecEvent(
        phase=typ.cast("ExecPhase", phase),
        program=typ.cast("Program", draw(_awkward_text)),
        argv=tuple(draw(st.lists(_awkward_text, max_size=3))),
        cwd=None,
        env=None,
        pid=draw(st.none() | st.integers(min_value=0, max_value=1 << 20)),
        timestamp=0.0,
        line=draw(st.none() | _awkward_text),
        exit_code=draw(st.none() | st.integers(min_value=-3, max_value=3)),
        duration_s=draw(
            st.none()
            | st.floats(
                min_value=0.0,
                max_value=100.0,
                allow_nan=False,
                allow_infinity=False,
            ),
        ),
        tags=dict(
            draw(st.lists(st.tuples(_awkward_text, _tag_values), max_size=3)),
        ),
        byte_count=draw(st.none() | st.integers(min_value=0, max_value=1 << 20)),
    )


@given(event=_events())
@settings(max_examples=200)
def test_every_event_emits_exactly_one_record(event: ExecEvent) -> None:
    """No phase is silently dropped, and none emits twice."""
    logger, handler = _capturing_logger("cuprum.test.one_record")
    structured_logging_hook(logger=logger)(event)

    assert len(handler.records) == 1, (
        f"phase {event.phase!r} emitted {len(handler.records)} records, expected 1"
    )


@given(event=_events())
@settings(max_examples=200)
def test_extras_cannot_collide_with_reserved_record_attributes(
    event: ExecEvent,
) -> None:
    """Every attached field is ``cuprum_``-prefixed, so nothing is shadowed.

    A collision does not fail in cuprum: ``Logger.makeRecord`` raises a
    ``KeyError`` deep inside the caller's logging stack, which is expensive to
    trace back here.
    """
    logger, handler = _capturing_logger("cuprum.test.reserved")
    structured_logging_hook(logger=logger)(event)

    record = handler.records[0]
    attached = set(vars(record)) - _RESERVED_RECORD_ATTRIBUTES
    unprefixed = {name for name in attached if not name.startswith("cuprum_")}
    assert not unprefixed, (
        f"every attached field must be cuprum_-prefixed, found {unprefixed}"
    )
    assert attached, "the hook must attach the structured fields it documents"


@given(event=_events())
@settings(max_examples=200)
def test_records_survive_the_json_formatter(event: ExecEvent) -> None:
    """Any event formats to JSON that ``json.loads`` accepts."""
    logger, handler = _capturing_logger("cuprum.test.json")
    structured_logging_hook(logger=logger)(event)

    rendered = JsonLoggingFormatter().format(handler.records[0])
    decoded = json.loads(rendered)

    assert decoded["cuprum_phase"] == event.phase, (
        "the decoded record must report the event's own phase"
    )
    assert decoded["message"], "a formatted record must carry a non-empty message"


@given(event=_events())
@settings(max_examples=200)
def test_message_formatting_is_total(event: ExecEvent) -> None:
    """Every phase formats to a non-empty message, known or not."""
    message = _format_message(event)

    assert message, f"phase {event.phase!r} produced an empty message"
    assert message.startswith("cuprum."), (
        f"messages must be namespaced, found {message!r}"
    )


@given(
    event=_events(),
    levels=st.builds(
        LogLevels,
        plan_level=st.sampled_from([logging.DEBUG, logging.INFO]),
        start_level=st.sampled_from([logging.INFO, logging.WARNING]),
        output_level=st.sampled_from([logging.DEBUG, logging.INFO]),
        exit_level=st.sampled_from([logging.INFO, logging.ERROR]),
        fail_fast_level=st.sampled_from([logging.WARNING, logging.ERROR]),
    ),
)
@settings(max_examples=200)
def test_each_phase_logs_at_its_configured_level(
    event: ExecEvent,
    levels: LogLevels,
) -> None:
    """Phases with a configured level use it; others fall back to ``DEBUG``.

    ``plan``, ``start``, ``stdout``, ``stderr``, and ``exit`` are mapped to
    ``levels`` and must log at their configured level. ``stdin`` and
    ``stdin_error`` are known phases that have no entry in the mapping, and
    any unknown phases must fall back to ``logging.DEBUG`` rather than raise
    — phases are part of the event contract and may grow, and a logging hook
    is the wrong place to discover that.

    The expected level is derived here independently rather than by accepting
    any configured value: a permissive check passes even if a phase is dropped
    from the map entirely and silently falls back to ``DEBUG``.
    """
    logger, handler = _capturing_logger("cuprum.test.levels")
    structured_logging_hook(logger=logger, levels=levels)(event)

    expected = {
        "plan": levels.plan_level,
        "start": levels.start_level,
        "stdout": levels.output_level,
        "stderr": levels.output_level,
        "exit": levels.exit_level,
        "pipeline_fail_fast": levels.fail_fast_level,
    }.get(event.phase, logging.DEBUG)

    assert handler.records[0].levelno == expected, (
        f"phase {event.phase!r} logged at {handler.records[0].levelname}, "
        f"expected {logging.getLevelName(expected)}"
    )
