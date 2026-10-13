"""The window in which a clock runs out but the child has already gone.

The settlement outlives the child by the drain: after the exit wait returns,
the captured consumers are given an EOF grace to observe EOF, and a grandchild
holding an inherited pipe can extend that further. A deadline landing in that
window has nothing left to interrupt — the child is already gone — so recording
it would publish ``TIMEOUT`` for a run that ended on its own, and skip its
``after`` hooks while doing so.

:mod:`cuprum.unittests.test_running_command_settlement_deadlines` covers what a deadline
bounds and what its expiry carries while the child is still healthy;
:mod:`cuprum.unittests.test_running_command_settlement` covers the rest of the
settlement's obligations. Both drive the process doubles in
:mod:`cuprum.unittests._running_command_support`, and so does this module —
what is added here is the state a double has to be built into, because a child
that has genuinely ended is precisely what the rule under test is about.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum._process_handle import settlement
from cuprum.unittests._running_command_support import (
    _handle,
    _probe_of,
    _StubProcess,
    _StubRunConfig,
)

if typ.TYPE_CHECKING:
    from cuprum.sh import CommandResult

#: How long a deadline is given to expire. Long enough that a scheduler under
#: load cannot expire the wait before the case has set it running, short enough
#: that every case stays well inside the per-test timeout.
_DEADLINE = 0.2

#: How long the double's drain is held open, so a deadline can fire inside the
#: window that follows the exit wait. Comfortably longer than :data:`_DEADLINE`
#: plus the capture EOF grace the drain grants its consumers, and far shorter
#: than the per-test timeout.
_DRAIN_DELAY = 1.0

#: How long a case holds a reaper that has not published yet. Longer than
#: :data:`_DEADLINE` by enough that the clock is certain to run out while the
#: publication is outstanding, and far shorter than the guard the case waits
#: under.
_PUBLICATION_DELAY = 0.5


class _ExitedProcess(_StubProcess):
    """A double that has already exited before anything waits on it."""

    def __init__(self, *, pid: int = 4399) -> None:
        """Start ended, with the child's own status already published."""
        super().__init__(pid=pid)
        self.exit_with(0)


class _UnreapedExitedProcess(_StubProcess):
    """A double the kernel reports as exited, with nothing published yet."""

    def __init__(self, *, pid: int = 4398) -> None:
        """Start ended on the kernel's view alone, and stay unpublished."""
        super().__init__(pid=pid)
        # ``exit_with`` is deliberately not called: it publishes the code,
        # and withholding that is the whole point. The double has no real
        # PID, so the kernel question the production probe would ask of a
        # real child is one only the double can answer — hence
        # ``kernel_exit_code`` and the patched probe in the case below.
        self.kernel_exit_code: int | None = 0
        self.exited.set()
        self.collection_started = asyncio.Event()

    async def wait(self) -> int:
        """Reap the double, publishing only once the case lets it.

        The reaper is what turns the kernel's answer into the published
        code, and here it is held open so the two disagree across the
        deadline's evaluation rather than only within it. Nothing publishes
        the code before this returns, so a branch reading ``returncode``
        finds ``None`` for a child the probe can already see has ended.

        The publication waits on a clock rather than on the loop's
        scheduling, because it does not matter how many times the deadline
        branch is entered: what matters is that the attribute is still
        unset every time it is read. Announcing the entry first is what
        stops the case from passing on a reaper that never ran at all.

        Returns
        -------
        int
            The code the case staged for this double, which is what a real
            reaper would have collected from the kernel.
        """
        await self.exited.wait()
        self.collection_started.set()
        await asyncio.sleep(_PUBLICATION_DELAY)
        self.returncode = self.kernel_exit_code
        return self.returncode if self.returncode is not None else -1


def test_a_deadline_after_the_child_exited_is_not_recorded() -> None:
    """A clock that fires once the child is gone reports the child's own exit.

    The double exits before its ``wait()`` returns, which is that window: the
    exit has happened and the settlement is still running. Both halves of the
    rule are asserted here, because they fail together: the caller reads the
    child's status rather than an expiry, and the hook the run registered — the
    one a deadline branch does not run — was run.
    """
    hook_calls: list[int] = []

    async def run_case() -> CommandResult:
        """Wait with a deadline that fires while the exit is being drained."""
        process = _ExitedProcess()
        handle = _handle(
            process,
            _StubRunConfig(
                after_hooks=(lambda _cmd, result: hook_calls.append(result.exit_code),),
                # The settlement's drain stays open past the deadline, which is
                # the window the rule under test is about.
                drain_delay=_DRAIN_DELAY,
            ),
        )
        return await handle.wait(timeout=_DEADLINE)

    result = asyncio.run(run_case())

    assert result.exit_code == 0, (
        "a child that exited on its own must report its own status, got "
        f"{result.exit_code}"
    )
    assert hook_calls == [0], (
        "the run ended on its own, so its after hook must have run with the "
        f"child's own outcome; got calls {hook_calls!r}, which is empty when "
        "the deadline was wrongly recorded and took the timeout branch"
    )


def test_a_deadline_reads_the_kernel_not_the_published_code() -> None:
    """A child the kernel reports as exited is seen as exited before its reap.

    The case above covers a double whose ``wait()`` publishes the exit on its
    way back, so ``returncode`` is already set when the deadline is asked. On
    the ``wait4`` backend the real reaper publishes from an executor thread,
    which leaves a window a double of that shape cannot express: the child has
    genuinely finished, its status is genuinely a zombie's, and ``returncode``
    is still ``None`` because no worker has run the collection yet.

    A deadline landing in that window must still read the child as gone. The
    status is observable without collecting it — that is what the non-reaping
    probe is for — and reading the attribute instead records a timeout, signals
    a child that is already dead, and raises an expiry for a command that met
    its deadline. The double therefore holds the publication open for longer
    than the deadline: the case is about the reading the branch takes while the
    two disagree, and a publication that landed first would answer it for both.
    """
    hook_calls: list[int] = []

    async def run_case() -> CommandResult:
        """Wait with a deadline that fires while the exit is uncollected."""
        process = _UnreapedExitedProcess()
        handle = _handle(
            process,
            _StubRunConfig(
                after_hooks=(lambda _cmd, result: hook_calls.append(result.exit_code),),
            ),
        )
        # The probe is the seam the double stands in for: a real child has a
        # PID the kernel can be asked about, and this one does not, so the
        # double supplies that answer rather than inventing a PID. A double
        # that answers ``None`` would make the case vacuous, because a branch
        # falling back to the attribute would agree with it.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(settlement, "_observed_exit_code", _probe_of(process))
            result = await handle.wait(timeout=_DEADLINE)
        assert process.collection_started.is_set(), (
            "the double never collected the child, so no branch was ever asked "
            "what the unpublished attribute said"
        )
        assert process.returncode == 0, (
            "the double never published the exit, so the case did not exercise "
            "a child that ended while its code was still uncollected"
        )
        return result

    result = asyncio.run(run_case())

    assert result.exit_code == 0, (
        "a child the kernel reports as exited must report its own status, got "
        f"{result.exit_code}; a deadline recorded instead means the child was "
        "read through ``returncode`` rather than through the kernel"
    )
    assert hook_calls == [0], (
        "the run ended on its own, so its after hook must have run with the "
        f"child's own outcome; got calls {hook_calls!r}, which is empty when "
        "the deadline was wrongly recorded and took the timeout branch"
    )
