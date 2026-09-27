"""Redirection and ownership tests for explicit standard-stream targets.

``RunOutputOptions`` accepts a ``StdioTarget`` for stdout and stderr, and this
module is what makes those targets mean something: the child's stream really
lands where the caller pointed it, and the descriptor is opened by cuprum,
handed over, and closed again — or, for a borrowed target, never touched.

The ownership half is the safety-critical half. "Cuprum closed a file it
opened" and "cuprum closed a descriptor it did not open" are indistinguishable
from the child's exit code; only the caller's own use of a borrowed resource
afterwards tells them apart, which is why every borrowed case here writes to
its descriptor *after* the run.
"""

from __future__ import annotations

import asyncio
import os
import stat
import typing as typ

import pytest

from cuprum.sh import RunOutputOptions, StdioTarget
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.sh import CommandResult, SafeCmd
    from tests.helpers.execution import ExecuteFn, _RunKwargs


_WRITE_STDOUT = "import sys; sys.stdout.write('out\\n'); sys.stdout.flush()"
_WRITE_STDERR = "import sys; sys.stderr.write('err\\n'); sys.stderr.flush()"
_WRITE_BOTH = f"{_WRITE_STDOUT}; {_WRITE_STDERR}"
_WRITE_THEN_FAIL = f"{_WRITE_BOTH}; raise SystemExit(3)"

# Only POSIX descriptor semantics are asserted: on Windows a "borrowed
# descriptor" is a CRT file descriptor, whose inheritance and reuse rules are a
# different contract that this change does not claim to implement.
_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="asserts POSIX descriptor inheritance and ownership",
)


