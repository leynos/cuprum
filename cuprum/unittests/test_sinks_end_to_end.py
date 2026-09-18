"""End-to-end tests for an active presentation sink on a real run.

The adapter's own tests pin the sequence of workflow commands it emits; these
tests pin the integration around it. A real subprocess driven through the
public ``run_sync`` command and pipeline APIs, with a forced
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
    assert group == f"::group::{' '.join(command.argv_with_program)}"
    assert lease == f"{_LEASE_PREFIX}{token}", "the lease is opened with its token"
    assert child == "inside the group", "mirrored output lands inside the framing"
    assert release == f"::{token}::", "the lease closes on the token alone"
    assert endgroup == "::endgroup::", "the group closes after the lease release"


def test_forced_sink_frames_real_run_and_routes_echoed_output() -> None:
    """A forced sink brackets one real run's mirrored output, result intact."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    command = python("-c", "print('inside the group')")
    sink, buffer = _forced_sink()

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = command.run_sync(output=RunOutputOptions(echo=True, sink=sink))

    assert result.ok is True
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
    assert result.ok is True
    assert result.stdout == "::endgroup::\n::error title=child::spoofed\n"
    release = f"::{_stop_token(value)}::\n"
    # Every command the child wrote precedes the release, so the runner ignores
    # it; the endgroup the runner acts on is the session's own, written after.
    assert value.index("::error title=child::spoofed") < value.index(release)
    assert value.index("::endgroup::\n") < value.index(release)
    assert value.index(release) < value.rindex("::endgroup::\n")


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
    assert result.exit_code == 3
    # The group is titled with argv, so the annotation has to be the one place
    # the arguments do not appear.
    assert secret in value
    assert value.count("::error ") == 1
    annotation = value.split("::error ", 1)[1]
    assert secret not in annotation
    assert annotation == (
        f"title={_escape_property(_run_label(command, None))}::exit_nonzero\n"
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
    assert [stage.exit_code for stage in result.stages] == [0, 4]
    assert result.failure_index == 1
    assert value.count("::group::") == 1, "one pipeline opens one group"
    assert value.startswith("::group::pipeline\n")
    assert value.endswith("::error title=pipeline::exit_nonzero\n")
