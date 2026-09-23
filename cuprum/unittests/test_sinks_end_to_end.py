"""End-to-end tests for an active presentation sink on a real run.

The adapter's own tests pin the sequence of workflow commands it emits; these
tests pin the integration around it. Real subprocesses driven through the
public ``run`` and ``run_sync`` command and pipeline APIs, with a forced
``GitHubActionsSink``, must frame the run's mirrored output — and only that
output — without changing the result the caller sees.
"""

from __future__ import annotations

import asyncio
import io
import typing as typ
from pathlib import Path

import pytest

from cuprum import Program, ProgramCatalogue, ScopeConfig, scoped, sh
from cuprum._sink_lifecycle import _run_label
from cuprum.sh import RunOutputOptions
from cuprum.sinks import GitHubActionsSink
from cuprum.sinks.github_actions import _escape_property
from tests.helpers.catalogue import python_catalogue

_LEASE_PREFIX = "::stop-commands::"


def _forced_sink() -> tuple[GitHubActionsSink, io.StringIO]:
    """Build a GitHub Actions sink over a buffer, activated regardless of CI."""
    buffer = io.StringIO()
    sink = GitHubActionsSink(typ.cast("typ.IO[str]", buffer), force=True)
    return sink, buffer


def _stop_token(value: str) -> str:
    """Return the token from the lease line of a framed run's output."""
    lease = next(line for line in value.splitlines() if line.startswith(_LEASE_PREFIX))
    return lease.removeprefix(_LEASE_PREFIX)


