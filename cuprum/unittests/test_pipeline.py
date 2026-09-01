"""Unit tests for Pipeline composition and execution."""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum import (
    ECHO,
    LS,
    ForbiddenProgramError,
    ScopeConfig,
    TimeoutExpired,
    scoped,
    sh,
)
from cuprum.sh import Pipeline, PipelineResult, RunOutputOptions, SafeCmd
from tests.helpers.catalogue import python_catalogue

# A nested pair describes how the leaves are bracketed; a leaf is its index.
type _AssociationTree = int | tuple[_AssociationTree, _AssociationTree]


@st.composite
def _association_trees(draw: st.DrawFn) -> tuple[int, _AssociationTree]:
    """Generate a stage count and a random bracketing of that many stages.

    Returns
    -------
    tuple[int, _AssociationTree]
        The number of leaves and a binary tree over leaf indices describing
        the order in which ``|`` is applied.
    """
    count = draw(st.integers(min_value=2, max_value=6))

    def build(low: int, high: int) -> _AssociationTree:
        """Bracket the half-open leaf range ``[low, high)``."""
        if high - low == 1:
            return low
        split = draw(st.integers(min_value=low + 1, max_value=high - 1))
        return (build(low, split), build(split, high))

    return count, build(0, count)


def _compose(tree: _AssociationTree, stages: list[SafeCmd]) -> SafeCmd | Pipeline:
    """Fold ``stages`` with ``|`` following the bracketing in ``tree``."""
    match tree:
        case int() as index:
            return stages[index]
        case (left, right):
            return _compose(left, stages) | _compose(right, stages)


def test_or_operator_composes_pipeline() -> None:
    """The | operator composes SafeCmd stages into a Pipeline."""
    echo = sh.make(ECHO)
    first = echo("-n", "hello")
    second = echo("-n", "world")

    pipeline = first | second

    assert isinstance(pipeline, Pipeline)
    assert pipeline.parts == (first, second)


@settings(max_examples=100, deadline=None)
@given(shape=_association_trees())
def test_or_operator_flattens_any_association(
    shape: tuple[int, _AssociationTree],
) -> None:
    """``|`` flattens to source-order stages however the operands are bracketed.

    Each stage carries a distinct argument, so the assertion pins the order as
    well as the membership of ``parts``.
    """
    count, tree = shape
    echo = sh.make(ECHO)
    stages = [echo("-n", f"stage-{index}") for index in range(count)]

    pipeline = _compose(tree, stages)

    assert isinstance(pipeline, Pipeline), (
        "composing two or more stages must yield a Pipeline"
    )
    assert pipeline.parts == tuple(stages), (
        "Pipeline stages must appear in source order regardless of bracketing"
    )


def test_pipeline_run_sync_enforces_scoped_allowlist() -> None:
    """Pipeline execution rejects stages outside the active allowlist."""
    echo = sh.make(ECHO)
    ls = sh.make(LS)
    pipeline = echo("hello") | ls()

    with (
        scoped(ScopeConfig(allowlist=frozenset([ECHO]))),
        pytest.raises(ForbiddenProgramError, match="ls"),
    ):
        pipeline.run_sync()


def _run_test_pipeline(
    stages_exit_codes: list[int],
) -> PipelineResult:
    """Execute a test pipeline with specified per-stage exit codes.

    Parameters
    ----------
    stages_exit_codes:
        Exit code for each pipeline stage.

    Returns
    -------
    PipelineResult
        PipelineResult from synchronous execution.

    Raises
    ------
    ValueError
        If fewer than two stages are supplied.

    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)

    stages = [
        python("-c", f"import sys; sys.exit({code})") for code in stages_exit_codes
    ]

    if len(stages) < 2:
        msg = "test pipeline helper requires at least two stages"
        raise ValueError(msg)

    pipeline = stages[0] | stages[1]
    for stage in stages[2:]:
        pipeline |= stage

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        return pipeline.run_sync()


def test_pipeline_run_streams_stdout_between_stages(stream_backend: str) -> None:
    """Pipeline.run_sync streams stdout into the next stage stdin."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    echo = sh.make(ECHO)

    pipeline = echo("-n", "hello") | python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read().upper())",
    )

    with scoped(ScopeConfig(allowlist=frozenset([ECHO, python_program]))):
        result = pipeline.run_sync()

    assert isinstance(result, PipelineResult)
    assert result.stdout == "HELLO"
    assert len(result.stages) == 2
    assert result.stages[0].stdout is None
    assert result.stages[0].exit_code == 0
    assert result.stages[1].exit_code == 0
    assert result.stages[0].pid > 0
    assert result.stages[1].pid > 0


