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
import dataclasses as dc
import io
import os
import typing as typ

import pytest

from cuprum.sh import RunOutputOptions, StdioTarget
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.sh import CommandResult, LineStream, SafeCmd
    from tests.helpers.execution import ExecuteFn, _RunKwargs


_WRITE_STDOUT = "import sys; sys.stdout.write('out\\n'); sys.stdout.flush()"
_WRITE_STDERR = "import sys; sys.stderr.write('err\\n'); sys.stderr.flush()"
_WRITE_BOTH = f"{_WRITE_STDOUT}; {_WRITE_STDERR}"
_WRITE_THEN_FAIL = f"{_WRITE_BOTH}; raise SystemExit(3)"

# The child reports the contents of the descriptor it was handed on fd 2 onto
# its own stdout, which is where the line stream can see it. A report sent back
# on stderr would travel through the very descriptor under test.
_READ_BACK_STDERR = (
    "import os, sys; os.lseek(2, 0, 0); sys.stdout.write(os.read(2, 4096).decode())"
)

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


@dc.dataclass(frozen=True, slots=True)
class DescriptorLog:
    """Descriptors cuprum opened for one path target, and those closed again."""

    opened: list[int]
    closed: list[int]


@pytest.fixture
def target_descriptor_log(
    monkeypatch: pytest.MonkeyPatch,
) -> cabc.Callable[[Path], DescriptorLog]:
    """Track descriptor opens and closes for a named owned path.

    Both hooks are process-wide, so they also see asyncio's own pipe
    descriptors and the interpreter's file writes; narrowing to the descriptors
    opened *for this target* is what makes the comparison mean what it says. A
    leaked target descriptor still fails, since it is counted as opened and then
    never appears among the closes.

    Returns
    -------
    collections.abc.Callable[[Path], DescriptorLog]
        A starter that installs the hooks for one target path and returns the
        log they fill for it.
    """

    def track(log: Path) -> DescriptorLog:
        """Install the hooks and return the lists they fill."""
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
        return DescriptorLog(opened, closed)

    return track


def _assert_no_descriptor_leak(descriptors: DescriptorLog) -> None:
    """Fail unless the target was opened and every open was matched by a close.

    Reopening the file and checking it is a regular file would not show this:
    both a closed and a leaked descriptor leave the path intact. Only counting
    the closes against the opens distinguishes them.
    """
    assert descriptors.opened, "the path target must actually have been opened"
    assert sorted(descriptors.closed) == sorted(descriptors.opened), (
        "every descriptor cuprum opened for the target must be closed again; "
        f"opened={descriptors.opened} closed={descriptors.closed}"
    )


@_posix_only
def test_path_target_does_not_leak_the_descriptor(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
    target_descriptor_log: cabc.Callable[[Path], DescriptorLog],
) -> None:
    """Cuprum's copy of a path descriptor is closed once the child has it.

    The run is observed from the spawn layer, which is the only place the
    descriptor exists: :func:`_spawn_subprocess` opens it, hands it to the
    child, and closes its own copy.
    """
    _, execute = execution_strategy
    log = tmp_path / "out.log"
    command = python_builder("-c", _WRITE_STDOUT)
    descriptors = target_descriptor_log(log)

    result = execute(
        command, {"output": _redirect_options(stdout=StdioTarget.path(log))}
    )

    assert result.exit_code == 0, "a redirected run should exit cleanly"
    _assert_no_descriptor_leak(descriptors)
    assert log.read_text(encoding="utf-8") == "out\n", (
        "the child's bytes must survive the descriptor being closed"
    )


