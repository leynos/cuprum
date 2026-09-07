"""Regression coverage for result-level echo fallback diagnostics (#356).

The canonical drain contract lives in ``test_stream_drain.py`` and the drain
guard itself in ``test_stream_echo_guard.py``; this module pins the additive
result surface built on top of the #350 echo guard: ``CommandResult`` and each
pipeline stage expose the handled echo-disablement records of their own
streams, in a deterministic stdout-then-stderr order, with nothing recorded
for binary passthrough, disabled echo, or a healthy sink.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import RelayFallback, TimeoutExpired
from cuprum.echo_events import EchoErrorCategory, EchoEvent, EchoStream
from cuprum.echo_observation import observe_echo
from cuprum.sh import CommandResult, ExecutionContext, RunOutputOptions
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd


_EXPECTED_STDOUT_FALLBACK = RelayFallback(
    stream=EchoStream.STDOUT,
    error_category=EchoErrorCategory.UNICODE_ENCODE,
)
_EXPECTED_STDERR_FALLBACK = RelayFallback(
    stream=EchoStream.STDERR,
    error_category=EchoErrorCategory.UNICODE_ENCODE,
)
_NON_ENCODABLE = "héllo ś"


class _Cp1252TextOnlySink:
    """Text-only sink rejecting payloads CP1252 cannot represent."""

    def __init__(self) -> None:
        """Record each attempted write payload."""
        self.attempts: list[str] = []

    def write(self, payload: str) -> int:
        """Record the write, then reject CP1252-unrepresentable text."""
        self.attempts.append(payload)
        payload.encode("cp1252")
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


class _PassthroughSink:
    """Sink that accepts every write, modelling a healthy echo target."""

    def __init__(self) -> None:
        """Collect written text for assertions."""
        self.written: list[str] = []

    def write(self, payload: str) -> int:
        """Accept the payload unchanged."""
        self.written.append(payload)
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


def _echo_context(sink: typ.IO[str]) -> ExecutionContext:
    """Build a context echoing stdout into ``sink``."""
    return ExecutionContext(stdout_sink=sink)


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


def test_single_command_result_carries_stdout_diagnostics(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A streamed run reports the stdout drain's disablement once."""
    sink = _Cp1252TextOnlySink()

    async def run_case() -> CommandResult:
        """Echo stdout into a rejecting sink while capturing."""
        with observe_echo(lambda _event: None):
            return await python_builder("-c", f"print('{_NON_ENCODABLE}')").run(
                output=RunOutputOptions(capture=True, echo=True),
                context=_echo_context(typ.cast("typ.IO[str]", sink)),
            )

    result = asyncio.run(run_case())

    assert result.stdout == f"{_NON_ENCODABLE}\n", (
        "capture must survive the echo disablement"
    )
    assert result.relay_fallbacks == (_EXPECTED_STDOUT_FALLBACK,), (
        f"exactly the stdout record is expected, got {result.relay_fallbacks!r}"
    )
    assert result.ok, "an echo failure must not change the child's exit status"


