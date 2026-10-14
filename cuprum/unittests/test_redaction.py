"""Tests for opt-in projection redaction."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import re
import typing as typ
from types import MappingProxyType

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum.events import ExecEvent
from cuprum.program import Program
from cuprum.redaction import RedactionError, RedactionPolicy, redacting_hook

_SENTINEL = "telemetry-secret-442"


def _event() -> ExecEvent:
    """Build one event carrying the sentinel through every redactable field."""
    return ExecEvent(
        phase="stdout",
        program=Program("python"),
        argv=("python", f"--value={_SENTINEL}"),
        cwd=None,
        env=MappingProxyType({"VARIABLE": _SENTINEL, "REMOVE": "kept"}),
        pid=37,
        timestamp=1.5,
        line=_SENTINEL,
        exit_code=None,
        duration_s=None,
        tags=MappingProxyType({"credential": _SENTINEL, "attempt": 2}),
        note=f"failure: {_SENTINEL}",
        project="trusted-project",
        stage_index=1,
    )


def test_default_policy_is_inert_and_returns_the_original_event() -> None:
    """The explicit default keeps existing projection behaviour unchanged."""
    event = _event()
    policy = RedactionPolicy()

    assert policy.literals == (), "the default literal-rule set must be empty"
    assert policy.patterns == (), "the default pattern-rule set must be empty"
    assert policy.mask == "[REDACTED]", "the replacement default must be stable"
    assert policy.apply_text(_SENTINEL) == _SENTINEL, (
        "an inert policy must leave arbitrary text untouched"
    )
    assert policy.redact_event(event) is event, (
        "an inert policy must not allocate a projection copy"
    )


def test_rule_inputs_are_coerced_and_validated() -> None:
    """Mutable rule collections become immutable and malformed rules fail early."""
    pattern = re.compile(r"session-[0-9]+")
    policy = RedactionPolicy(
        literals=typ.cast("tuple[str, ...]", [_SENTINEL]),
        patterns=typ.cast("tuple[re.Pattern[str], ...]", [pattern]),
    )

    assert policy.literals == (_SENTINEL,), "literal rules must be snapshotted"
    assert policy.patterns == (pattern,), "pattern rules must be snapshotted"
    bytes_pattern = typ.cast("re.Pattern[str]", re.compile(rb"bytes-only"))
    with pytest.raises(TypeError, match="compiled text regular expressions"):
        RedactionPolicy(patterns=(bytes_pattern,))
    with pytest.raises(ValueError, match="empty strings"):
        RedactionPolicy(literals=("",))


def test_apply_text_masks_literals_and_pattern_matches() -> None:
    """Literal and regular-expression rules replace text with the same mask."""
    policy = RedactionPolicy(
        literals=(_SENTINEL,),
        patterns=(re.compile(r"session-[0-9]+"),),
        mask="<masked>",
    )

    redacted = policy.apply_text(f"{_SENTINEL} session-314")

    assert redacted == "<masked> <masked>", (
        "each configured rule must use the configured replacement"
    )


def test_failed_pattern_raises_without_echoing_source_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed rule stops projection and exposes only safe failure metadata."""
    policy = RedactionPolicy(patterns=(re.compile(r"credential"),))

    def fail_substitution(
        _pattern: re.Pattern[str],
        value: str,
        _mask: str,
    ) -> str:
        """Model a rule whose exception embeds its input."""
        raise ValueError(value)

    monkeypatch.setattr("cuprum.redaction._substitute_pattern", fail_substitution)

    with pytest.raises(RedactionError) as raised:
        policy.apply_text(_SENTINEL)

    error = raised.value
    assert error.rule_kind == "pattern", "the exception must identify its rule kind"
    assert error.rule_index == 0, "the exception must identify its rule position"
    assert error.error_type == "ValueError", "the exception type must be recorded"
    assert _SENTINEL not in str(error), "failure diagnostics must not echo input"
    assert error.__suppress_context__, "raw rule errors must not appear in tracebacks"
    assert error.__context__ is None, "the wrapped exception must not retain raw text"