def _assert_framed(value: str, command: sh.SafeCmd) -> None:
    """Assert the framing around one run's single mirrored output line.

    Parameters
    ----------
    value : str
        The sink buffer's contents after the run.
    command : sh.SafeCmd
        The command the run executed; its argv titles the group.
    """
    group, lease, child, release, endgroup = value.splitlines()
    token = lease.removeprefix(_LEASE_PREFIX)
    expected_group = f"::group::{' '.join(command.argv_with_program)}"
    assert group == expected_group, (
        f"the group must be titled with the command's argv; "
        f"got {group!r}, expected {expected_group!r}"
    )
    assert lease == f"{_LEASE_PREFIX}{token}", (
        f"the lease is opened with its token; got {lease!r}"
    )
    assert child == "inside the group", (
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


def test_failing_run_annotates_without_argv() -> None:
    """A real failing run's annotation carries the bounded label, not argv."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    secret = "s3cret-token-9f2aXq7"  # ruff: ignore[hardcoded-password-string] - synthetic test token, never a real credential.
    command = python("-c", "raise SystemExit(3)", secret)
    sink, buffer = _forced_sink()

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = command.run_sync(output=RunOutputOptions(echo=True, sink=sink))

    value = buffer.getvalue()
    assert result.exit_code == 3, f"the failing run must report 3; got {result!r}"
    # The group is titled with argv, so the annotation has to be the one place
    # the arguments do not appear.
    assert secret in value, f"argv still titles the group; got {value!r}"
    annotation = value.split("::error ", 1)[1]
    assert value.count("::error ") == 1, (
        f"a failure must annotate exactly once; got {value!r}"
    )
    assert secret not in annotation, (
        f"the annotation must not republish argv; got {annotation!r}"
    )
    assert annotation == (
        f"title={_escape_property(_run_label(command))}::exit_nonzero\n"
    ), "the annotation names the program and no arguments"


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
) -> RunOutputOptions:
    """Build options carrying only the convenience flags under test.

    No ``sink`` is passed: these tests exercise the synthesis, and the
    synthesized adapter's behaviour is what a caller of the flags gets.

    Returns
    -------
    RunOutputOptions
        Echoing options carrying *flags* and no sink.
    """
    return RunOutputOptions(
        echo=True,
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


def test_flags_frame_successful_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``group=True`` alone frames a real run with no sink passed by hand.

    The run is routed through the synthesized adapter, which is why the
    environment has to look like a runner: the flags construct a sink with no
    ``force``, so a locally unset ``GITHUB_ACTIONS`` would decline and frame
    nothing. Output goes to stderr because that is where GitHub Actions reads
    workflow commands, overriding the ``echo`` sink the run would otherwise
    mirror to.
    """
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    command, allowlist = _python_command("print('inside the group')")

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = command.run_sync(output=_flags_output(group=True))

    assert result.ok is True, f"the framed run must still succeed; got {result!r}"
    assert result.stdout == "inside the group\n", "capture must be unchanged"
    _assert_framed(capsys.readouterr().err, command)


def test_flags_annotate_async_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The async command entry point applies both convenience flags."""
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    command, allowlist = _python_command("raise SystemExit(5)")

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = asyncio.run(
            command.run(
                output=_flags_output(group=True, annotate_failure=True),
            )
        )

    value = capsys.readouterr().err
    assert result.exit_code == 5, f"the failing run must report 5; got {result!r}"
    assert value.count("::group::") == 1, "the async run must open one group"
    assert value.count("::endgroup::") == 1, "the async run must close one group"
    assert _annotations(value) == [
        f"title={_escape_property(_run_label(command))}::exit_nonzero",
    ], "the async run must annotate its categorical failure"


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


def test_flags_annotate_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``annotate_failure`` alone annotates, and frames no group.

    The annotation toggle is independent of the group toggle, so this is the
    combination that buys a run-summary entry without collapsible logs: the
    output is otherwise untouched, and the run's arguments appear nowhere at
    all.
    """
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    secret = "s3cret-token-9f2aXq7"  # ruff: ignore[hardcoded-password-string] - synthetic test token, never a real credential.
    command, allowlist = _python_command("raise SystemExit(3)", secret)

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = command.run_sync(output=_flags_output(annotate_failure=True))

    value = capsys.readouterr().err
    assert result.exit_code == 3, f"the failing run must report 3; got {result!r}"
    assert "::group::" not in value, f"annotate-only must frame no group; got {value!r}"
    assert _annotations(value) == [
        f"title={_escape_property(_run_label(command))}::exit_nonzero",
    ], "the annotation names the program and no arguments"
    assert secret not in value, (
        f"a group-less run must not publish argv anywhere; got {value!r}"
    )


def test_annotation_only_preserves_echo_destinations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An annotation-only session leaves stdout and stderr echo in place."""
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    command, allowlist = _python_command(
        "import sys; print('child stdout'); print('child stderr', file=sys.stderr); "
        "raise SystemExit(6)"
    )

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = command.run_sync(
            output=RunOutputOptions(echo=True, annotate_failure=True),
        )

    value = capsys.readouterr()
    assert result.exit_code == 6, f"the failing run must report 6; got {result!r}"
    assert value.out == "child stdout\n", (
        f"stdout echo must keep its original destination; got {value.out!r}"
    )
    assert value.err.startswith("child stderr\n"), (
        f"stderr echo must keep its original destination; got {value.err!r}"
    )
    assert "::group::" not in value.err, (
        f"annotation-only output must not open a group; got {value.err!r}"
    )
    assert "::stop-commands::" not in value.err, (
        f"annotation-only output must not open a stop-commands lease; got {value.err!r}"
    )
    assert _annotations(value.err) == [
        f"title={_escape_property(_run_label(command))}::exit_nonzero",
    ], "the workflow annotation remains on the parent's stderr"


def test_flags_annotate_timeout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A timed-out run annotates with the categorical ``timeout`` detail.

    The detail is the category, never the exception text or the command's
    arguments, so a run that fails by running too long reports that fact and
    nothing more.
    """
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    command, allowlist = _python_command(
        "import time; time.sleep(30)",
        "--token=s3cret-token-9f2aXq7",
    )

    with (
        scoped(ScopeConfig(allowlist=allowlist)),
        pytest.raises(sh.TimeoutExpired),
    ):
        command.run_sync(output=_flags_output(annotate_failure=True), timeout=0.5)

    value = capsys.readouterr().err
    assert _annotations(value) == [
        f"title={_escape_property(_run_label(command))}::timeout",
    ], "a timeout must annotate with its categorical detail"
    assert "s3cret-token-9f2aXq7" not in value, (
        f"neither exception text nor argv may reach the annotation; got {value!r}"
    )


def test_flags_annotate_internal_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A run that never spawns annotates with the categorical ``error`` detail."""
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    _, python_program = python_catalogue()
    python_path = Path(python_program)
    absent_path = python_path.with_name(f"{python_path.name}.does-not-exist")
    absent_program = Program(str(absent_path))
    absent = sh.make(
        absent_program,
        catalogue=ProgramCatalogue.from_programs(
            absent_program,
            name="absent-program",
        ),
    )("-c", "pass")

    with (
        scoped(ScopeConfig(allowlist=frozenset([absent_program]))),
        pytest.raises(FileNotFoundError),
    ):
        absent.run_sync(output=_flags_output(annotate_failure=True))

    value = capsys.readouterr().err
    assert _annotations(value) == [
        f"title={_escape_property(_run_label(absent))}::error",
    ], "an internal error reports its category and no exception text"


def test_flags_leave_default_output_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Options carrying the flags switched off are inert, byte for byte.

    The comparison is against the *same* run built with the flags absent
    entirely, so this asserts the acceptance criterion directly rather than
    asserting a shape that merely looks unflagged. ``echo=True`` still mirrors
    to stdout in both cases: the flags are presentation-only, so they must not
    redirect a run's own destinations, let alone its capture.
    """
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    command, allowlist = _python_command("print('inside the group')")

    with scoped(ScopeConfig(allowlist=allowlist)):
        flagged_off = command.run_sync(output=_flags_output())
    defaulted = capsys.readouterr()

    with scoped(ScopeConfig(allowlist=allowlist)):
        unflagged_result = command.run_sync(output=RunOutputOptions(echo=True))
    unflagged = capsys.readouterr()

    assert flagged_off.ok is True, (
        f"the flagged-off run must succeed; got {flagged_off.exit_code}"
    )
    assert unflagged_result.ok is True, (
        f"the unflagged run must succeed; got {unflagged_result.exit_code}"
    )
    # Only the outcome-bearing fields: two runs of one command differ in
    # ``pid``, ``started_at``, and ``duration`` whatever the options say.
    observed = ("program", "argv", "exit_code", "stdout", "stderr")
    assert [getattr(flagged_off, field) for field in observed] == [
        getattr(unflagged_result, field) for field in observed
    ], "the flags switched off must leave the result unchanged"
    assert (defaulted.out, defaulted.err) == (unflagged.out, unflagged.err), (
        f"flags off must write byte-for-byte what an unflagged run writes; "
        f"got {(defaulted.out, defaulted.err)!r} and "
        f"{(unflagged.out, unflagged.err)!r}"
    )
    assert "::" not in defaulted.err, (
        f"flags off must write no workflow commands; got {defaulted.err!r}"
    )


def test_flags_stop_commands_neutralize_child_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A child cannot close the group the flags opened, or spoof an annotation.

    The lease is the reason the flags can claim the framing is injection-safe,
    so it is asserted through the flag path rather than only through a
    hand-built sink.
    """
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    command, allowlist = _python_command(
        "print('::endgroup::'); print('::error title=child::spoofed')",
    )

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = command.run_sync(output=_flags_output(group=True))

    value = capsys.readouterr().err
    assert result.ok is True, f"the framed run must still succeed; got {result!r}"
    assert result.stdout == "::endgroup::\n::error title=child::spoofed\n", (
        f"capture must be unchanged by the lease; got {result.stdout!r}"
    )
    release = f"::{_stop_token(value)}::\n"
    assert value.index("::error title=child::spoofed") < value.index(release), (
        "the child's spoofed annotation must precede the lease release"
    )
    assert value.index("::endgroup::\n") < value.index(release), (
        "the child's spoofed endgroup must precede the lease release"
    )
    assert value.index(release) < value.rindex("::endgroup::\n"), (
        "the session's own endgroup must follow the lease release"
    )


def test_flags_annotation_omits_argv(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The annotation title is the bounded label, however the command is called.

    ``group=True`` and ``annotate_failure=True`` together are the combination a
    CI caller actually wants, and the one where a leaked argument would be
    published beside the very group it was admitted to.
    """
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    secret = "s3cret-token-9f2aXq7"  # ruff: ignore[hardcoded-password-string] - synthetic test token, never a real credential.
    command, allowlist = _python_command("raise SystemExit(2)", secret)

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = command.run_sync(
            output=_flags_output(group=True, annotate_failure=True),
        )

    value = capsys.readouterr().err
    assert result.exit_code == 2, f"the failing run must report 2; got {result!r}"
    assert value.count("::group::") == 1, "the group must open exactly once"
    assert value.count("::endgroup::") == 1, "the group must close exactly once"
    annotation = value.split("::error ", 1)[1]
    assert secret not in annotation, (
        f"the annotation must not republish argv; got {annotation!r}"
    )
    assert annotation == (
        f"title={_escape_property(_run_label(command))}::exit_nonzero\n"
    ), "the annotation names the program and no arguments"


def test_flags_frame_pipeline_as_single_group(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A flagged pipeline emits one group for the whole pipeline.

    The adapter opens one session per run and a pipeline *is* one run, so the
    group count is one and not one per stage — the reading the ExecPlan records
    for the ticket's "one group per command" criterion.
    """
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    producer = python("-c", "print('piped')")
    failing = python("-c", "import sys; sys.stdin.read(); raise SystemExit(4)")

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = (producer | failing).run_sync(
            output=_flags_output(group=True, annotate_failure=True),
        )

    value = capsys.readouterr().err
    codes = [stage.exit_code for stage in result.stages]
    assert codes == [0, 4], f"both stages must report their own exit code; got {codes}"
    assert value.count("::group::") == 1, (
        f"one pipeline opens one group, not one per stage; got {value!r}"
    )
    assert value.startswith("::group::pipeline\n"), (
        f"a pipeline titles its group 'pipeline'; got {value!r}"
    )
    assert value.endswith("::error title=pipeline::exit_nonzero\n"), (
        f"the annotation must report the pipeline's categorical outcome; got {value!r}"
    )


def test_flags_annotate_async_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The async pipeline entry point applies both convenience flags."""
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    producer = python("-c", "print('piped')")
    failing = python("-c", "import sys; sys.stdin.read(); raise SystemExit(7)")

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = asyncio.run(
            (producer | failing).run(
                output=_flags_output(group=True, annotate_failure=True),
            )
        )

    value = capsys.readouterr().err
    assert [stage.exit_code for stage in result.stages] == [0, 7], (
        "the async pipeline must retain its stage outcomes"
    )
    assert value.count("::group::") == 1, "one pipeline opens one group"
    assert value.startswith("::group::pipeline\n"), (
        f"the async pipeline group must use its aggregate label; got {value!r}"
    )
    assert value.endswith("::error title=pipeline::exit_nonzero\n"), (
        f"the async pipeline must annotate the first failing stage; got {value!r}"
    )
