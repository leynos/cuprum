"""Every way a reported run can end, and what it leaves behind.

A watchdog is a second owner of the run's lifetime, so each exit path has to
give it back exactly once: ordinary completion, a deadline, cancellation, a
spawn that never happened, a callback that failed or returned the wrong thing,
a destination that will not accept a line, and a child that exited while a
grandchild held the pipe open. What these tests watch for is the aftermath --
a stray task, a second warning, a keepalive offered as child output -- and the
stream policy that decides whether an idle run drains a pipe it will not keep;
the reporting itself, and what resets it, is pinned in the execution tests.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import time
import typing as typ

import pytest

from cuprum._idle_heartbeat import _build_idle_monitor
from cuprum._pipeline_types import _ExecutionHooks, _StageObservation
from cuprum._subprocess_execution import _SubprocessExecution
from cuprum.sh import ExecutionContext, RunOutputOptions, TimeoutExpired
from tests.helpers.catalogue import python_builder as build_python_builder
from tests.helpers.idle import keepalives, pending_tasks
from tests.helpers.stream_pipes import drain_blocking_payload_size

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._idle_heartbeat import _IdleMonitor
    from cuprum.sh import SafeCmd

_MAX_LINE_BYTES = 512
# Long enough that a channel which failed had several further chances to warn.
_QUIET_SECONDS = 0.35


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    cabc.Callable[..., SafeCmd]
        A builder that creates SafeCmd instances for the running interpreter.
    """
    return build_python_builder()


class _UnwritableSink:
    """Text sink whose every write fails, like a log destination gone away."""

    def write(self, _payload: str) -> int:
        """Fail the way a broken destination does."""
        msg = "the destination is gone"
        raise OSError(msg)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


def _execution(
    command: SafeCmd,
    *,
    capture: bool,
    echo: bool,
    idle: _IdleMonitor | None,
) -> _SubprocessExecution:
    """Build the execution bundle a single-command run would build."""
    return _SubprocessExecution(
        cmd=command,
        ctx=ExecutionContext(),
        capture=capture,
        echo_stdout=echo,
        echo_stderr=echo,
        max_echo_line_bytes=None,
        timeout=None,
        observation=_StageObservation(
            cmd=command,
            hooks=_ExecutionHooks(before_hooks=(), after_hooks=(), observe_hooks=()),
            tags={},
            cwd=None,
            env_overlay=None,
            pending_tasks=[],
            wall_clock=time.monotonic,
        ),
        stdin_data=None,
        idle=idle,
    )


