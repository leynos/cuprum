"""Redaction scope and escaping for GitHub Actions presentation titles."""

from __future__ import annotations

import io
import re

import pytest

from cuprum.redaction import RedactionError, RedactionPolicy
from cuprum.sinks import (
    GitHubActionsSink,
    SessionOutcome,
    SessionStart,
    TerminalOutcome,
)

_SENTINEL = "argv-sentinel-442"
_POLICY = RedactionPolicy(literals=(_SENTINEL,))


@pytest.mark.parametrize("redaction", [None, RedactionPolicy(), _POLICY])
def test_group_titles_redact_only_when_configured(
    redaction: RedactionPolicy | None,
) -> None:
    """Configured titles hide argv values while defaults retain existing framing."""
    buffer = io.StringIO()
    start = SessionStart(label="project: cat", argv=("cat", _SENTINEL))
    sink = GitHubActionsSink(buffer, force=True, redaction=redaction)

    session = sink.open_session(start)
    assert session is not None, "a forced sink must activate"
    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=1))

    rendered = buffer.getvalue()
    expected_value = "[REDACTED]" if redaction is _POLICY else _SENTINEL
    assert rendered.startswith(f"::group::cat {expected_value}\n"), (
        "only an active configured policy must change the argv-derived title"
    )
    annotation = next(
        line for line in rendered.splitlines() if line.startswith("::error ")
    )
    assert annotation.startswith("::error title=project%3A cat::"), (
        "annotations must retain the escaped bounded label"
    )
    assert _SENTINEL not in annotation, "annotations must never carry argv"
    if redaction is _POLICY:
        assert _SENTINEL not in rendered, "configured framing must exclude the sentinel"
    assert start.argv == ("cat", _SENTINEL), "presentation must not mutate raw argv"


def test_redaction_precedes_workflow_command_escaping() -> None:
    """Rules match raw title text and their replacement is escaped afterwards."""
    buffer = io.StringIO()
    raw_value = "argv%\r\n442"
    policy = RedactionPolicy(
        patterns=(re.compile(re.escape(raw_value)),), mask="masked%\r\n"
    )
    sink = GitHubActionsSink(buffer, force=True, redaction=policy)
    session = sink.open_session(
        SessionStart(label="project: cat,step%", argv=("cat", raw_value))
    )
    assert session is not None, "a forced sink must activate"
    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=1))

    rendered = buffer.getvalue()
    assert rendered.startswith("::group::cat masked%25%0D%0A\n"), (
        "the raw match must be replaced before escaping the replacement"
    )
    assert "::error title=project%3A cat%2Cstep%25::" in rendered, (
        "property escaping must remain independent of title redaction"
    )
    assert "argv%25%0D%0A442" not in rendered, "escaping must not defeat the rule"


@pytest.mark.usefixtures("failing_redaction_substitution")
def test_failed_title_redaction_writes_nothing() -> None:
    """A failed rule propagates before a group or annotation can expose raw argv."""
    buffer = io.StringIO()
    sink = GitHubActionsSink(
        buffer,
        force=True,
        redaction=RedactionPolicy(patterns=(re.compile(r"."),)),
    )

    with pytest.raises(RedactionError) as raised:
        sink.open_session(SessionStart(label="project: cat", argv=("cat", _SENTINEL)))

    assert not buffer.getvalue(), "title failure must never retry with raw data"
    assert _SENTINEL not in str(raised.value), "failure metadata must exclude argv"


@pytest.mark.parametrize("title", [None, "trusted-caption"])
def test_trusted_labels_and_explicit_titles_stay_unchanged(title: str | None) -> None:
    """Only argv-derived titles are within the sink's redaction scope."""
    buffer = io.StringIO()
    policy = RedactionPolicy(literals=("trusted-label", "trusted-caption"))
    sink = GitHubActionsSink(buffer, force=True, title=title, redaction=policy)
    session = sink.open_session(SessionStart(label="trusted-label", argv=()))
    assert session is not None, "a forced sink must activate"
    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=1))

    label = title or "trusted-label"
    rendered = buffer.getvalue()
    assert rendered.startswith(f"::group::{label}\n"), (
        "trusted labels and explicit titles must remain caller-controlled"
    )
    assert f"::error title={label}::" in rendered, (
        "annotation labels must remain independent of argv redaction"
    )