def test_redact_event_copies_only_the_selected_text_fields() -> None:
    """The projection sanitizes selected strings without changing the source."""
    event = _event()
    policy = RedactionPolicy(literals=(_SENTINEL,))

    projected = policy.redact_event(event)

    assert projected is not event, "an active policy must return a copy"
    assert projected.argv == ("python", "--value=[REDACTED]"), (
        "each argv item must be redacted independently"
    )
    projected_env = projected.env
    assert projected_env is not None, "the environment overlay must remain present"
    assert projected_env["VARIABLE"] == "[REDACTED]", (
        "environment values must be sanitized without changing their names"
    )
    assert projected_env["REMOVE"] == "kept", "unmatched values must survive"
    assert projected.line == "[REDACTED]", "output lines must be sanitized"
    assert projected.note == "failure: [REDACTED]", "notes must be sanitized"
    assert projected.tags == {"credential": "[REDACTED]", "attempt": 2}, (
        "string tag values must be sanitized while other values survive"
    )
    assert tuple(projected.tags) == ("credential", "attempt"), (
        "tag keys and their order must remain stable"
    )
    assert projected.program is event.program, "the allowlisted program is trusted"
    assert projected.project == event.project, "the trusted project is unchanged"
    assert projected.pid == event.pid, "the process id must remain unchanged"
    assert projected.stage_index == event.stage_index, (
        "the stage index must remain unchanged"
    )
    assert event.argv == ("python", f"--value={_SENTINEL}"), (
        "redaction must not mutate executed arguments"
    )
    raw_env = event.env
    assert raw_env is not None, "the source environment overlay must remain present"
    assert raw_env["VARIABLE"] == _SENTINEL, (
        "the child environment must remain unchanged"
    )
    assert event.line == _SENTINEL, "redaction must not alter captured output"
    assert event.note == f"failure: {_SENTINEL}", "redaction must not alter diagnostics"
    assert event.tags["credential"] == _SENTINEL, (
        "redaction must not mutate the raw event tags"
    )


@given(
    prefix=st.text(alphabet=st.characters(blacklist_characters="!")),
    suffix=st.text(alphabet=st.characters(blacklist_characters="!")),
)
def test_literal_redaction_preserves_surrounding_unicode(
    prefix: str,
    suffix: str,
) -> None:
    """Only the configured marker changes across arbitrary surrounding text."""
    policy = RedactionPolicy(literals=("!secret!",))

    assert policy.apply_text(prefix + "!secret!" + suffix) == (
        prefix + "[REDACTED]" + suffix
    )


def test_pattern_mask_is_literal_and_optional_fields_stay_absent() -> None:
    """Replacement syntax is plain text and missing event fields stay missing."""
    policy = RedactionPolicy(patterns=(re.compile(r"secret"),), mask=r"\1\g<value>")
    event = dc.replace(_event(), env=None, line=None, note=None)

    assert policy.apply_text("secret") == r"\1\g<value>"
    assert policy.apply_text("unmatched") == "unmatched"
    projected = policy.redact_event(event)
    assert projected.env is None
    assert projected.line is None
    assert projected.note is None
    selected = {"argv", "env", "line", "note", "tags"}
    for field in dc.fields(event):
        if field.name not in selected:
            assert getattr(projected, field.name) == getattr(event, field.name)


def test_observer_wrapper_isolates_sanitized_copies() -> None:
    """Wrapping one consumer does not hide values from other raw observers."""
    received: list[ExecEvent] = []
    event = _event()
    hook = redacting_hook(received.append, RedactionPolicy(literals=(_SENTINEL,)))

    assert hook(event) is None
    assert received[0].line == "[REDACTED]"
    assert event.line == _SENTINEL


def test_observer_wrapper_preserves_async_consumers() -> None:
    """The emitter can await an async consumer returned by the wrapper."""
    received: list[ExecEvent] = []

    async def consume(event: ExecEvent) -> None:
        """Store the projected event after yielding to the event loop."""
        await asyncio.sleep(0)
        received.append(event)

    async def run_case() -> None:
        """Await the wrapped consumer and verify deferred delivery."""
        result = redacting_hook(consume, RedactionPolicy(literals=(_SENTINEL,)))(
            _event()
        )
        assert result is not None
        assert not received
        await result

    asyncio.run(run_case())
    assert received[0].line == "[REDACTED]"


def test_failed_redaction_never_calls_the_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing projection cannot fall back to passing the raw event."""
    received: list[ExecEvent] = []

    def fail_substitution(
        _pattern: re.Pattern[str],
        value: str,
        _mask: str,
    ) -> str:
        """Model a rule whose exception embeds its input."""
        raise ValueError(value)

    monkeypatch.setattr("cuprum.redaction._substitute_pattern", fail_substitution)
    hook = redacting_hook(
        received.append, RedactionPolicy(patterns=(re.compile(r"."),))
    )
    with pytest.raises(RedactionError):
        hook(_event())
    assert not received
