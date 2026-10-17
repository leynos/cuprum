"""Backend-specific spawn failure on the standard-stream redirect path.

``test_safe_cmd_redirect.py`` covers the *shared* spawn-entry failure: the
dispatch at ``spawn_direct_process`` refusing, which is the same failure
whichever backend would have been selected. This module covers the other half:
a failure raised inside a *selected* backend's own spawn entry point. A failure
above the dispatch cannot separate the two routes, so it is not evidence that
either route's failure path was exercised at all.

Selection is arranged the way the rest of the suite arranges it. The ``wait4``
arm patches that backend's ``_spawn_popen``; the asyncio arm forces
``wait4_resource_measurement_available`` to report ``False`` and patches
``asyncio.create_subprocess_exec``. Both routes are patched in every case — the
selected one refuses and records, the other is trapped — and the record is
compared against a table written out at module level. A case that only trusted
its own label would be a claim about which route was *asked* for, not about
which one ran.

The ownership rules are the subject, so each case is one the redirect suite
already covers for the shared failure: an owned path target is still closed, and
a borrowed descriptor or file object is still left alone. The borrowed cases
write to their own resource after the failed run, because a close cuprum did not
owe raises from that write and no exit code can reveal it.
"""

from __future__ import annotations

import dataclasses as dc
import os
import typing as typ

import pytest

