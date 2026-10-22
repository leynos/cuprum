"""End-to-end stdio and line-observation contracts for pipelines."""

from __future__ import annotations

import io
import time
import types
import typing as typ
from collections import Counter

import pytest

from cuprum import Program, RunOutputOptions, ScopeConfig, scoped, sh
from cuprum.sh import ExecutionContext, Pipeline
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.lines import LineEvent


# The producer names its streams so the final stage's stdout is distinguishable
# from the intermediate stage's; the consumer relays stdin and adds its own
# stderr line, so every stage contributes an observable stderr event.
_LINE_OBSERVATION_PRODUCER = (
    "import sys; print('p-out'); print('p-out'); print('p-err', file=sys.stderr); "
    "print('p-err', file=sys.stderr)"
)
_LINE_OBSERVATION_CONSUMER = (
    "import sys; sys.stdout.write(sys.stdin.read()); print('c-err', file=sys.stderr); "
    "print('c-err', file=sys.stderr)"
)

_EXPECTED_OBSERVED_LINES = Counter({
    ("stdout", "p-out"): 2,
    ("stderr", "p-err"): 2,
    ("stderr", "c-err"): 2,
})


@pytest.mark.parametrize(
    ("capture", "expected_stdout", "expected_stderr"),
    [
        pytest.param(True, "INTERMEDIATE", "", id="capture-final-stdio"),
        pytest.param(False, None, None, id="discard-final-stdio"),
    ],
)
def test_pipeline_stdio_policy_streams_intermediate_stdout_end_to_end(
    *,
    capture: bool,
    expected_stdout: str | None,
    expected_stderr: str | None,
) -> None:
    """Pipeline execution streams intermediate stdout and applies final capture.

    Parameters
    ----------
    capture : bool
        Whether the final stage's stdio should be captured.
    expected_stdout : str | None
        The expected final stdout, or ``None`` when ``capture`` is ``False``
        and stdout is not captured.
    expected_stderr : str | None
        The expected final stderr, or ``None`` when ``capture`` is ``False``
        and stderr is not captured.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)

    producer = python("-c", "import sys; sys.stdout.write('intermediate')")
    transformer = python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read().upper())",
    )

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = (producer | transformer).run_sync(
            output=RunOutputOptions(capture=capture),
        )

    assert result.stdout == expected_stdout, (
        f"capture={capture}: result.stdout mismatch"
    )
    assert len(result.stages) == 2, f"capture={capture}: result.stages length mismatch"
    assert result.stages[0].stdout is None, (
        f"capture={capture}: stage 0 stdout mismatch"
    )
    assert result.stages[0].stderr == expected_stderr, (
        f"capture={capture}: stage 0 stderr mismatch"
    )
    assert result.stages[0].exit_code == 0, (
        f"capture={capture}: stage 0 exit_code mismatch"
    )
    assert result.stages[1].stdout == expected_stdout, (
        f"capture={capture}: stage 1 stdout mismatch"
    )
    assert result.stages[1].stderr == expected_stderr, (
        f"capture={capture}: stage 1 stderr mismatch"
    )
    assert result.stages[1].exit_code == 0, (
        f"capture={capture}: stage 1 exit_code mismatch"
    )


def _line_observation_pipeline() -> tuple[Pipeline, frozenset[Program]]:
    """Build a two-stage pipeline where each stage writes to both streams."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    producer = python("-c", _LINE_OBSERVATION_PRODUCER)
    consumer = python("-c", _LINE_OBSERVATION_CONSUMER)
    return producer | consumer, frozenset([python_program])