@_posix_only
def test_partial_owned_open_closes_what_it_already_opened(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed second open does not leak the descriptor the first one opened.

    Both streams can name an owned path, and they are opened in stdout-then-
    stderr order. The ``finally`` that closes owned descriptors wraps the
    *spawn*, so a stderr open that fails never reaches it: without cleanup the
    stdout descriptor opened moments earlier would leak, once per failed run.

    Only the first open is forced to succeed and the second to fail, so the
    assertion is about the partial case specifically rather than about the
    general path, which ``test_path_target_does_not_leak_the_descriptor``
    already covers.
    """
    _, execute = execution_strategy
    out_log = tmp_path / "out.log"
    err_log = tmp_path / "err.log"
    command = python_builder("-c", _WRITE_BOTH)

    opened: list[int] = []
    closed: list[int] = []
    real_open = os.open
    real_close = os.close

    def tracking_open(path: object, flags: int, *args: object) -> int:
        """Open the stdout target, then fail on the stderr target."""
        if str(path) == str(err_log):
            msg = "stderr target refused"
            raise OSError(msg)
        fd = real_open(path, flags, *args)  # ty: ignore[invalid-argument-type]
        if str(path) == str(out_log):
            opened.append(fd)
        return fd

    def tracking_close(fd: int) -> None:
        """Close a descriptor, recording it when the target opened it."""
        if fd in opened:
            closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", tracking_close)

    with pytest.raises(OSError, match="stderr target refused"):
        execute(
            command,
            {
                "output": _redirect_options(
                    stdout=StdioTarget.path(out_log),
                    stderr=StdioTarget.path(err_log),
                )
            },
        )

    assert opened, "the stdout target must have been opened before stderr failed"
    assert sorted(closed) == sorted(opened), (
        "a spawn that never happened must still close the descriptors it opened "
        f"before failing; opened={opened} closed={closed}"
    )


@_posix_only
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
    capfd: pytest.CaptureFixture[str],
) -> None:
    """An inherited stdout is the parent's own stream, not a pipe cuprum reads.

    The child writes a sentinel that could not appear anywhere else, and
    ``capfd`` reads it back off the *descriptor* the parent held while the run
    executed. That is the boundary the claim is about: an exit code and a
    ``None`` stdout are both satisfied by a child whose output went nowhere at
    all, so only the sentinel arriving on the caller's own descriptor shows the
    child inherited the parent's real stream rather than a substitute.
    """
    label, execute = execution_strategy
    sentinel = f"inherit-sentinel-{label}"
    command = python_builder(
        "-c",
        f"import sys; sys.stdout.write({sentinel!r}); sys.stdout.flush()",
    )

    result = execute(
        command, {"output": _redirect_options(stdout=StdioTarget.inherit())}
    )

    assert result.exit_code == 0, "an inherited stdout should exit cleanly"
    assert result.stdout is None, "an inherited stream is not a parent-side pipe"
    assert capfd.readouterr().out == sentinel, (
        "the child's bytes must arrive on the parent's own stdout descriptor; "
        "anything else means the run bound the stream somewhere other than "
        "the parent's real stream"
    )


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
def test_borrowed_file_object_written_before_iteration_reaches_the_child(
    python_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
) -> None:
    """On the ``lines()`` path the flush must still follow the fork.

    ``lines()`` resolves its stdio when it is *called* but spawns the child only
    when the returned stream is first *iterated*, so a flush performed during
    resolution fires early and silently drops anything the caller writes in
    between. This test writes after the call and before the iteration, which is
    the only window that distinguishes the two placements.

    The child reports what it read on its own stdout, which the line stream
    observes; reporting on stderr would send the answer through the descriptor
    under test.
    """
    log = tmp_path / "borrowed.log"
    command = python_builder("-c", _READ_BACK_STDERR)

    with log.open("w+b") as handle:
        handle.write(b"caller-first\n")
        stream = command.lines(output=_redirect_options(stderr=StdioTarget.fd(handle)))
        # After resolution, before the fork: a resolver-side flush has already
        # happened and cannot see this.
        handle.write(b"caller-second\n")
        lines = asyncio.run(_collect_stdout_lines(stream))

    assert lines == ["caller-first", "caller-second"], (
        "a borrowed object must be flushed immediately before the fork, so "
        f"bytes written between calling lines() and iterating it reach the "
        f"child; the child read {lines!r}"
    )


async def _collect_stdout_lines(stream: LineStream) -> list[str]:
    """Drain a line stream and return the text of each stdout line."""
    return [event.text async for event in stream if event.stream == "stdout"]


@_posix_only
def test_path_target_closed_after_a_failing_run(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
    target_descriptor_log: cabc.Callable[[Path], DescriptorLog],
) -> None:
    """A non-zero exit closes the owned descriptor just the same."""
    _, execute = execution_strategy
    log = tmp_path / "out.log"
    command = python_builder("-c", _WRITE_THEN_FAIL)
    descriptors = target_descriptor_log(log)

    result = execute(
        command, {"output": _redirect_options(stdout=StdioTarget.path(log))}
    )

    _assert_no_descriptor_leak(descriptors)
    assert result.exit_code == 3, "the child's own exit code must be reported"
    assert log.read_text(encoding="utf-8") == "out\n", (
        "output written before the failure must still be in the file"
    )


@_posix_only
def test_path_target_closed_after_a_timeout(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
    target_descriptor_log: cabc.Callable[[Path], DescriptorLog],
) -> None:
    """A timed-out run still closes the descriptor cuprum opened."""
    _, execute = execution_strategy
    log = tmp_path / "out.log"
    command = python_builder("-c", "import time; time.sleep(30)")
    descriptors = target_descriptor_log(log)

    with pytest.raises(TimeoutError):
        execute(
            command,
            {
                "output": _redirect_options(stdout=StdioTarget.path(log)),
                "timeout": 0.05,
            },
        )

    _assert_no_descriptor_leak(descriptors)
    assert not log.read_text(encoding="utf-8"), (
        "a timed-out child wrote nothing, and the file must still be closed"
    )


@_posix_only
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


@_posix_only
def test_descriptorless_file_object_is_refused_with_the_stream_named(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """An in-memory object cannot be inherited, and the refusal says which stream.

    ``StdioTarget.fd`` accepts "an open file object", and ``io.StringIO`` is
    one, so nothing rejects it until the spawn layer asks for its descriptor.
    The object's own ``fileno`` failure is a bare ``fileno`` naming neither the
    stream nor the fault, which is useless to a caller with two redirected
    streams; the resolver re-raises with both, keeping the original exception
    reachable as ``__cause__``.

    ``io.UnsupportedOperation`` is a subclass of both ``OSError`` and
    ``ValueError``, so re-raising as a plain ``ValueError`` is the one choice
    that would silently drop the ``OSError`` arm for callers catching that.
    The raised type is asserted directly, not merely through ``__cause__``: a
    caller writing ``except OSError`` catches only what is raised, so pinning
    the cause alone would let the broad arm be dropped while the test stayed
    green. Both arms are checked for that reason.
    """
    command = python_builder("-c", _WRITE_STDOUT)

    with pytest.raises(ValueError, match="stdout") as info:
        command.run_sync(output=_redirect_options(stdout=StdioTarget.fd(io.StringIO())))

    assert "StringIO" in str(info.value), (
        "the message must name the offending object type, not just the stream"
    )
    assert isinstance(info.value, OSError), (
        "callers catching OSError must see the refusal itself; only the "
        "raised type decides that, which is why __cause__ is not enough"
    )
    assert isinstance(info.value.__cause__, io.UnsupportedOperation), (
        "the original fileno failure must stay reachable as __cause__"
    )
