"""Snapshots of the GitHub Actions sink's emitted title commands."""

from __future__ import annotations

import io
import typing as typ

from cuprum.sinks import (
    GitHubActionsSink,
    SessionOutcome,
    SessionStart,
    TerminalOutcome,
)

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

    from cuprum.sinks._github_actions_options import _GitHubActionsSinkOptionsKwargs


def _title_commands(
    label: str,
    argv: tuple[str, ...],
    *,
    sink_options: _GitHubActionsSinkOptionsKwargs | None = None,
    should_fail: bool = False,
) -> list[str]:
    """Return only stable group and error commands from one sink session."""
    buffer = io.StringIO()
    options: _GitHubActionsSinkOptionsKwargs = {"force": True}
    if sink_options is not None:
        options.update(sink_options)

    sink = GitHubActionsSink(buffer, **options)
    session = sink.open_session(SessionStart(label=label, argv=argv))
    assert session is not None, "the forced sink must return an active session"
    outcome = TerminalOutcome.EXIT_NONZERO if should_fail else TerminalOutcome.EXIT_ZERO
    exit_code = 1 if should_fail else 0
    session.close(SessionOutcome(outcome, exit_code=exit_code))

    return [
        line
        for line in buffer.getvalue().splitlines()
        if line.startswith(("::group::", "::error "))
    ]


def test_title_command_variants_match_snapshot(snapshot: SnapshotAssertion) -> None:
    """Snapshot stable workflow title lines across sink configuration variants."""
    secret = "cuprum-sentinel-token-0000"  # ruff: ignore[hardcoded-password-string] - synthetic regression value, never a real credential.
    projections = {
        "default": _title_commands(
            "project: program",
            ("deploy", "--token", secret),
            should_fail=True,
        ),
        "explicit_title": _title_commands(
            "project: program",
            ("deploy", "--token", secret),
            sink_options={"title": "Safe operation", "argv_title": True},
            should_fail=True,
        ),
        "argv_title": _title_commands(
            "project: program",
            ("deploy", "--mode", "safe"),
            sink_options={"argv_title": True},
            should_fail=True,
        ),
        "pipeline": _title_commands("pipeline", ("deploy",), should_fail=True),
        "annotation_only": _title_commands(
            "project: program",
            ("deploy",),
            sink_options={"emit_group": False},
            should_fail=True,
        ),
        "group_only": _title_commands(
            "project: program",
            ("deploy",),
            sink_options={"emit_annotation": False},
            should_fail=True,
        ),
    }

    assert projections == snapshot, (
        "the workflow title commands should retain their reviewed wire shapes"
    )
    assert projections["default"] == [
        "::group::project: program",
        "::error title=project%3A program::exit_nonzero",
    ], "the default group and failure annotation use the bounded label"
    assert "cuprum-sentinel-token-0000" not in str(projections), (
        "default and explicit safe titles must not publish the argv token"
    )
