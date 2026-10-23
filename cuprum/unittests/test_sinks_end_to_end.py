"""End-to-end tests for an active presentation sink on a real run.

The adapter's own tests pin the sequence of workflow commands it emits; these
tests pin the integration around it. Real subprocesses driven through the
public ``run`` and ``run_sync`` command and pipeline APIs, with a forced
``GitHubActionsSink``, must frame the run's mirrored output — and only that
output — without changing the result the caller sees.
"""

from __future__ import annotations

import io
import typing as typ

from cuprum import ScopeConfig, scoped, sh
from cuprum._sink_lifecycle import _run_label
from cuprum.sh import RunOutputOptions
from cuprum.sinks import GitHubActionsSink
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.redaction import RedactionPolicy

_LEASE_PREFIX = "::stop-commands::"


def _forced_sink(
    *,
    argv_title: bool = False,
    redaction: RedactionPolicy | None = None,
    title: str | None = None,
) -> tuple[GitHubActionsSink, io.StringIO]:
    """Build a GitHub Actions sink over a buffer, activated regardless of CI."""
    buffer = io.StringIO()
    sink = GitHubActionsSink(
        typ.cast("typ.IO[str]", buffer),
        argv_title=argv_title,
        force=True,
        redaction=redaction,
        title=title,
    )
    return sink, buffer


def _stop_token(value: str) -> str:
    """Return the token from the lease line of a framed run's output."""
    lease = next(line for line in value.splitlines() if line.startswith(_LEASE_PREFIX))
    return lease.removeprefix(_LEASE_PREFIX)


def _assert_framed(
    value: str,
    command: sh.SafeCmd,
    *,
    child_output: str = "inside the group",
    expected_group_title: str | None = None,
) -> None:
    """Assert the framing around one run's single mirrored output line.

    Parameters
    ----------
    value : str
        The sink buffer's contents after the run.
    command : sh.SafeCmd
        The command the run executed; its bounded run label titles the group
        unless an expected title is supplied.
    child_output : str
        The one line echoed between the start and end framing.
    expected_group_title : str | None
        The expected title when a caller explicitly selects one.
    """
    group, lease, child, release, endgroup = value.splitlines()
    token = lease.removeprefix(_LEASE_PREFIX)
    group_title = (
        _run_label(command) if expected_group_title is None else expected_group_title
    )
    expected_group = f"::group::{group_title}"
    assert group == expected_group, (
        f"the group must use its configured title; "
        f"got {group!r}, expected {expected_group!r}"
    )
    assert lease == f"{_LEASE_PREFIX}{token}", (
        f"the lease is opened with its token; got {lease!r}"
    )
    assert child == child_output, (
        f"mirrored output lands inside the framing; got {child!r}"
    )
    assert release == f"::{token}::", (
        f"the lease closes on the token alone; got {release!r}"
    )
    assert endgroup == "::endgroup::", (
        f"the group closes after the lease release; got {endgroup!r}"
    )


def test_forced_sink_frames_real_run_and_routes_echoed_output() -> None:
    """A forced sink brackets one real run's mirrored output, result intact."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    command = python("-c", "print('inside the group')")
    sink, buffer = _forced_sink()

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = command.run_sync(output=RunOutputOptions(echo=True, sink=sink))

    assert result.ok is True, f"the framed run must still succeed; got {result!r}"
    assert result.stdout == "inside the group\n", "capture must be unchanged"
    _assert_framed(buffer.getvalue(), command)


def test_child_workflow_commands_stay_inside_the_lease() -> None:
    """A child cannot close the group early by printing workflow commands."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    command = python(
        "-c",
        "print('::endgroup::'); print('::error title=child::spoofed')",
    )
    sink, buffer = _forced_sink()

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = command.run_sync(output=RunOutputOptions(echo=True, sink=sink))

    value = buffer.getvalue()
    assert result.ok is True, f"the framed run must still succeed; got {result!r}"
    assert result.stdout == "::endgroup::\n::error title=child::spoofed\n", (
        f"capture must be unchanged by the lease; got {result.stdout!r}"
    )
    release = f"::{_stop_token(value)}::\n"
    # Every command the child wrote precedes the release, so the runner ignores
    # it; the endgroup the runner acts on is the session's own, written after.
    assert value.index("::error title=child::spoofed") < value.index(release), (
        "the child's spoofed annotation must precede the lease release"
    )
    assert value.index("::endgroup::\n") < value.index(release), (
        "the child's spoofed endgroup must precede the lease release"
    )
    assert value.index(release) < value.rindex("::endgroup::\n"), (
        "the session's own endgroup must follow the lease release"
    )


