"""Integration coverage for pipeline relay-fallback ownership (#356)."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum.echo_events import EchoErrorCategory, EchoEvent, EchoStream, RelayFallback
from cuprum.echo_observation import observe_echo
from cuprum.sh import ExecutionContext, RunOutputOptions
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import PipelineResult, SafeCmd


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


_EXPECTED_STDERR_FALLBACK = RelayFallback(
    stream=EchoStream.STDERR,
    error_category=EchoErrorCategory.UNICODE_ENCODE,
)
_EXPECTED_STDOUT_FALLBACK = RelayFallback(
    stream=EchoStream.STDOUT,
    error_category=EchoErrorCategory.UNICODE_ENCODE,
)
_NON_ENCODABLE = "héllo ś"


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


def test_pipeline_final_stage_owns_its_stdout_diagnostics(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The final stage reports its stdout disablement; earlier stages none."""
    rejecting = _Cp1252TextOnlySink()
    accepting = _PassthroughSink()

    async def run_case() -> tuple[tuple[RelayFallback, ...], tuple[RelayFallback, ...]]:
        """Pipe two stages; the final stage's stdout echoes to the bad sink."""
        with observe_echo(lambda _event: None):
            pipeline = python_builder("-c", "print('stage one')") | python_builder(
                "-c",
                f"import sys; print(sys.stdin.read().strip() + ' {_NON_ENCODABLE}')",
            )
            result = await pipeline.run(
                output=RunOutputOptions(capture=True, echo=True),
                context=ExecutionContext(
                    stdout_sink=typ.cast("typ.IO[str]", rejecting),
                    stderr_sink=typ.cast("typ.IO[str]", accepting),
                ),
            )
        return result.stages[0].relay_fallbacks, result.stages[1].relay_fallbacks

    first_fallbacks, final_fallbacks = asyncio.run(run_case())

    assert first_fallbacks == (), (
        f"the first stage has no echo of its own stdout, got {first_fallbacks!r}"
    )
    assert final_fallbacks == (_EXPECTED_STDOUT_FALLBACK,), (
        f"the final stage must own its stdout fallback, got {final_fallbacks!r}"
    )


def test_pipeline_stage_results_keep_stage_order(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Stage order is preserved while diagnostics stay per stage."""
    rejecting = _Cp1252TextOnlySink()
    first_stderr = "wörld ś\n"
    second_stderr = "zażółć\n"

    async def run_case() -> tuple[PipelineResult, list[EchoEvent]]:
        """Run a two-stage pipeline echoing every stderr to one sink."""
        events: list[EchoEvent] = []
        first_source = (
            f"import sys; sys.stderr.write({first_stderr!r}); "
            "sys.stderr.flush(); print('mid', flush=True)"
        )
        second_source = (
            f"import sys; sys.stdin.read(); sys.stderr.write({second_stderr!r}); "
            'sys.stderr.flush(); print("done", flush=True)'
        )
        with observe_echo(events.append):
            pipeline = python_builder("-c", first_source) | python_builder(
                "-c", second_source
            )
            result = await pipeline.run(
                output=RunOutputOptions(capture=True, echo=True),
                context=ExecutionContext(
                    stderr_sink=typ.cast("typ.IO[str]", rejecting),
                ),
            )
        return result, events

    result, events = asyncio.run(run_case())

    assert len(result.stages) == 2, "stage order and count must be preserved"
    assert result.stages[0].exit_code == 0, "the first stage must succeed"
    assert result.stages[1].exit_code == 0, "the second stage must succeed"
    assert result.stages[0].stderr == first_stderr, (
        f"the first stage must retain its stderr, got {result.stages[0].stderr!r}"
    )
    assert result.stages[1].stderr == second_stderr, (
        f"the second stage must retain its stderr, got {result.stages[1].stderr!r}"
    )
    assert len(events) == 2, f"each stderr drain must emit once, got {events!r}"
    assert all(event.stream is EchoStream.STDERR for event in events), (
        f"only stderr drains must emit echo events, got {events!r}"
    )
    assert result.stages[0].relay_fallbacks == (_EXPECTED_STDERR_FALLBACK,), (
        "the first stage's stderr failure must be recorded, "
        f"got {result.stages[0].relay_fallbacks!r}"
    )
    assert result.stages[1].relay_fallbacks == (_EXPECTED_STDERR_FALLBACK,), (
        "the second stage's stderr failure must be recorded, "
        f"got {result.stages[1].relay_fallbacks!r}"
    )
    assert len(rejecting.attempts) == 2, (
        f"each stage must make one independent echo attempt, got {rejecting.attempts!r}"
    )