def _execute_async(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the async run() method."""
    return asyncio.run(cmd.run(**kwargs))


def _execute_sync(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the sync run_sync() method."""
    return cmd.run_sync(**kwargs)


@pytest.fixture(params=["async", "sync"], ids=["run()", "run_sync()"])
def execution_strategy(request: pytest.FixtureRequest) -> tuple[str, ExecuteFn]:
    """Provide parameterized execution strategies for run() and run_sync().

    Parameters
    ----------
    request : pytest.FixtureRequest
        The injected fixture request whose ``param`` selects the asynchronous
        or synchronous execution strategy returned here.

    Returns
    -------
    tuple[str, ExecuteFn]
        The strategy label and its execution callable.
    """
    if request.param == "async":
        return ("async", _execute_async)
    return ("sync", _execute_sync)


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    collections.abc.Callable[..., SafeCmd]
        A builder that creates SafeCmd instances for the running interpreter.
    """
    return build_python_builder()


def _redirect_options(
    stdout: StdioTarget | None = None, stderr: StdioTarget | None = None
) -> RunOutputOptions:
    """Build options that redirect both streams, leaving capture off.

    Capture reads a parent-side pipe, so it is incompatible with a redirected
    stream and must be disabled for these runs; ``RunOutputOptions`` rejects
    the combination rather than silently choosing one.

    Returns
    -------
    RunOutputOptions
        Options naming the given targets for stdout and stderr, with capture
        off. An argument left ``None`` leaves that stream at its unset default,
        which resolves to a library-owned pipe.
    """
    return RunOutputOptions(capture=False, stdout=stdout, stderr=stderr)


@_posix_only
def test_path_target_receives_the_children_bytes(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
) -> None:
    """A path target's file holds exactly what the child wrote."""
    _, execute = execution_strategy
    log = tmp_path / "out.log"
    command = python_builder("-c", _WRITE_STDOUT)

    result = execute(
        command,
        {
            "output": _redirect_options(stdout=StdioTarget.path(log)),
        },
    )

    assert result.exit_code == 0, "a redirected run should exit cleanly"
    assert log.read_text(encoding="utf-8") == "out\n", (
        "the file cuprum opened must hold exactly the child's stdout bytes"
    )


@_posix_only
def test_path_target_does_not_leak_the_descriptor(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cuprum's copy of a path descriptor is closed once the child has it.

    The run is observed from the spawn layer, which is the only place the
    descriptor exists: :func:`_spawn_subprocess` opens it, hands it to the
    child, and closes its own copy. Both hooks are process-wide, so they also
    see asyncio's own pipe descriptors and the interpreter's file writes;
    narrowing to the descriptors opened *for this target* is what makes the
    comparison mean what it says. A leaked target descriptor still fails, since
    it is counted as opened and then never appears among the closes.
    """
    _, execute = execution_strategy
    log = tmp_path / "out.log"
    command = python_builder("-c", _WRITE_STDOUT)

    opened: list[int] = []
    closed: list[int] = []
    real_open = os.open
    real_close = os.close

    def tracking_open(path: object, flags: int, *args: object) -> int:
        """Open the file, recording a descriptor the target owns."""
        fd = real_open(path, flags, *args)  # ty: ignore[invalid-argument-type]
        if str(path) == str(log):
            opened.append(fd)
        return fd

    def tracking_close(fd: int) -> None:
        """Close a descriptor, recording it when the target opened it."""
        if fd in opened:
            closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", tracking_close)

    result = execute(
        command, {"output": _redirect_options(stdout=StdioTarget.path(log))}
    )

    assert result.exit_code == 0, "a redirected run should exit cleanly"
    assert opened, "the path target must actually have been opened"
    assert sorted(closed) == sorted(opened), (
        "every descriptor cuprum opened for the target must be closed again; "
        f"opened={opened} closed={closed}"
    )
    assert log.read_text(encoding="utf-8") == "out\n", (
        "the child's bytes must survive the descriptor being closed"
    )


def test_redirected_stream_is_not_captured(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
) -> None:
    """A redirected stream reaches no parent-side pipe, so capture sees nothing."""
    _, execute = execution_strategy
    log = tmp_path / "out.log"
    command = python_builder("-c", _WRITE_BOTH)

    result = execute(
        command,
        {
            "output": _redirect_options(
                stdout=StdioTarget.path(log),
                stderr=StdioTarget.path(tmp_path / "err.log"),
            ),
        },
    )

    assert result.exit_code == 0, "both streams redirected should still exit cleanly"
    assert result.stdout is None, "a redirected stdout has no parent-side pipe"
    assert result.stderr is None, "a redirected stderr has no parent-side pipe"


@_posix_only
def test_inherit_target_leaves_the_parent_stream_alone(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """An inherited stdout is the parent's own stream, not a pipe cuprum reads."""
    _, execute = execution_strategy
    command = python_builder("-c", _WRITE_STDOUT)

    result = execute(
        command, {"output": _redirect_options(stdout=StdioTarget.inherit())}
    )

    assert result.exit_code == 0, "an inherited stdout should exit cleanly"
    assert result.stdout is None, "an inherited stream is not a parent-side pipe"


@_posix_only
def test_borrowed_descriptor_survives_the_run(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
) -> None:
    """Cuprum never closes a descriptor it borrowed.

    The caller's own write after the run is the assertion. A ``close`` cuprum
    did not owe the caller raises ``EBADF`` from it, which is exactly the
    defect no exit code can reveal.
    """
    _, execute = execution_strategy
    log = tmp_path / "borrowed.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    try:
        command = python_builder("-c", _WRITE_STDOUT)

        result = execute(
            command, {"output": _redirect_options(stdout=StdioTarget.fd(fd))}
        )

        assert result.exit_code == 0, "a borrowed-descriptor run should exit cleanly"
        # The caller's descriptor is still theirs: this write is the proof.
        os.write(fd, b"caller\n")
        os.fsync(fd)
    finally:
        os.close(fd)

    assert log.read_text(encoding="utf-8") == "out\ncaller\n", (
        "the child's bytes and the caller's own write must both land in the file"
    )


@_posix_only
def test_borrowed_file_object_is_flushed_before_spawn(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
) -> None:
    """A borrowed file object's buffered bytes are visible to the child.

    The child seeks to the start and reports what it finds. Without the flush
    cuprum performs before spawning, the caller's bytes would still be sitting
    in the object's buffer and the child would read an empty file.
    """
    _, execute = execution_strategy
    log = tmp_path / "borrowed.log"
    command = python_builder("-c", _WRITE_STDOUT)

    with log.open("w+b") as handle:
        # Written and deliberately neither flushed nor sought, so the bytes sit
        # in the object's buffer. Seeking would flush them itself, and the test
        # would then pass even with no flush in the implementation.
        handle.write(b"caller-first\n")

        result = execute(
            command,
            {"output": _redirect_options(stdout=StdioTarget.fd(handle))},
        )

        assert result.exit_code == 0, "a borrowed-file-object run exits cleanly"

    written = log.read_bytes()
    assert written == b"caller-first\nout\n", (
        "the child must inherit the borrowed descriptor at the caller's offset, "
        f"so its bytes land after the flushed ones; got {written!r}"
    )


@_posix_only
def test_path_target_closed_after_a_failing_run(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
) -> None:
    """A non-zero exit closes the owned descriptor just the same."""
    _, execute = execution_strategy
    log = tmp_path / "out.log"
    command = python_builder("-c", _WRITE_THEN_FAIL)

    result = execute(
        command, {"output": _redirect_options(stdout=StdioTarget.path(log))}
    )

    assert result.exit_code == 3, "the child's own exit code must be reported"
    assert log.read_text(encoding="utf-8") == "out\n", (
        "output written before the failure must still be in the file"
    )
    assert stat.S_ISREG(log.stat().st_mode), "the file must be a plain closed file"


def test_path_target_closed_after_a_timeout(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
) -> None:
    """A timed-out run still closes the descriptor cuprum opened."""
    _, execute = execution_strategy
    log = tmp_path / "out.log"
    command = python_builder("-c", "import time; time.sleep(30)")

    with pytest.raises(TimeoutError):
        execute(
            command,
            {
                "output": _redirect_options(stdout=StdioTarget.path(log)),
                "timeout": 0.05,
            },
        )

    # The window that proves the close: the file must be reopenable and empty.
    assert log.read_text(encoding="utf-8") == "", (
        "a timed-out child wrote nothing, and the file must still be closed"
    )


def test_both_streams_can_be_redirected_at_once(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
) -> None:
    """Distinct paths for stdout and stderr each receive their own stream."""
    _, execute = execution_strategy
    out_log = tmp_path / "out.log"
    err_log = tmp_path / "err.log"
    command = python_builder("-c", _WRITE_BOTH)

    result = execute(
        command,
        {
            "output": _redirect_options(
                stdout=StdioTarget.path(out_log),
                stderr=StdioTarget.path(err_log),
            ),
        },
    )

    assert result.exit_code == 0, "both streams redirected should exit cleanly"
    assert out_log.read_text(encoding="utf-8") == "out\n", (
        "stdout must land only in the stdout target"
    )
    assert err_log.read_text(encoding="utf-8") == "err\n", (
        "stderr must land only in the stderr target"
    )


@_posix_only
def test_pipe_target_still_captures(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """An explicit pipe target keeps capture working, proving the rule is a boundary.

    This is the accepted near-miss for the capture/redirect rejection rows: the
    same options object, with ``StdioTarget.pipe()`` instead of a file, is
    entirely legal and captures as usual.
    """
    _, execute = execution_strategy
    command = python_builder("-c", _WRITE_BOTH)

    result = execute(
        command,
        {
            "output": RunOutputOptions(
                capture=True, stdout=StdioTarget.pipe(), stderr=StdioTarget.pipe()
            ),
        },
    )

    assert result.exit_code == 0, "an explicit pipe target should exit cleanly"
    assert result.stdout == "out\n", "an explicit pipe target must still capture"
    assert result.stderr == "err\n", "an explicit pipe target must still capture"


def test_capture_with_a_path_target_is_rejected(
    tmp_path: Path,
) -> None:
    """Capture needs a parent-side pipe, so a file target is a contradiction."""
    with pytest.raises(ValueError, match="stdout cannot be redirected") as info:
        RunOutputOptions(capture=True, stdout=StdioTarget.path(tmp_path / "out.log"))

    assert "capture" in str(info.value), (
        "the message must name the conflicting setting, not just the field"
    )