def test_pipeline_propagates_cancelled_pipe_task(
    stream_backend: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled inter-stage pump must not let successful stages hide cancellation."""

    async def cancelled_pipe_task() -> None:
        """Model a pump cancelled independently of the process stages."""
        await asyncio.sleep(0)
        raise asyncio.CancelledError

    def create_cancelled_pipe_task(
        _processes: object,
        *,
        observations: object = (),
    ) -> list[asyncio.Task[None]]:
        """Inject the cancelled pipe task at the pipeline creation boundary."""
        del observations
        return [asyncio.create_task(cancelled_pipe_task())]

    monkeypatch.setattr(
        "cuprum._pipeline_collect._create_pipe_tasks",
        create_cancelled_pipe_task,
    )

    with pytest.raises(asyncio.CancelledError):
        _run_test_pipeline([0, 0])


def test_pipeline_ignores_broken_pipe_task(
    stream_backend: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken inter-stage pump is expected when otherwise-successful stages exit."""

    async def broken_pipe_task() -> None:
        """Model a downstream stage closing its input early."""
        await asyncio.sleep(0)
        raise BrokenPipeError

    def create_broken_pipe_task(
        _processes: object,
        *,
        observations: object = (),
    ) -> list[asyncio.Task[None]]:
        """Inject the expected broken-pipe task at the creation boundary."""
        del observations
        return [asyncio.create_task(broken_pipe_task())]

    monkeypatch.setattr(
        "cuprum._pipeline_collect._create_pipe_tasks",
        create_broken_pipe_task,
    )

    assert _run_test_pipeline([0, 0]).ok, (
        "an expected broken pipe must not fail otherwise-successful stages"
    )


def test_pipeline_timeout_raises_timeout_expired(stream_backend: str) -> None:
    """Pipeline timeouts raise TimeoutExpired and respect capture flags."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)

    pipeline = python("-c", "import time; time.sleep(5)") | python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read())",
    )

    with (
        scoped(ScopeConfig(allowlist=frozenset([python_program]))),
        pytest.raises(TimeoutExpired, match=r"timed out") as exc_info,
    ):
        pipeline.run_sync(timeout=0.2, output=RunOutputOptions(capture=False))

    assert exc_info.value.timeout == pytest.approx(0.2)
    assert exc_info.value.output is None
    assert exc_info.value.stderr is None


@pytest.mark.parametrize(
    ("stage_codes", "expect_ok", "expect_failure_index"),
    [
        pytest.param(
            [0, 1],
            False,
            1,
            id="failure-sets-ok-false-and-exposes-failed-stage",
        ),
        pytest.param(
            [0, 0],
            True,
            None,
            id="success-has-no-failure",
        ),
    ],
)
def test_pipeline_run_sync_failure_semantics(
    stream_backend: str,
    stage_codes: list[int],
    *,
    expect_ok: bool,
    expect_failure_index: int | None,
) -> None:
    """Validate PipelineResult failure semantics for success and failure cases.

    Tests that:
    - Failed pipelines set ok=False and expose failure/failure_index
    - Successful pipelines set ok=True with failure=None and failure_index=None
    """
    result = _run_test_pipeline(stage_codes)

    assert isinstance(result, PipelineResult)
    assert result.ok is expect_ok
    assert result.failure_index == expect_failure_index
    assert result.final is result.stages[-1]
    assert len(result.stages) == len(stage_codes)

    for idx, expected_code in enumerate(stage_codes):
        exit_code = result.stages[idx].exit_code
        if expect_failure_index is not None and idx < expect_failure_index:
            assert exit_code in {expected_code, -15}, (
                "an upstream stage must either complete before fail-fast or be "
                "terminated by it"
            )
            continue
        assert exit_code == expected_code, (
            f"stage {idx} must retain its expected exit code after fail-fast"
        )

    if expect_failure_index is not None:
        assert result.failure is result.stages[expect_failure_index]
        assert result.final.exit_code != 0
    else:
        assert result.failure is None
        assert result.final.exit_code == 0


def test_pipeline_requires_at_least_two_stages() -> None:
    """Pipelines reject construction with fewer than two parts."""
    echo = sh.make(ECHO)
    only = echo("-n", "one")

    with pytest.raises(ValueError, match="at least two stages"):
        Pipeline((only,))
