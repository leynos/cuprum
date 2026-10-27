"""Public-boundary coverage for stream-echo observation hooks."""

from __future__ import annotations

import codecs
import inspect
import io
import logging
import typing as typ

if typ.TYPE_CHECKING:
    import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum.echo_events import EchoErrorCategory, EchoEvent, EchoStream
from cuprum.echo_observation import observe_echo
from cuprum.sh import ExecutionContext, RunOutputOptions
from cuprum.unittests._hook_disposal_test_helpers import (
    _started_coroutine_raising,
    hook_returning,
)
from tests.helpers.catalogue import python_catalogue


def test_coroutine_disposal_failure_preserves_echoing_and_later_hooks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bad echo hook cannot fail a command or prevent later observers."""
    caplog.set_level(logging.WARNING, logger="cuprum.echo_observation")
    catalogue, python_program = python_catalogue()
    command = sh.make(python_program, catalogue=catalogue)
    payload = "echo-disposal-ś"
    coroutine = _started_coroutine_raising(RuntimeError("close failed"))
    received: list[EchoEvent] = []
    sink = typ.cast("typ.IO[str]", codecs.getwriter("cp1252")(io.BytesIO()))

    with (
        sink,
        scoped(ScopeConfig(allowlist=frozenset((python_program,)))),
        observe_echo(hook_returning(coroutine)),
        observe_echo(received.append),
    ):
        result = command(
            "-c",
            f"import sys; sys.stdout.write({payload!r})",
        ).run_sync(
            output=RunOutputOptions(capture=True, echo=True),
            context=ExecutionContext(
                stdout_sink=sink,
                stderr_sink=io.StringIO(),
            ),
        )

    assert result.ok, "disposal failure must not fail the public command"
    assert result.stdout == payload, "the command must preserve captured output"
    assert len(received) == 1, "the later hook must receive the echo event"
    assert received[0].stream is EchoStream.STDOUT, (
        "the public echo workflow must report stdout delivery"
    )
    assert received[0].error_category is EchoErrorCategory.UNICODE_ENCODE, (
        "the rejecting sink must report its encoding failure"
    )
    disposal_records = [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "echo_observer_disposal_failed"
    ]
    assert len(disposal_records) == 1, (
        "the started coroutine's close failure must be reported once"
    )
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED, (
        "the returned coroutine must be closed"
    )