@pytest.mark.usefixtures("stream_backend")
def test_pipeline_line_stamps_read_the_stage_observation_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stage's stamps and its start reference come from one clock.

    ``_spawn_pipeline_stages`` samples each stage's start reference and the
    per-line callbacks subtract it when stamping. Both must read the stage
    observation's ``monotonic_clock``: a reference taken from a module-level
    ``perf_counter`` turns each stamp into a difference between two clocks
    under injection, and a stamp read from the real clock floors to ``0.0``
    when the injected reference sits above it.

    One driven clock answers both readings, advancing by ``step`` on every
    call from one origin far above any real reading. Any two readings are
    therefore a whole number of steps apart, and no stamp can exceed the
    widest gap between readings actually taken — an exact bound requiring no
    tolerance, so a stamp that came from a clock reading in the millions
    instead of the trillions cannot slip under it.
    """
    from cuprum import _pipeline_observation

    clock_origin = 1e12
    step = 0.25
    readings: list[float] = []

    def driven_clock() -> float:
        """Answer the next reading of the injected clock."""
        reading = clock_origin + step * len(readings)
        readings.append(reading)
        return reading

    # The observation's clocks are resolved *by name* from its module, so the
    # whole ``time`` binding is replaced rather than one attribute on the
    # shared ``time`` module. Patching ``time.perf_counter`` itself would also
    # reach any module-level ``import time`` in the spawn path and make this
    # test vacuous.
    monkeypatch.setattr(
        _pipeline_observation,
        "time",
        types.SimpleNamespace(perf_counter=driven_clock, time=time.time),
    )
    events: list[LineEvent] = []
    pipeline, allowlist = _line_observation_pipeline()

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = pipeline.run_sync(
            output=RunOutputOptions(capture=False, echo=False, on_line=events.append),
        )

    assert result.ok is True, "the pipeline should succeed"
    assert events, "the pipeline's observed streams must reach the line hook"
    stamps = [event.at for event in events]
    widest_gap = len(readings) - 1
    step_counts = [stamp / step for stamp in stamps]
    assert all(
        0 < count <= widest_gap and count.is_integer() for count in step_counts
    ), (
        "both readings come from the driven clock, which starts far above any "
        "real reading and advances in whole steps, so every stamp must be a "
        f"positive whole number of steps no wider than the {widest_gap} gaps "
        f"actually read; got {stamps!r}"
    )


@pytest.mark.usefixtures("stream_backend")
def test_pipeline_on_line_observes_without_capture_or_echo() -> None:
    """A registered ``on_line`` keeps the pipes open on its own.

    Every stage's stderr and the final stage's stdout are consumed for line
    observation even though capture and echo are both off, so the callback —
    not DEVNULL — receives them.
    """
    events: list[LineEvent] = []
    pipeline, allowlist = _line_observation_pipeline()

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = pipeline.run_sync(
            output=RunOutputOptions(
                capture=False,
                echo=False,
                on_line=events.append,
            ),
        )

    observed = Counter((event.stream, event.text) for event in events)
    assert observed == _EXPECTED_OBSERVED_LINES, (
        f"on_line must observe the final stdout and every stderr, got {observed!r}"
    )
    assert result.ok is True, "the pipeline should succeed"
    assert result.final.stdout is None, (
        "capture=False must leave stdout unset even while lines are observed"
    )
    assert result.final.stderr is None, (
        "capture=False must leave stderr unset even while lines are observed"
    )
    assert all(event.at >= 0.0 for event in events), (
        "line stamps must be monotonic seconds since the stage started"
    )


@pytest.mark.usefixtures("stream_backend")
def test_pipeline_on_line_composes_with_capture_and_echo() -> None:
    """Line observation is additive: capture and echo still happen."""
    events: list[LineEvent] = []
    stdout_sink = io.StringIO()
    pipeline, allowlist = _line_observation_pipeline()

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = pipeline.run_sync(
            output=RunOutputOptions(
                capture=True,
                echo=True,
                on_line=events.append,
            ),
            context=ExecutionContext(
                stdout_sink=typ.cast("typ.IO[str]", stdout_sink),
                stderr_sink=typ.cast("typ.IO[str]", io.StringIO()),
            ),
        )

    observed = Counter((event.stream, event.text) for event in events)
    assert observed == _EXPECTED_OBSERVED_LINES, (
        f"capture and echo must not suppress line observation, got {observed!r}"
    )
    assert result.stdout == "p-out\np-out\n", (
        f"capture must still return the final stage stdout, got {result.stdout!r}"
    )
    assert stdout_sink.getvalue() == "p-out\np-out\n", (
        f"echo must still reach the configured sink, got {stdout_sink.getvalue()!r}"
    )
