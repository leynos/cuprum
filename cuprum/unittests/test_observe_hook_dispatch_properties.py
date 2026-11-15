"""Property tests for observe-hook dispatch.

The dispatcher must schedule exactly the results that
:func:`inspect.isawaitable` accepts, in order, up to the first failing hook, and
a settled dispatch must call every hook before surfacing the first failure. The
oracle in ``_observe_hook_dispatch_oracle`` states that specification
independently of the dispatcher, so these tests pass both before and after the
result-guarded fast path lands: they are a preservation property, not a red
test. The seeded-fault tests in ``test_observe_hook_dispatch_seeded_faults``
prove the property can reject a wrong dispatcher.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

from hypothesis import HealthCheck, example, given, settings
from hypothesis import event as hypothesis_event
from hypothesis import strategies as st

from cuprum._observability import _emit_exec_event
from cuprum.unittests._observe_hook_dispatch_compare import make_event, mismatch
from cuprum.unittests._observe_hook_dispatch_support import HOOK_KINDS

if typ.TYPE_CHECKING:
    import pytest

_PHASES = ("plan", "start", "stdout", "exit")

# ``check_raises`` escapes the dispatcher unwrapped, so the tasks scheduled
# before it never reach the caller. The comparison records them as they are
# created and settles them, so a mixed sequence is exercised rather than
# leaking a pending task.
_KINDS = st.lists(st.sampled_from(HOOK_KINDS), min_size=0, max_size=8)


# ``caplog`` is function-scoped, so Hypothesis cannot reset it between
# generated inputs. The test clears it at the start of every example, which is
# the reset it needs; the health check would otherwise reject the fixture.
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@example(kinds=["sync_none"], phase="plan")
@example(kinds=["sync_value"], phase="plan")
@example(kinds=["plain_coroutine"], phase="plan")
@example(kinds=["lambda_coroutine"], phase="plan")
@example(kinds=["wraps_coroutine"], phase="plan")
@example(kinds=["async_def"], phase="plan")
@example(kinds=["partial_async"], phase="plan")
@example(kinds=["bound_async_method"], phase="plan")
@example(kinds=["async_call_instance"], phase="plan")
@example(kinds=["sync_call_instance"], phase="plan")
@example(kinds=["async_mock"], phase="plan")
@example(kinds=["autospec_async"], phase="plan")
@example(kinds=["marked_coroutine_function"], phase="plan")
@example(kinds=["future"], phase="plan")
@example(kinds=["generator_coroutine"], phase="plan")
@example(kinds=["awaitable_object"], phase="plan")
@example(kinds=["failing_awaitable"], phase="stdout")
@example(kinds=["mock_result"], phase="plan")
@example(kinds=["magic_mock_result"], phase="plan")
@example(kinds=["raising"], phase="plan")
@example(kinds=["raising_fatal"], phase="plan")
@example(kinds=["raising_cancelled"], phase="plan")
@example(kinds=["check_raises"], phase="exit")
@example(kinds=["async_def", "check_raises", "async_def"], phase="plan")
@example(kinds=["future", "check_raises"], phase="stdout")
@example(kinds=["failing_awaitable", "check_raises", "sync_none"], phase="exit")
@example(kinds=["async_def", "async_def", "raising"], phase="stdout")
@given(kinds=_KINDS, phase=st.sampled_from(_PHASES))
def test_dispatch_matches_specification(
    kinds: list[str],
    phase: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dispatch schedules exactly the awaitable results, in order."""
    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="cuprum._observability")
    # Tag every generated sequence with the hook classes it exercised, so a
    # ``--hypothesis-show-statistics`` run proves the domain is not vacuous.
    for kind in kinds:
        hypothesis_event(kind)
    event = make_event(phase)

    async def run() -> None:
        """Compare the dispatcher with the specification inside a loop."""
        found = await mismatch(_emit_exec_event, kinds, event, caplog.records)
        assert found is None, f"{kinds} at {phase}: {found}"

    asyncio.run(run())


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@example(kinds=[])
@example(kinds=["raising"])
@example(kinds=["raising", "async_def"])
@example(kinds=["async_def", "raising", "future"])
@example(kinds=["raising_cancelled", "async_def"])
@example(kinds=["failing_awaitable", "async_def"])
@example(kinds=["check_raises"])
@example(kinds=["async_def", "check_raises", "async_def"])
@example(kinds=["raising", "check_raises", "future"])
@example(kinds=["raising", "async_def", "raising_fatal", "future"])
@given(kinds=_KINDS)
def test_settled_dispatch_matches_specification(
    kinds: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A settled dispatch calls every hook, then surfaces the first failure."""
    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="cuprum._observability")
    for kind in kinds:
        hypothesis_event(kind)
    event = make_event("settled")

    async def run() -> None:
        """Compare the settled dispatcher with the specification."""
        found = await mismatch(_emit_exec_event, kinds, event, caplog.records)
        assert found is None, f"{kinds} settled: {found}"

    asyncio.run(run())
