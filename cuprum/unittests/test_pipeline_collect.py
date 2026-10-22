"""Unit tests for the pipeline collection invariant guards.

These cover the impossible-state guards in ``cuprum._pipeline_collect``: the
lazy ``cuprum.sh`` import shim and the timeout branch that can only be reached
when a ``TimeoutError`` arrives without a configured timeout. Both raise
``_PipelineInvariantError`` so callers can tell them apart from unrelated
runtime failures.
"""

from __future__ import annotations

import asyncio
import io
import sys
import types
import typing as typ

import pytest

from cuprum._pipeline_collect import (
    _collect_pipeline_inputs,
    _PipelineInvariantError,
    _sh_module,
)
from cuprum._pipeline_config import _PipelineRunConfig
from cuprum._pipeline_types import _ExecutionInvariantError
from cuprum._sink_lifecycle import _SinkBracket
from cuprum._subprocess_timeout import _SubprocessInvariantError
from cuprum.capture_policy import FullCapture
from cuprum.sh import ExecutionContext

if typ.TYPE_CHECKING:
    from cuprum._pipeline_records import _PipelineSpawnResult
    from cuprum.sh import SafeCmd


def test_sh_module_requires_cuprum_sh_to_be_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lazy shim fails loudly when ``cuprum.sh`` is not yet imported."""
    monkeypatch.delitem(sys.modules, "cuprum.sh", raising=False)

    with pytest.raises(_PipelineInvariantError) as exc_info:
        _sh_module()

    assert (
        str(exc_info.value) == "cuprum.sh must be imported before running pipelines"
    ), "the missing-import invariant must retain its diagnostic"


def test_sh_module_error_is_a_runtime_error() -> None:
    """The guard stays catchable by broad runtime handlers."""
    assert issubclass(_PipelineInvariantError, RuntimeError), (
        "pipeline invariant errors must remain catchable as runtime errors"
    )
    assert issubclass(_PipelineInvariantError, _ExecutionInvariantError), (
        "pipeline invariant errors must share the package invariant base"
    )
    assert issubclass(_SubprocessInvariantError, _ExecutionInvariantError), (
        "subprocess invariant errors must share the package invariant base"
    )


def _timeout_free_config() -> _PipelineRunConfig:
    """Build a run configuration with no timeout configured."""
    return _PipelineRunConfig(
        ctx=ExecutionContext(),
        capture_stdout=FullCapture(),
        capture_stderr=FullCapture(),
        echo_stdout=False,
        echo_stderr=False,
        max_echo_line_bytes=None,
        timeout=None,
        stdout_sink=io.StringIO(),
        stderr_sink=io.StringIO(),
        sink_bracket=_SinkBracket(None),
    )


def test_timeout_without_configured_timeout_raises_invariant_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout with no configured timeout is reported as a broken invariant."""
    originating_error = TimeoutError("stage timed out")

    async def fake_await_pipeline_wait_result(
        *_args: object,
        **_kwargs: object,
    ) -> object:
        """Model a wait that times out despite no timeout being configured."""
        await asyncio.sleep(0)
        raise originating_error

    monkeypatch.setattr(
        "cuprum._pipeline_collect._await_pipeline_wait_result",
        fake_await_pipeline_wait_result,
    )

    # The guarded branch needs only the pipe-task inputs and timed-out stage
    # termination target. Empty processes, observations and readers keep every
    # teardown step a no-op, so the run reaches the invariant without a real
    # spawn. Each attribute read on the way is part of what the stand-in has to
    # provide; the ``cast`` that presents this as a spawn result would otherwise
    # hide its absence from the type checker and leave the branch to fail with
    # an ``AttributeError`` at runtime.
    spawn = typ.cast(
        "_PipelineSpawnResult",
        types.SimpleNamespace(
            processes=[],
            stderr_tasks=[],
            stdout_task=None,
            relay_diagnostics_by_stage=(),
            owns_group=(),
            stages=types.SimpleNamespace(observations=()),
        ),
    )
    parts = typ.cast("tuple[SafeCmd, ...]", ())

    with pytest.raises(_PipelineInvariantError) as exc_info:
        asyncio.run(_collect_pipeline_inputs(parts, spawn, _timeout_free_config()))

    assert str(exc_info.value) == "TimeoutError without a configured timeout", (
        "the missing-timeout invariant must retain its diagnostic"
    )
    # The originating TimeoutError is chained so the impossible state stays
    # diagnosable from the traceback.
    assert exc_info.value.__cause__ is originating_error, (
        "the pipeline invariant must preserve the originating timeout as its cause"
    )
