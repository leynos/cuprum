"""The Windows arm of the process-group ownership contract.

``ProcessGroupPolicy.OWN_GROUP`` asks for a guarantee that POSIX process
groups provide and Windows does not. The policy is refused there rather than
emulated, so this module pins the refusal on the platform where it is the
real behaviour of the real ``os.name`` — not the patched stand-in that
``test_process_group_ownership.py`` uses to pin the same branch everywhere
else.

Windows would need a Job Object, and the reason it is not a drop-in is a race
rather than an absence: a Job Object cannot be assigned atomically with
process creation, so the assignment happens after the child exists. A child
that spawned a descendant in that window would have it outside the job, which
is exactly the containment the policy promises. Refusing is honest; accepting
the option and delivering partial containment would not be.

The refusal also has to be *early*. Both tests below check that it is raised
while the spawn arguments are being built, before any child exists, so a
caller learns that it cannot have containment from an exception rather than
from a descendant that outlived its run.

CI runs this arm on ``windows-2022`` through the typecheck-and-test matrix,
which executes the whole suite. It is not in ``EXTENSION_TEST_TARGETS``: that
list is for modules gated on the compiled extension, and this one is gated on
the platform.

Example
-------
pytest cuprum/unittests/test_process_group_ownership_windows.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from cuprum import ECHO, _subprocess_context, sh
from cuprum.sh import ExecutionContext, ProcessGroupPolicy, RunOutputOptions

_windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="asserts the refusal that OS-level process-group ownership gets",
)

pytestmark = _windows_only

# The message a caller sees has to name the policy and the platform, because
# the two things it must not be mistaken for are another option's error and a
# POSIX failure. Matching on the invariant part rather than the whole string
# leaves the wording free to change without weakening what is pinned.
_REFUSAL = r"OWN_GROUP requires POSIX process groups"


def test_the_platform_really_has_no_posix_process_groups() -> None:
    """The premise every other assertion here rests on, stated outright.

    Without this, the module would pass on any platform simply by skipping,
    and a future change that made the guard fire where process groups *do*
    exist would look the same as the guard working.
    """
    assert os.name != "posix", (
        "this module asserts the non-POSIX refusal; if os.name is 'posix' the "
        "assertions below are testing nothing"
    )


def test_owned_policy_is_refused_before_any_child_exists() -> None:
    """``OWN_GROUP`` raises while the spawn arguments are being built."""
    with pytest.raises(ValueError, match=_REFUSAL):
        _subprocess_context._ownership_spawn_kwargs(ProcessGroupPolicy.OWN_GROUP)


def test_a_run_that_asks_for_ownership_fails_during_spawn() -> None:
    """The refusal reaches a caller through the public API, not just a helper.

    The helper being guarded is not on its own the contract: what a caller
    relies on is that asking for containment it cannot have fails the run, and
    fails it while building the spawn call rather than after a child has been
    started. The command is one that would otherwise run to completion
    immediately, so a run that reached it would return instead of raising, and
    the ordering is visible in which of the two happened.
    """

    async def run_case() -> None:
        """Run one command under the owning policy, expecting the refusal."""
        with pytest.raises(ValueError, match=_REFUSAL):
            await sh.make(ECHO)("-n", "owned").run(
                output=RunOutputOptions(capture=True, echo=False),
                context=ExecutionContext(process_group=ProcessGroupPolicy.OWN_GROUP),
            )

    asyncio.run(run_case())


def test_the_inherited_default_is_accepted_on_this_platform() -> None:
    """``INHERIT`` is the default everywhere and implies no spawn flag.

    The rejection is specific to the owning policy; a guard that refused the
    default too would leave cuprum unusable on Windows while looking like a
    correctly scoped refusal.
    """
    assert (
        _subprocess_context._ownership_spawn_kwargs(
            ProcessGroupPolicy.INHERIT,
        )
        == {}
    ), "the default must stay available and inert on every platform"