def test_stderr_echo_failure_is_recorded_against_stderr(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A stderr sink failure produces a stderr-labelled record only."""
    stderr_sink = _Cp1252TextOnlySink()

    async def run_case() -> CommandResult:
        """Echo stderr into the rejecting sink while stdout stays healthy."""
        with observe_echo(lambda _event: None):
            return await python_builder(
                "-c",
                "import sys; sys.stderr.write('wörld ś\\n'); sys.stderr.flush()",
            ).run(
                output=RunOutputOptions(capture=True, echo=True),
                context=ExecutionContext(
                    stderr_sink=typ.cast("typ.IO[str]", stderr_sink)
                ),
            )

    result = asyncio.run(run_case())

    assert result.relay_fallbacks == (_EXPECTED_STDERR_FALLBACK,), (
        f"the stderr record must name stderr, got {result.relay_fallbacks!r}"
    )
    assert result.ok


def test_run_sync_exposes_the_same_result_shape(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The synchronous API returns the same diagnostics as the async API."""
    sink = _Cp1252TextOnlySink()

    with observe_echo(lambda _event: None):
        result = python_builder("-c", f"print('{_NON_ENCODABLE}')").run_sync(
            output=RunOutputOptions(capture=True, echo=True),
            context=_echo_context(typ.cast("typ.IO[str]", sink)),
        )

    assert result.relay_fallbacks == (_EXPECTED_STDOUT_FALLBACK,)
    assert result.stdout == f"{_NON_ENCODABLE}\n"


def test_normal_and_disabled_echo_produce_empty_diagnostics(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Healthy runs — echoing or not — report no fallback records."""
    accepting = _PassthroughSink()

    async def run_case() -> tuple[tuple[RelayFallback, ...], tuple[RelayFallback, ...]]:
        """Run once with echo enabled and once with echo disabled."""
        with observe_echo(lambda _event: None):
            echoed = await python_builder("-c", "print('plain')").run(
                output=RunOutputOptions(capture=True, echo=True),
                context=_echo_context(typ.cast("typ.IO[str]", accepting)),
            )
            silent = await python_builder("-c", "print('plain')").run(
                output=RunOutputOptions(capture=True),
            )
        return echoed.relay_fallbacks, silent.relay_fallbacks

    echoed_fallbacks, silent_fallbacks = asyncio.run(run_case())

    assert echoed_fallbacks == (), (
        f"healthy echo records nothing, got {echoed_fallbacks!r}"
    )
    assert silent_fallbacks == (), "disabled echo records nothing"


def test_concurrent_commands_do_not_share_diagnostics(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Concurrent runs each report only their own fallbacks."""
    rejecting = _Cp1252TextOnlySink()
    accepting = _PassthroughSink()

    async def run_case() -> tuple[tuple[RelayFallback, ...], tuple[RelayFallback, ...]]:
        """Run one failing and one healthy echo command concurrently."""
        with observe_echo(lambda _event: None):
            failing = asyncio.create_task(
                python_builder("-c", f"print('{_NON_ENCODABLE}')").run(
                    output=RunOutputOptions(capture=True, echo=True),
                    context=_echo_context(typ.cast("typ.IO[str]", rejecting)),
                ),
            )
            healthy = asyncio.create_task(
                python_builder("-c", "print('plain')").run(
                    output=RunOutputOptions(capture=True, echo=True),
                    context=_echo_context(typ.cast("typ.IO[str]", accepting)),
                ),
            )
            failing_result, healthy_result = await asyncio.gather(failing, healthy)
        return failing_result.relay_fallbacks, healthy_result.relay_fallbacks

    failing_fallbacks, healthy_fallbacks = asyncio.run(run_case())

    assert failing_fallbacks == (_EXPECTED_STDOUT_FALLBACK,)
    assert healthy_fallbacks == (), (
        "the healthy run must not acquire the failing run's record"
    )


def test_nested_commands_keep_their_own_diagnostics(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A command nested after another does not inherit its diagnostics."""
    rejecting = _Cp1252TextOnlySink()
    accepting = _PassthroughSink()

    async def run_case() -> tuple[tuple[RelayFallback, ...], tuple[RelayFallback, ...]]:
        """Run a healthy command, then a failing one, in one context."""
        with observe_echo(lambda _event: None):
            outer = await python_builder("-c", "print('outer plain')").run(
                output=RunOutputOptions(capture=True, echo=True),
                context=_echo_context(typ.cast("typ.IO[str]", accepting)),
            )
            inner = await python_builder("-c", f"print('{_NON_ENCODABLE}')").run(
                output=RunOutputOptions(capture=True, echo=True),
                context=_echo_context(typ.cast("typ.IO[str]", rejecting)),
            )
        return outer.relay_fallbacks, inner.relay_fallbacks

    outer_fallbacks, inner_fallbacks = asyncio.run(run_case())

    assert outer_fallbacks == (), "the outer run must stay clean"
    assert inner_fallbacks == (_EXPECTED_STDOUT_FALLBACK,), (
        f"the inner run must own its record, got {inner_fallbacks!r}"
    )


def test_timeout_with_pre_expiry_disablement_observes_event(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """An echo failure observed before expiry stays on the event channel."""
    events: list[EchoEvent] = []
    sink = _Cp1252TextOnlySink()

    async def run_case() -> None:
        """Timeout a run whose child first trips the echo sink."""
        with observe_echo(events.append):
            await python_builder(
                "-c",
                f"import time; print('{_NON_ENCODABLE}', flush=True); time.sleep(5)",
            ).run(
                timeout=1.0,
                output=RunOutputOptions(capture=True, echo=True),
                context=_echo_context(typ.cast("typ.IO[str]", sink)),
            )

    with pytest.raises(TimeoutExpired):
        asyncio.run(run_case())

    assert len(events) == 1, (
        f"the pre-timeout disablement must stay observable, found {events!r}"
    )
    assert events[0].stream == EchoStream.STDOUT


def test_failing_echo_observer_does_not_change_results(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A raising observer leaves result collection and capture untouched."""
    sink = _Cp1252TextOnlySink()

    def exploding_observer(_event: EchoEvent) -> None:
        """Model a broken metrics backend."""
        msg = "observer exploded"
        raise RuntimeError(msg)

    with observe_echo(exploding_observer):
        result = python_builder("-c", f"print('{_NON_ENCODABLE}')").run_sync(
            output=RunOutputOptions(capture=True, echo=True),
            context=_echo_context(typ.cast("typ.IO[str]", sink)),
        )

    assert result.stdout == f"{_NON_ENCODABLE}\n", (
        "capture must survive a broken observer"
    )
    assert result.relay_fallbacks == (_EXPECTED_STDOUT_FALLBACK,), (
        "the diagnostic collector is independent of the observer registry"
    )


def test_no_fallback_record_for_program_without_output(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A command producing no output records nothing even when echoing."""
    accepting = _PassthroughSink()

    async def run_case() -> tuple[RelayFallback, ...]:
        """Echo silence through a healthy sink."""
        with observe_echo(lambda _event: None):
            result = await python_builder("-c", "pass").run(
                output=RunOutputOptions(capture=True, echo=True),
                context=_echo_context(typ.cast("typ.IO[str]", accepting)),
            )
        return result.relay_fallbacks

    assert asyncio.run(run_case()) == ()


def test_event_stream_identity_matches_diagnostic_stream(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The EchoEvent and the result record describe the same transition."""
    events: list[EchoEvent] = []
    sink = _Cp1252TextOnlySink()

    with observe_echo(events.append):
        result = python_builder("-c", f"print('{_NON_ENCODABLE}')").run_sync(
            output=RunOutputOptions(capture=True, echo=True),
            context=_echo_context(typ.cast("typ.IO[str]", sink)),
        )

    assert len(events) == 1, f"one event for one transition, found {events!r}"
    assert result.relay_fallbacks == (
        RelayFallback(
            stream=events[0].stream,
            error_category=events[0].error_category,
        ),
    ), "the result record and the event must agree on the transition"
