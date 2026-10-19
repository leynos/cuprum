"""Command output behaviour when bounded delivery is enabled."""

from __future__ import annotations

import asyncio
import io
import typing as typ

import pytest

from cuprum.echo_events import EchoErrorCategory, EchoEvent, EchoStream, RelayFallback
from cuprum.echo_observation import observe_echo
from cuprum.sh import BoundedDelivery, CommandResult, ExecutionContext, RunOutputOptions
from tests.helpers.catalogue import python_builder

if typ.TYPE_CHECKING:
    from cuprum.sh import SafeCmd


class _FailingDeliverySink(io.StringIO):
    """Raise at one selected destination operation."""

    def __init__(self, failure_site: str) -> None:
        """Select whether ``write`` or ``flush`` fails."""
        super().__init__()
        self.failure_site = failure_site

    def write(self, text: str, /) -> int:
        """Fail the configured write, otherwise store the text."""
        if self.failure_site == "write":
            raise OSError
        return super().write(text)

    def flush(self) -> None:
        """Fail the configured flush, otherwise flush the string buffer."""
        if self.failure_site == "flush":
            raise OSError
        super().flush()


def _run_command(
    command: SafeCmd,
    *,
    is_sync: bool,
    output: RunOutputOptions,
    context: ExecutionContext,
) -> CommandResult:
    """Run a command through the selected public entry point."""
    if is_sync:
        return command.run_sync(output=output, context=context)
    return asyncio.run(command.run(output=output, context=context))


@pytest.mark.parametrize("is_sync", [False, True], ids=["run", "run-sync"])
def test_bounded_delivery_echoes_off_loop_and_preserves_capture(
    is_sync: bool,
) -> None:
    """The opt-in worker delivers output without changing command capture."""
    stdout_sink = io.StringIO()
    command = python_builder()("-c", 'import sys; sys.stdout.write("delivered")')

    result = _run_command(
        command,
        is_sync=is_sync,
        output=RunOutputOptions(
            capture=True,
            echo_stdout=True,
            echo_stderr=False,
            delivery=BoundedDelivery(max_queued_bytes=128),
        ),
        context=ExecutionContext(stdout_sink=stdout_sink),
    )

    assert result.exit_code == 0
    assert result.stdout == "delivered"
    assert stdout_sink.getvalue() == "delivered"
    assert result.relay_fallbacks == ()


@pytest.mark.parametrize("failure_site", ["write", "flush"])
def test_bounded_delivery_sink_failures_are_reported_without_failing_run(
    failure_site: str,
) -> None:
    """Worker sink errors stay categorical and leave command outcomes intact."""
    sink = _FailingDeliverySink(failure_site)
    events: list[EchoEvent] = []
    command = python_builder()("-c", 'import sys; sys.stdout.write("captured")')

    with observe_echo(events.append):
        result = command.run_sync(
            output=RunOutputOptions(
                capture=True,
                echo_stdout=True,
                echo_stderr=False,
                delivery=BoundedDelivery(max_queued_bytes=128),
            ),
            context=ExecutionContext(stdout_sink=sink),
        )

    expected_fallback = RelayFallback(
        stream=EchoStream.STDOUT,
        error_category=EchoErrorCategory.DELIVERY_ERROR,
    )
    assert result.exit_code == 0
    assert result.stdout == "captured"
    assert result.relay_fallbacks == (expected_fallback,)
    assert events == [
        EchoEvent(
            stream=EchoStream.STDOUT,
            error_category=EchoErrorCategory.DELIVERY_ERROR,
        ),
    ]


@pytest.mark.parametrize("is_sync", [False, True], ids=["run", "run-sync"])
def test_bounded_delivery_saturation_keeps_capture_and_reports_source_bytes(
    is_sync: bool,
) -> None:
    """A saturated queue drops presentation but retains the full capture."""
    payload = "queue overflow"
    sink = io.StringIO()
    events: list[EchoEvent] = []
    command = python_builder()("-c", f"import sys; sys.stdout.write({payload!r})")

    with observe_echo(events.append):
        result = _run_command(
            command,
            is_sync=is_sync,
            output=RunOutputOptions(
                capture=True,
                echo_stdout=True,
                echo_stderr=False,
                delivery=BoundedDelivery(max_queued_bytes=1),
            ),
            context=ExecutionContext(stdout_sink=sink),
        )

    assert result.stdout == payload
    assert not sink.getvalue()
    expected_fallback = RelayFallback(
        stream=EchoStream.STDOUT,
        error_category=EchoErrorCategory.DROPPED,
    )
    assert result.relay_fallbacks == (expected_fallback,)
    assert events == [
        EchoEvent(
            stream=EchoStream.STDOUT,
            error_category=EchoErrorCategory.DROPPED,
            dropped_bytes=len(payload.encode()),
        ),
    ]


def test_bounded_delivery_accounts_truncated_echo_by_original_bytes() -> None:
    """A truncation marker does not count as source output in the queue bound."""
    payload = "x" * 80 + "\n"
    sink = io.StringIO()
    events: list[EchoEvent] = []
    command = python_builder()("-c", f"import sys; sys.stdout.write({payload!r})")

    with observe_echo(events.append):
        result = command.run_sync(
            output=RunOutputOptions(
                capture=True,
                echo_stdout=True,
                echo_stderr=False,
                max_echo_line_bytes=40,
                delivery=BoundedDelivery(max_queued_bytes=len(payload.encode())),
            ),
            context=ExecutionContext(stdout_sink=sink),
        )

    assert result.stdout == payload
    assert sink.getvalue()
    assert len(sink.getvalue().encode()) <= 40
    assert [event.error_category for event in events] == [
        EchoErrorCategory.TRUNCATED,
    ]
