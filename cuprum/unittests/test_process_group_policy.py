"""The opt-in process-group ownership policy, and the spawn it implies.

The policy is two decisions that have to agree: what
:class:`~cuprum.sh.ProcessGroupPolicy` a run is given, and what spawn that
policy asks for. A policy that reached the context but never the spawn would
be a containment claim with nothing behind it, so these tests assert on the
spawn arguments themselves rather than inferring them from teardown behaviour.

The context field is a ``StrEnum``, so both spellings a caller may hold are
covered: the member and the string. Every consumer compares the policy with
``is``, which is why a string that never became a member is a defect rather
than a spelling preference.

The teardown behaviour ownership buys lives in
``test_process_group_ownership.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import os
import typing as typ

import pytest

from cuprum import ECHO, _subprocess_context, _wait4_process, sh
from cuprum.sh import ExecutionContext, ProcessGroupPolicy, RunOutputOptions
from cuprum.unittests._process_group_ownership_support import _posix_only

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

pytestmark = _posix_only


def test_owned_policy_is_rejected_where_posix_groups_do_not_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OWN_GROUP`` refuses rather than silently containing nothing.

    The refusal is raised while building the spawn arguments, before any child
    exists, so a caller that asked for containment it cannot have learns that
    from the exception instead of from a descendant that outlived its run.

    ``os.name`` is patched rather than the test being Windows-only: the guard
    is the thing under test, and it must hold regardless of which platform
    happens to be running the suite. The Windows job exercises the same helper
    through its real ``os.name``; this pins the branch deterministically
    everywhere. ``monkeypatch`` is what does the patching so the restore is
    guaranteed rather than left to a ``finally``.
    """
    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(ValueError, match="POSIX process groups"):
        _subprocess_context._ownership_spawn_kwargs(
            ProcessGroupPolicy.OWN_GROUP,
        )


def test_inherited_policy_spawns_no_new_session() -> None:
    """``INHERIT`` implies no spawn keyword, whatever the platform.

    The default has to stay inert: the policy is opt-in, and a spawn flag that
    slipped into the default path would change process topology for every
    existing caller.
    """
    assert not _subprocess_context._ownership_spawn_kwargs(
        ProcessGroupPolicy.INHERIT
    ), "INHERIT must not alter how a child is spawned"


def test_context_defaults_to_the_inherited_policy() -> None:
    """A context states ``INHERIT`` unless the caller says otherwise."""
    assert ExecutionContext().process_group is ProcessGroupPolicy.INHERIT


def test_a_string_policy_resolves_to_the_member_it_names() -> None:
    """A policy spelled as a string reaches the consumers as a member.

    ``ProcessGroupPolicy`` is a ``StrEnum``, so a caller holding the policy as
    a configured string — ``"own_group"`` from a settings file — can pass it
    straight to the context. Every consumer compares with ``is``, so the raw
    string would match no member and quietly select the inherited default
    instead: the spawn would not open a session and teardown would signal the
    direct child alone, with nothing raised to show for it.
    """
    context = ExecutionContext(process_group="own_group")

    assert context.process_group is ProcessGroupPolicy.OWN_GROUP, (
        "a string policy must resolve to the member it names, not fall back "
        "to the default"
    )
    assert context.resolved_process_group is ProcessGroupPolicy.OWN_GROUP, (
        "the narrow view the execution layer reads must be the same member"
    )


def test_a_string_policy_that_names_no_member_is_rejected() -> None:
    """A misspelling fails where it is written rather than containing nothing.

    The alternative is the silent degradation above, which is the one outcome
    the policy's design rules out: accepting a containment request while
    delivering no containment. A near miss such as ``"own-group"`` therefore
    has to raise, and the message has to name what was expected, because the
    caller's next step is to correct the spelling they hold.
    """
    with pytest.raises(ValueError, match="invalid process_group") as excinfo:
        ExecutionContext(process_group="own-group")

    assert "'own-group'" in str(excinfo.value), (
        f"the rejected spelling must be named; got {excinfo.value}"
    )
    assert "own_group" in str(excinfo.value), (
        f"the accepted spellings must be named; got {excinfo.value}"
    )


@dc.dataclass(frozen=True, slots=True)
class _SpawnedArgv:
    """The argv the run handed to the spawn call."""

    argv: tuple[str, ...]
    start_new_session: bool


def _recording_spawn(
    recorded: list[_SpawnedArgv],
) -> cabc.Callable[
    [_wait4_process.DirectProcessConfig],
    cabc.Awaitable[asyncio.subprocess.Process],
]:
    """Build a spawn double that records its inputs, then spawns for real.

    The double is a closure over *recorded* so the two tests below share the
    recording without either owning the other's list, and it delegates to the
    real spawn so the recorded call is the production call rather than a
    stub's.

    Returns
    -------
    cabc.Callable
        A drop-in replacement for ``_wait4_process.spawn_direct_process``:
        it takes the spawn config and awaits the spawned process.
    """
    real_spawn = _wait4_process.spawn_direct_process

    async def recording_spawn(
        config: _wait4_process.DirectProcessConfig,
    ) -> asyncio.subprocess.Process:
        """Record the spawn inputs, then spawn for real."""
        recorded.append(_SpawnedArgv(config.argv, config.start_new_session))
        return await real_spawn(config)

    return recording_spawn


def test_owned_policy_spawns_the_child_as_a_session_leader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The policy reaches the spawn call, rather than only the teardown.

    Signalling a group the run does not lead would be a containment claim with
    nothing behind it, so the test asserts on the spawn flag itself instead of
    inferring it from teardown behaviour. The child is a real one so the
    recorded call is the production call, not a stub's.
    """
    recorded: list[_SpawnedArgv] = []
    monkeypatch.setattr(
        _wait4_process, "spawn_direct_process", _recording_spawn(recorded)
    )

    async def run_case() -> None:
        """Run one command under the owning policy."""
        cmd = sh.make(ECHO)("-n", "owned")
        await cmd.run(
            output=RunOutputOptions(capture=True, echo=False),
            context=ExecutionContext(process_group=ProcessGroupPolicy.OWN_GROUP),
        )

    asyncio.run(run_case())

    assert recorded, "the run must have spawned through the recorded call"
    assert recorded[0].start_new_session, (
        "OWN_GROUP must spawn the child as its own session leader"
    )


def test_the_lines_path_spawns_its_child_as_a_session_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SafeCmd.lines()`` carries the policy too, not only ``run()``.

    The two paths reach the spawn through different code — one builds a
    subprocess execution, the other builds a line-stream run — so a policy
    wired into only one of them would leave the other inheriting silently.
    Iterating a line stream is exactly the case ownership matters for, because
    a line consumer is what outlives a descendant that holds the pipe.

    The child is one that ends immediately: the assertion is about the spawn
    arguments, so the run only has to reach the spawn and finish.
    """
    recorded: list[_SpawnedArgv] = []
    monkeypatch.setattr(
        _wait4_process, "spawn_direct_process", _recording_spawn(recorded)
    )

    async def run_case() -> None:
        """Iterate one command's lines under the owning policy."""
        cmd = sh.make(ECHO)("-n", "owned")
        stream = cmd.lines(
            output=RunOutputOptions(capture=True, echo=False),
            context=ExecutionContext(process_group=ProcessGroupPolicy.OWN_GROUP),
        )
        async for _event in stream:
            pass

    asyncio.run(run_case())

    assert recorded, "the run must have spawned through the recorded call"
    assert recorded[0].start_new_session, (
        "OWN_GROUP must spawn a line stream's child as its own session leader"
    )