from cuprum import _wait4_spawn
from cuprum.sh import SafeCmd, StdioTarget
from cuprum.unittests._stdio_redirect_support import (
    _WRITE_STDOUT,
    DescriptorLog,
    _assert_no_descriptor_leak,
    _posix_only,
    _redirect_options,
    execution_strategy,
    python_builder,
    target_descriptor_log,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.sh import CommandResult
    from cuprum.unittests._stdio_redirect_support import ExecuteFn

# Fixtures are resolved by name from the test module's namespace, so the three
# that live in the support module are re-exported rather than merely imported.
__all__ = ["execution_strategy", "python_builder", "target_descriptor_log"]

# The label each arm of ``refusing_backend_spawn`` must record. Written out
# rather than read from ``request.param``: a case that compared the record to
# its own input would only restate the fixture's name for itself. Pinned here,
# the two are independent, so an arm that silently changed routes fails.
_EXPECTED_BACKENDS: typ.Final[dict[str, str]] = {
    "wait4": "wait4",
    "asyncio": "asyncio",
}


def _refuse_to_spawn(*_: object, **__: object) -> typ.NoReturn:
    """Fail the spawn the way a missing executable does."""
    message = "missing"
    raise FileNotFoundError(message)


def _refusing_entry(reached: list[str], label: str) -> cabc.Callable[..., typ.NoReturn]:
    """Build a spawn entry that records the route and refuses to spawn."""

    def refuse(*_: object, **__: object) -> typ.NoReturn:
        """Record that this route ran, then fail the spawn."""
        reached.append(label)
        return _refuse_to_spawn()

    return refuse


def _trapping_entry(label: str) -> cabc.Callable[..., typ.NoReturn]:
    """Build a spawn entry that names the route when the wrong one is taken.

    This is a diagnostic, not the detection mechanism. Detection lives in the
    record assertion the cases make: an asyncio case that stopped turning the
    wait4 route off would spawn a real child and ``pytest.raises`` would report
    ``DID NOT RAISE``. What the trap buys is a failure that says *which* route
    ran, instead of one that says only that something unexpected succeeded.

    Returns
    -------
    collections.abc.Callable[..., typing.NoReturn]
        A spawn entry that raises when reached.
    """

    def refuse(*_: object, **__: object) -> typ.NoReturn:
        """Fail, naming the route that should not have been reached."""
        message = (
            f"the {label} spawn entry was reached, but this case selected the "
            "other backend; the failure under test is not on this route"
        )
        raise AssertionError(message)

    return refuse


@dc.dataclass(slots=True)
class _BackendTraversal:
    """Which spawn routes were reached, and which route the case selected.

    ``reached`` is held live rather than copied. The spawn entry is called
    after the fixture returns, so a snapshot taken at setup would always be
    empty and every case would pass its own assertion for the wrong reason.
    """

    requested: str
    reached: list[str]


@pytest.fixture(params=["wait4", "asyncio"], ids=["wait4", "asyncio"])
def refusing_backend_spawn(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> _BackendTraversal:
    """Make the selected backend's own spawn entry refuse, and record it.

    ``refusing_spawn`` in the support module refuses one level up, at the
    dispatch that chooses between routes, so it exercises neither route. This
    fixture replaces the entry point *inside* the route, which is the only place
    a failure can differ between them.

    Returns
    -------
    _BackendTraversal
        The route this case selected and the routes whose spawn entry was
        actually reached. Both are needed to make the claim; see the type for
        why the second is handed out live.
    """
    reached: list[str] = []

    if request.param == "wait4":
        # Read from the module at call time, so this selects the failure for
        # this case without rebinding the import.
        monkeypatch.setattr(
            _wait4_spawn, "_spawn_popen", _refusing_entry(reached, "wait4")
        )
        monkeypatch.setattr(
            _wait4_spawn.asyncio,
            "create_subprocess_exec",
            _trapping_entry("asyncio"),
        )
    else:
        # ``spawn_direct_process`` reads this at call time; patching the
        # dispatch itself would replace the branch under test. The trap is what
        # reports if this patch ever stops taking effect.
        monkeypatch.setattr(
            _wait4_spawn, "wait4_resource_measurement_available", lambda: False
        )
        monkeypatch.setattr(
            _wait4_spawn.asyncio,
            "create_subprocess_exec",
            _refusing_entry(reached, "asyncio"),
        )
        monkeypatch.setattr(_wait4_spawn, "_spawn_popen", _trapping_entry("wait4"))
    return _BackendTraversal(request.param, reached)


@dc.dataclass(frozen=True, slots=True)
class _BackendFailureCase:
    """One route under test, with the helpers that drive a failure through it.

    The route, the execution strategy, and the builder travel together because
    they are one decision about how a case runs. Passing them as separate
    arguments would let a caller pair one case's route with another's strategy,
    which no type would catch.
    """

    requested: str
    reached: list[str]
    builder: cabc.Callable[..., SafeCmd]
    execute: cabc.Callable[..., CommandResult]
    tmp_path: Path

    def failing_spawn(self, target: StdioTarget) -> None:
        """Run a command whose spawn the selected backend refuses."""
        command = self.builder("-c", _WRITE_STDOUT)
        with pytest.raises(FileNotFoundError):
            self.execute(command, {"output": _redirect_options(stdout=target)})

    def assert_only_the_selected_route_ran(self) -> None:
        """Fail unless the selected route's entry was the only one reached."""
        expected = [_EXPECTED_BACKENDS[self.requested]]
        assert self.reached == expected, (
            "the spawn failure must have come from the route this case "
            f"selected; asked for {self.requested!r} and reached {self.reached!r}"
        )


@pytest.fixture
def backend_failure_case(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    refusing_backend_spawn: _BackendTraversal,
    tmp_path: Path,
) -> _BackendFailureCase:
    """Bundle one route, one execution strategy, and the helpers to drive them."""
    _, execute = execution_strategy
    return _BackendFailureCase(
        requested=refusing_backend_spawn.requested,
        reached=refusing_backend_spawn.reached,
        builder=python_builder,
        execute=execute,
        tmp_path=tmp_path,
    )


@_posix_only
def test_an_owned_path_is_closed_after_a_backend_spawn_failure(
    backend_failure_case: _BackendFailureCase,
    target_descriptor_log: cabc.Callable[[Path], DescriptorLog],
) -> None:
    """The backend that refused to spawn still leaves no owned descriptor open."""
    log = backend_failure_case.tmp_path / "out.log"
    descriptors = target_descriptor_log(log)

    backend_failure_case.failing_spawn(StdioTarget.path(log))

    _assert_no_descriptor_leak(descriptors)
    backend_failure_case.assert_only_the_selected_route_ran()


@_posix_only
def test_a_borrowed_descriptor_survives_a_backend_spawn_failure(
    backend_failure_case: _BackendFailureCase,
) -> None:
    """No backend closes a descriptor the caller lent cuprum when it fails."""
    log = backend_failure_case.tmp_path / "borrowed.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    try:
        backend_failure_case.failing_spawn(StdioTarget.fd(fd))

        os.write(fd, b"caller\n")
        os.fsync(fd)
    finally:
        os.close(fd)

    backend_failure_case.assert_only_the_selected_route_ran()
    assert log.read_text(encoding="utf-8") == "caller\n", (
        "a child that never started wrote nothing, and the caller's own write "
        "must still succeed against the descriptor cuprum was lent"
    )


@_posix_only
def test_a_borrowed_file_object_survives_a_backend_spawn_failure(
    backend_failure_case: _BackendFailureCase,
) -> None:
    """The borrowed flush leaves the object usable when a backend fails."""
    log = backend_failure_case.tmp_path / "borrowed.log"
    with log.open("w+b") as handle:
        backend_failure_case.failing_spawn(StdioTarget.fd(handle))

        handle.write(b"caller\n")
        handle.flush()

    backend_failure_case.assert_only_the_selected_route_ran()
    assert log.read_bytes() == b"caller\n", (
        "a child that never started wrote nothing, and the caller's own write "
        "must still land through the object cuprum was lent"
    )