def test_disabled_reporting_asks_the_parent_to_consume_nothing(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The default options buy no watchdog, no timer, and no extra pipe."""
    sink = io.StringIO()
    command = python_builder("-c", "import time; time.sleep(0.3); print('done')")

    result = asyncio.run(command.run(context=ExecutionContext(stderr_sink=sink)))

    assert not keepalives(sink), (
        f"a run without idle_after must stay silent for sink={sink.getvalue()!r}"
    )
    assert result.stdout == "done\n", "the default run must still capture output"
    assert _consumes(command, idle=None) == (False, False), (
        "a disabled heartbeat must leave both streams unconsumed"
    )
    armed = _build_idle_monitor(30.0, None, "cargo")
    assert armed is not None, "an interval must build a monitor"
    assert _consumes(command, idle=armed) == (True, True), (
        "idle reporting must ask the parent to drain both streams"
    )


def _consumes(command: SafeCmd, *, idle: _IdleMonitor | None) -> tuple[bool, bool]:
    """Report whether an idle run must drain stdout and stderr."""
    execution = _execution(command, capture=False, echo=False, idle=idle)
    return execution.consumes_stdout, execution.consumes_stderr


def test_watching_without_capturing_still_drains_both_streams(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A payload larger than the pipe buffer must not wedge an idle run."""
    payload = drain_blocking_payload_size()
    source = (
        f"import sys; data = 'x' * {payload}; "
        "sys.stdout.write(data); sys.stdout.flush(); "
        "sys.stderr.write(data); sys.stderr.flush()"
    )
    command = python_builder("-c", source)

    result = asyncio.run(
        command.run(
            output=RunOutputOptions(capture=False, echo=False, idle_after=0.2),
            context=ExecutionContext(stderr_sink=io.StringIO()),
        ),
    )

    assert result.exit_code == 0, (
        f"a drained run must not block its child: {result.exit_code!r}"
    )
    assert result.stdout is None, f"observation must retain nothing: {result.stdout!r}"
    assert result.stderr is None, f"observation must retain nothing: {result.stderr!r}"


def test_silent_diagnostic_destination_disables_the_channel_once(
    python_builder: cabc.Callable[..., SafeCmd],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A destination that refuses a line costs one warning, not the run."""
    command = python_builder("-c", "import time; time.sleep(0.3); print('done')")

    with caplog.at_level("WARNING", logger="cuprum.idle"):
        result = asyncio.run(
            command.run(
                output=RunOutputOptions(idle_after=0.08),
                context=ExecutionContext(
                    stderr_sink=typ.cast("typ.IO[str]", _UnwritableSink()),
                ),
            ),
        )

    assert result.stdout == "done\n", f"the run must be unaffected: {result!r}"
    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["idle_notification_disabled error=OSError"], (
        "exactly one sanitized warning must be reported for "
        f"a destination that refused a line, got {messages!r}"
    )


def test_failing_callback_disables_reporting_without_failing_the_run(
    python_builder: cabc.Callable[..., SafeCmd],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken callback costs the run its keepalives, not its result."""
    sink = io.StringIO()

    def on_idle(_elapsed_total: float, _elapsed_idle: float) -> None:
        """Fail the way an ordinary callback bug would."""
        msg = "callback exploded"
        raise RuntimeError(msg)

    command = python_builder("-c", "import time; time.sleep(0.3); print('done')")
    with caplog.at_level("WARNING", logger="cuprum.idle"):
        result = asyncio.run(
            command.run(
                output=RunOutputOptions(idle_after=0.08, on_idle=on_idle),
                context=ExecutionContext(stderr_sink=sink),
            ),
        )

    assert result.stdout == "done\n", f"the run must be unaffected: {result!r}"
    assert not keepalives(sink), (
        f"a caller callback replaces the renderer for sink={sink.getvalue()!r}"
    )
    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["idle_notification_disabled error=RuntimeError"], (
        f"exactly one sanitized warning must be reported, got {messages!r}"
    )


def test_coroutine_callback_return_is_closed_and_reported(
    python_builder: cabc.Callable[..., SafeCmd],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An accidentally asynchronous callback is closed, not leaked."""
    returned: list[object] = []

    async def rendered() -> None:
        """Stand in for a coroutine the caller never awaits."""

    def on_idle(_elapsed_total: float, _elapsed_idle: float) -> object:
        """Return a coroutine, the way a mistaken async callback does."""
        coroutine = rendered()
        returned.append(coroutine)
        return coroutine

    command = python_builder("-c", "import time; time.sleep(0.3); print('done')")
    with caplog.at_level("WARNING", logger="cuprum.idle"):
        result = asyncio.run(
            command.run(
                output=RunOutputOptions(
                    idle_after=0.08,
                    on_idle=typ.cast("cabc.Callable[[float, float], None]", on_idle),
                ),
                context=ExecutionContext(stderr_sink=io.StringIO()),
            ),
        )

    assert result.stdout == "done\n", f"the run must be unaffected: {result!r}"
    assert returned, "the callback must have been invoked"
    states = [
        inspect.getcoroutinestate(item)
        for item in returned
        if inspect.iscoroutine(item)
    ]
    assert states, "the returned values must have been coroutines"
    assert set(states) == {inspect.CORO_CLOSED}, (
        f"every returned coroutine must be closed, got {states!r}"
    )
    messages = [record.getMessage() for record in caplog.records]
    assert "idle_callback_returned_value result_type=coroutine" in messages, (
        f"the return value must be reported once, got {messages!r}"
    )


def test_control_flow_callback_failure_is_not_swallowed(
    python_builder: cabc.Callable[..., SafeCmd],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A callback's ``KeyboardInterrupt`` escapes the run, plainly.

    ``asyncio`` re-raises control flow out of the loop, so the run's watchdog is
    what is left to give back -- and the sanitized driver-failure warning it
    logs instead is the evidence that the shielded cleanup gave it back.
    """
    sink = io.StringIO()
    command = python_builder("-c", f"import time; time.sleep({_QUIET_SECONDS})")
    invocations: list[int] = []

    def on_idle(_elapsed_total: float, _elapsed_idle: float) -> None:
        """Raise the control-flow exception a handler must not absorb."""
        invocations.append(1)
        raise KeyboardInterrupt

    with (
        caplog.at_level("WARNING", logger="cuprum.idle"),
        pytest.raises(KeyboardInterrupt),
    ):
        asyncio.run(
            command.run(
                output=RunOutputOptions(idle_after=0.05, on_idle=on_idle),
                context=ExecutionContext(stderr_sink=sink),
            ),
        )

    assert invocations, "the callback must have been invoked"
    assert not keepalives(sink), (
        f"a caller callback replaces the renderer for sink={sink.getvalue()!r}"
    )
    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["idle_heartbeat_failed error=KeyboardInterrupt"], (
        "a control-flow failure must be named as such rather than contained as "
        f"an ordinary one, got {messages!r}"
    )


def test_timeout_keeps_the_heartbeat_out_of_the_reported_output(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A deadline ends the run on schedule, with no keepalive in its output."""
    sink = io.StringIO()
    command = python_builder("-c", "import time; time.sleep(5)")

    with pytest.raises(TimeoutExpired) as caught:
        asyncio.run(
            command.run(
                output=RunOutputOptions(idle_after=0.1),
                timeout=0.4,
                context=ExecutionContext(stderr_sink=sink),
            ),
        )

    assert keepalives(sink), "the timeout run must still have been reported"
    reported = caught.value.output or ""
    assert isinstance(reported, str), (
        f"a capturing timeout must report text, got {reported!r}"
    )
    assert "[cuprum]" not in reported, (
        f"the keepalive must not be reported as child output: {reported!r}"
    )


def test_cancellation_leaves_no_watchdog_or_consumer_behind(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Cancelling a reported run settles every task the run owned."""
    sink = io.StringIO()
    command = python_builder("-c", "import time; time.sleep(5)")

    async def exercise() -> tuple[list[str], list[asyncio.Task[object]]]:
        """Cancel a running command twice, then look for stragglers."""
        task = asyncio.create_task(
            command.run(
                output=RunOutputOptions(idle_after=0.05),
                context=ExecutionContext(stderr_sink=sink),
            ),
        )
        await asyncio.sleep(0.25)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # A second cancellation must not resurrect or strand anything.
        task.cancel()
        await asyncio.sleep(0)
        return keepalives(sink), pending_tasks()

    lines, leftover = asyncio.run(exercise())
    assert lines, "the run must have been reporting before it was cancelled"
    assert not leftover, f"cancellation left tasks behind: {leftover!r}"


def test_a_failed_spawn_leaves_no_watchdog_behind(
    python_builder: cabc.Callable[..., SafeCmd],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that never started is never reported, nor left watching."""
    sink = io.StringIO()
    command = python_builder("-c", "pass")

    async def refuse_to_spawn(*_: object, **__: object) -> typ.NoReturn:
        """Fail the spawn the way a missing executable does."""
        await asyncio.sleep(0)
        message = "missing"
        raise FileNotFoundError(message)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse_to_spawn)

    async def exercise() -> list[asyncio.Task[object]]:
        """Run the command and survey the loop once the failure settles."""
        with pytest.raises(FileNotFoundError):
            await command.run(
                output=RunOutputOptions(idle_after=0.05),
                context=ExecutionContext(stderr_sink=sink),
            )
        return pending_tasks()

    leftover = asyncio.run(exercise())
    assert not leftover, f"the failed spawn left tasks behind: {leftover!r}"
    assert not keepalives(sink), (
        f"a spawn that never happened must not be reported: {sink.getvalue()!r}"
    )


def test_a_grandchilds_pipe_does_not_extend_the_heartbeat(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Silence reporting stops with the child, not with its stream's EOF."""
    sink = io.StringIO()
    grandchild = "import time; time.sleep(1.2)"
    source = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        "print('done')"
    )

    result = asyncio.run(
        python_builder("-c", source).run(
            output=RunOutputOptions(idle_after=0.05),
            context=ExecutionContext(stderr_sink=sink),
        ),
    )

    assert result.stdout == "done\n", f"the child must have exited: {result!r}"
    assert len(keepalives(sink)) <= 5, (
        "a grandchild holding the pipe open must not keep the run 'still "
        f"running' for sink={sink.getvalue()!r}"
    )
