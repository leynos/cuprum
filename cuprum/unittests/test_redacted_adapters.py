"""Projection-boundary tests for redaction-aware logging and tracing."""

from __future__ import annotations

import dataclasses as dc
import re
import typing as typ

import pytest

from cuprum.adapters.logging_adapter import _build_extra, structured_logging_hook
from cuprum.adapters.tracing_adapter import InMemoryTracer, TracingHook, tracing_hook
from cuprum.context import ScopeConfig, scoped
from cuprum.logging_hooks import logging_hook
from cuprum.redaction import RedactionError, RedactionPolicy
from cuprum.unittests._adapter_test_support import (
    _make_exec_event,
    _python_builder,
    capturing_logger,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.events import ExecPhase

_SENTINEL = "adapter-secret-442"
_POLICY = RedactionPolicy(literals=(_SENTINEL,))


@pytest.mark.parametrize("phase", ["plan", "stdout", "stderr"])
def test_structured_logging_redacts_messages_and_record_attributes(
    phase: ExecPhase,
) -> None:
    """The rendered and structured forms use the same sanitized event."""
    event = _make_exec_event(
        phase=phase,
        overrides={
            "argv": ("cat", _SENTINEL),
            "env": {"TOKEN": _SENTINEL},
            "line": _SENTINEL,
            "note": _SENTINEL,
            "tags": {"credential": _SENTINEL},
        },
    )
    with capturing_logger("cuprum.test.redacted.structured") as capture:
        structured_logging_hook(logger=capture.logger, redaction=_POLICY)(event)

    assert len(capture.records) == 1, "one event must produce one record"
    record = capture.records[0]
    assert _SENTINEL not in record.getMessage(), "rendered messages must be sanitized"
    assert _SENTINEL not in repr(vars(record)), "record attributes must be sanitized"
    assert vars(record)["cuprum_argv"] == ("cat", "[REDACTED]"), (
        "the shared projection must receive sanitized arguments"
    )
    assert event.argv[-1] == _SENTINEL, "the raw arguments must remain unchanged"
    assert event.line == _SENTINEL, "the raw output line must remain unchanged"


@pytest.mark.parametrize("use_factory", [False, True])
def test_tracing_redacts_attributes_and_diagnostic_events(use_factory: bool) -> None:
    """Both public constructors sanitize attributes, output, and notes."""
    tracer = InMemoryTracer()
    hook = (
        tracing_hook(tracer, redaction=_POLICY)
        if use_factory
        else TracingHook(tracer, redaction=_POLICY)
    )
    event = _make_exec_event(
        phase="start",
        overrides={
            "argv": ("cat", _SENTINEL),
            "env": {"TOKEN": _SENTINEL},
            "tags": {"pipeline_stages": _SENTINEL},
        },
    )
    hook(event)
    hook(dc.replace(event, phase="stdout", line=_SENTINEL))
    hook(dc.replace(event, phase="stdin_error", note=_SENTINEL))

    assert len(tracer.spans) == 1, "the correlated execution must create one span"
    span = tracer.spans[0]
    assert _SENTINEL not in repr(span), "span data must exclude configured secrets"
    assert span.attributes["cuprum.argv"] == ["cat", "[REDACTED]"], (
        "span attributes must use the sanitized argument vector"
    )
    assert span.attributes["cuprum.pipeline_stages"] == "[REDACTED]", (
        "string tag projections must use the same policy"
    )
    assert span.events == [
        ("cuprum.stdout", {"line": "[REDACTED]"}),
        ("cuprum.stdin_error", {"note": "[REDACTED]"}),
    ], "output and diagnostic notes must be sanitized before projection"
    assert event.tags["pipeline_stages"] == _SENTINEL, "raw tags must stay unchanged"


@pytest.mark.parametrize("redaction", [None, RedactionPolicy()])
def test_adapters_preserve_explicit_unredacted_defaults(
    redaction: RedactionPolicy | None,
) -> None:
    """An omitted or inert policy preserves existing exports independently."""
    event = _make_exec_event(phase="start", overrides={"argv": ("cat", _SENTINEL)})
    tracer = InMemoryTracer()
    with capturing_logger("cuprum.test.redacted.defaults") as capture:
        structured_logging_hook(logger=capture.logger, redaction=redaction)(event)
        TracingHook(tracer, redaction=redaction)(event)

    assert vars(capture.records[0])["cuprum_argv"][-1] == _SENTINEL, (
        "logging must remain opt-in"
    )
    assert tracer.spans[0].attributes["cuprum.argv"] == ["cat", _SENTINEL], (
        "tracing must remain opt-in"
    )
    sanitized = InMemoryTracer()
    TracingHook(sanitized, redaction=_POLICY)(event)
    assert _SENTINEL not in repr(sanitized.spans), "another adapter can opt in locally"
    assert _SENTINEL in repr(tracer.spans), "an opted-in adapter must not affect others"


@pytest.mark.parametrize(
    "phase", ["pipeline_fail_fast", "capture_eof_grace_expired", "group_teardown"]
)
def test_structured_logging_retains_phase_specific_projection(phase: ExecPhase) -> None:
    """Sanitization must not expand the bounded diagnostic field whitelists."""
    event = _make_exec_event(
        phase=phase,
        overrides={"argv": ("cat", _SENTINEL), "tags": {"credential": _SENTINEL}},
    )
    with capturing_logger("cuprum.test.redacted.diagnostics") as capture:
        structured_logging_hook(logger=capture.logger, redaction=_POLICY)(event)

    actual = {
        key: value
        for key, value in vars(capture.records[0]).items()
        if key.startswith("cuprum_")
    }
    assert actual == _build_extra(event), "existing diagnostic overrides must survive"
    assert "cuprum_argv" not in actual, "diagnostics must retain their bounded fields"


@pytest.mark.parametrize("redaction", [None, RedactionPolicy(), _POLICY])
def test_plain_logging_redacts_record_arguments_without_changing_execution(
    redaction: RedactionPolicy | None,
) -> None:
    """Plain logging projects argv locally while the child receives raw values."""
    builder, catalogue = _python_builder()
    command = builder("-c", "import sys; print(sys.argv[1])", _SENTINEL)
    with (
        capturing_logger("cuprum.test.redacted.plain") as capture,
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        logging_hook(logger=capture.logger, redaction=redaction),
    ):
        result = command.run_sync()

    assert result.stdout == _SENTINEL + "\n", "execution output must stay raw"
    assert command.argv[-1] == _SENTINEL, "the executed argv must stay raw"
    assert len(capture.records) == 2, "start and exit messages must remain enabled"
    if redaction is _POLICY:
        assert _SENTINEL not in repr([vars(record) for record in capture.records]), (
            "even unformatted record arguments must exclude configured secrets"
        )
        assert all(
            _SENTINEL not in record.getMessage() for record in capture.records
        ), "rendered plain log messages must be sanitized"
    else:
        assert _SENTINEL in capture.records[0].getMessage(), (
            "the omitted and inert defaults must retain verbatim argument logging"
        )


@pytest.mark.usefixtures("failing_redaction_substitution")
def test_failed_redaction_cannot_emit_a_record_or_create_a_span() -> None:
    """Projection failures propagate before either backend receives raw data."""
    policy = RedactionPolicy(patterns=(re.compile(r"."),))
    event = _make_exec_event(phase="start", overrides={"argv": ("cat", _SENTINEL)})
    tracer = InMemoryTracer()
    with capturing_logger("cuprum.test.redacted.failure") as capture:
        with pytest.raises(RedactionError):
            structured_logging_hook(logger=capture.logger, redaction=policy)(event)
        with pytest.raises(RedactionError):
            TracingHook(tracer, redaction=policy)(event)

    assert not capture.records, "a failing redactor must not emit a log record"
    assert not tracer.spans, "a failing redactor must not create a span"


@pytest.mark.usefixtures("failing_redaction_substitution")
def test_failed_plain_logging_redaction_prevents_execution(tmp_path: Path) -> None:
    """A before-hook failure propagates without logging or starting the child."""
    marker = tmp_path / "started"
    builder, catalogue = _python_builder()
    command = builder(
        "-c", "import pathlib, sys; pathlib.Path(sys.argv[1]).touch()", str(marker)
    )
    policy = RedactionPolicy(patterns=(re.compile(r"."),))
    with (
        capturing_logger("cuprum.test.redacted.plain-failure") as capture,
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        logging_hook(logger=capture.logger, redaction=policy),
        pytest.raises(RedactionError),
    ):
        command.run_sync()

    assert not marker.exists(), "a failed before hook must not execute the command"
    assert not capture.records, "plain logging must not fall back to raw arguments"