def _assert_between_framing(value: str, *lines: str) -> None:
    """Assert each of *lines* falls between the group opening and lease release.

    Parameters
    ----------
    value : str
        The sink buffer's contents after the run.
    *lines : str
        The echoed output lines that must be framed.
    """
    opened = value.index("::group::")
    release = value.index(f"::{_stop_token(value)}::\n")
    for line in lines:
        index = value.index(line)
        assert opened < index < release, (
            f"the {line!r} echo must land inside the framing; "
            f"found it at {index} outside ({opened}, {release})"
        )


def test_forced_sink_frames_both_echoed_streams() -> None:
    """Both echoed streams land inside one session's framing, capture intact.

    A single session brackets the whole run, so the stdout and stderr echoes
    are written through the same log destination. Both must fall between the
    session's opening frame and its lease release; a stream that escaped the
    framing would appear above the group or after the release.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    command = python(
        "-c",
        "import sys; print('out line'); sys.stderr.write('err line\\n')",
    )
    sink, buffer = _forced_sink()

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = command.run_sync(output=RunOutputOptions(echo=True, sink=sink))

    assert result.ok is True, f"the framed run must still succeed; got {result!r}"
    assert result.stdout == "out line\n", (
        f"stdout capture must be unchanged; got {result.stdout!r}"
    )
    assert result.stderr == "err line\n", (
        f"stderr capture must be unchanged; got {result.stderr!r}"
    )

    value = buffer.getvalue()
    assert value.startswith("::group::"), (
        f"the group must open the session before either stream; got {value!r}"
    )
    _assert_between_framing(value, "out line", "err line")
    assert value.endswith("::endgroup::\n"), (
        f"the group must close after the lease release; got {value!r}"
    )


def test_forced_sink_frames_pipeline_and_keeps_results() -> None:
    """A pipeline with a forced sink is framed once and reports its stages.

    The framing brackets the whole pipeline rather than each stage, and the
    annotation reports the first failing stage's outcome.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    producer = python("-c", "print('piped')")
    failing = python("-c", "import sys; sys.stdin.read(); raise SystemExit(4)")
    sink, buffer = _forced_sink()

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = (producer | failing).run_sync(
            output=RunOutputOptions(echo=True, sink=sink),
        )

    value = buffer.getvalue()
    codes = [stage.exit_code for stage in result.stages]
    assert codes == [0, 4], f"both stages must report their own exit code; got {codes}"
    assert result.failure_index == 1, (
        f"the failure index must name the failing stage; got {result.failure_index}"
    )
    assert value.count("::group::") == 1, "one pipeline opens one group"
    assert value.startswith("::group::pipeline\n"), (
        f"a pipeline titles its group 'pipeline'; got {value!r}"
    )
    assert value.endswith("::error title=pipeline::exit_nonzero\n"), (
        f"the annotation must report the pipeline's categorical outcome; got {value!r}"
    )


# ---------------------------------------------------------------------------
# The convenience flags, driven end to end
# ---------------------------------------------------------------------------

_ON_CI = "true"


def _flags_output(
    *,
    group: bool = False,
    annotate_failure: bool = False,
    capture: bool = True,
    echo: bool = True,
) -> RunOutputOptions:
    """Build options carrying only the convenience flags under test.

    No ``sink`` is passed: these tests exercise the synthesis, and the
    synthesized adapter's behaviour is what a caller of the flags gets.

    Returns
    -------
    RunOutputOptions
        Options carrying *flags* and no sink.
    """
    return RunOutputOptions(
        capture=capture,
        echo=echo,
        group=group,
        annotate_failure=annotate_failure,
    )


def _python_command(
    *source: str,
) -> tuple[sh.SafeCmd, frozenset[sh.Program]]:
    """Build one interpreter command and the allowlist entry it needs."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    return python("-c", *source), frozenset([python_program])


def _annotations(value: str) -> list[str]:
    """Return the annotation payloads in a run's workflow-command output.

    Counting ``"::error"`` as a substring would not do: an annotation for the
    ``error`` outcome ends ``::error`` because that *is* the categorical
    detail, so one annotation contains the substring twice. Only a line that
    opens with the command and its space is an annotation.

    Returns
    -------
    list[str]
        One payload per annotation, in the order the session wrote them.
    """
    return [
        line.removeprefix("::error ")
        for line in value.splitlines()
        if line.startswith("::error ")
    ]
