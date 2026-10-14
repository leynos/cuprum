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

from cuprum.context import UNSET, EnvMode
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
        literals=[_SENTINEL],
        patterns=[pattern],
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


@pytest.mark.usefixtures("failing_redaction_substitution")
def test_failed_pattern_raises_without_echoing_source_text() -> None:
    """A failed rule stops projection and exposes only safe failure metadata."""
    policy = RedactionPolicy(patterns=(re.compile(r"credential"),))

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
    ), "only the configured marker must change"


def test_pattern_mask_is_literal_and_optional_fields_stay_absent() -> None:
    """Replacement syntax is plain text and missing event fields stay missing."""
    policy = RedactionPolicy(patterns=(re.compile(r"secret"),), mask=r"\1\g<value>")
    event = dc.replace(_event(), env=None, line=None, note=None)

    assert policy.apply_text("secret") == r"\1\g<value>", "the mask must remain literal"
    assert policy.apply_text("unmatched") == "unmatched", "unmatched text must survive"
    projected = policy.redact_event(event)
    assert projected.env is None, "an absent environment must remain absent"
    assert projected.line is None, "an absent output line must remain absent"
    assert projected.note is None, "an absent note must remain absent"
    selected = {"argv", "env", "line", "note", "tags"}
    for field in dc.fields(event):
        if field.name not in selected:
            assert getattr(projected, field.name) == getattr(event, field.name), (
                f"unselected field {field.name} must remain unchanged"
            )


def test_observer_wrapper_isolates_sanitized_copies() -> None:
    """Wrapping one consumer does not hide values from other raw observers."""
    received: list[ExecEvent] = []
    event = _event()
    hook = redacting_hook(received.append, RedactionPolicy(literals=(_SENTINEL,)))

    assert hook(event) is None, "the synchronous consumer must return None"
    assert received[0].line == "[REDACTED]", "the consumer must receive redacted text"
    assert event.line == _SENTINEL, "the raw event must remain unchanged"


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
        assert result is not None, "the async consumer must return an awaitable"
        assert not received, "a consumer must not run before successful delivery"
        await result

    asyncio.run(run_case())
    assert received[0].line == "[REDACTED]", "the consumer must receive redacted text"


@pytest.mark.usefixtures("failing_redaction_substitution")
def test_failed_redaction_never_calls_the_observer() -> None:
    """A failing projection cannot fall back to passing the raw event."""
    received: list[ExecEvent] = []

    hook = redacting_hook(
        received.append, RedactionPolicy(patterns=(re.compile(r"."),))
    )
    with pytest.raises(RedactionError):
        hook(_event())
    assert not received, "a consumer must not run before successful delivery"


def test_single_rules_and_iterators_have_typed_constructor_support() -> None:
    """Supported input shapes produce the same immutable policy fields."""
    pattern = re.compile(r"session-[0-9]+")
    single = RedactionPolicy(literals=_SENTINEL, patterns=pattern)
    iterated = RedactionPolicy(literals=iter([_SENTINEL]), patterns=iter([pattern]))

    assert single == iterated, "input shapes must not alter policy meaning"
    assert single.literals == (_SENTINEL,), "stored literals remain tuple-typed"
    assert single.patterns == (pattern,), "stored patterns remain tuple-typed"
    changed = dc.replace(single, mask="<masked>")
    assert changed.apply_text(_SENTINEL) == "<masked>", (
        "dataclass replacement must retain constructor normalization"
    )


@pytest.mark.parametrize(
    "literals",
    [("tok", "token-123"), ("token-123", "tok")],
)
def test_overlapping_literals_mask_the_complete_longer_value(
    literals: tuple[str, ...],
) -> None:
    """A shorter configured prefix must not leave a longer secret's suffix."""
    policy = RedactionPolicy(literals=literals)

    assert policy.apply_text("token-123 tok") == "[REDACTED] [REDACTED]", (
        "overlapping literals must be fully masked regardless of rule order"
    )


def test_trusted_fields_keys_and_environment_markers_are_outside_text_scope() -> None:
    """Only selected values change even when other fields match literal rules."""
    event = dc.replace(
        _event(),
        env={"VARIABLE": _SENTINEL, "DELETE": UNSET},
        env_mode=EnvMode.REPLACE,
    )
    policy = RedactionPolicy(
        literals=(
            "python",
            "trusted-project",
            "credential",
            "VARIABLE",
            "replace",
            _SENTINEL,
        ),
    )
    projected = policy.redact_event(event)

    assert projected.program == event.program, "trusted programs must stay unchanged"
    assert projected.project == event.project, "trusted projects must stay unchanged"
    assert projected.env_mode is EnvMode.REPLACE, "typed enums must stay unchanged"
    assert projected.argv[0] == "[REDACTED]", "argv uses value redaction independently"
    assert projected.tags["credential"] == "[REDACTED]", "tag keys must stay unchanged"
    assert projected.env is not None, "the environment overlay must remain present"
    assert projected.env["VARIABLE"] == "[REDACTED]", "environment names must survive"
    assert projected.env["DELETE"] is UNSET, "deletion markers must stay unchanged"
