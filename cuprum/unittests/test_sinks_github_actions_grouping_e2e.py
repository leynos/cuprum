"""Real-child regression tests for GitHub Actions group-title disclosure."""

from __future__ import annotations

import pytest

from cuprum import ScopeConfig, scoped
from cuprum._sink_lifecycle import _run_label
from cuprum.redaction import RedactionPolicy
from cuprum.sh import RunOutputOptions
from cuprum.sinks.github_actions import _escape_property
from cuprum.unittests.test_sinks_end_to_end import (
    _ON_CI,
    _assert_framed,
    _flags_output,
    _forced_sink,
    _python_command,
)


@pytest.mark.parametrize("capture", [True, False], ids=["capture-on", "capture-off"])
def test_group_flag_omits_token_argument_without_echo(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    capture: bool,
) -> None:
    """The convenience flag never logs argv, even without echo or capture."""
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    token = "cuprum-sentinel-token-0000"  # ruff: ignore[hardcoded-password-string] - synthetic regression value, never a real credential.
    command, allowlist = _python_command("print('hidden by echo=False')", token)

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = command.run_sync(
            output=_flags_output(group=True, capture=capture, echo=False),
        )

    rendered = capsys.readouterr().err
    group = rendered.splitlines()[0]
    assert result.ok is True, f"the grouped command must succeed; got {result!r}"
    assert group == f"::group::{_run_label(command)}", (
        f"group=True must use the bounded label; got {group!r}"
    )
    assert token not in group, f"the token must not enter the group line; got {group!r}"
    assert token not in rendered, (
        f"the token must not enter workflow output; got {rendered!r}"
    )
    assert "hidden by echo=False" not in rendered, (
        f"echo=False must keep child output out of the workflow log; got {rendered!r}"
    )


def test_group_and_annotation_flags_omit_argv_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default group and error lines both omit a synthetic argv secret."""
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    token = "cuprum-sentinel-token-0000"  # ruff: ignore[hardcoded-password-string] - synthetic regression value, never a real credential.
    command, allowlist = _python_command("raise SystemExit(2)", token)

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = command.run_sync(
            output=_flags_output(group=True, annotate_failure=True, echo=False),
        )

    rendered = capsys.readouterr().err
    group = next(line for line in rendered.splitlines() if line.startswith("::group::"))
    annotation = next(
        line for line in rendered.splitlines() if line.startswith("::error ")
    )
    assert result.exit_code == 2, (
        f"the child must fail with exit code 2; got {result!r}"
    )
    assert group == f"::group::{_run_label(command)}", (
        f"the group line must use the bounded label; got {group!r}"
    )
    assert token not in group, f"argv must not reach the group line; got {group!r}"
    assert token not in annotation, (
        f"argv must not reach the annotation; got {annotation!r}"
    )
    assert annotation == (
        f"::error title={_escape_property(_run_label(command))}::exit_nonzero"
    ), "the annotation names the bounded run label"


def test_forced_sink_redacts_opt_in_argv_title_without_changing_run() -> None:
    """Explicit argv titles are sanitized while command execution stays raw."""
    sentinel = "gha-e2e-secret-442"
    command, allowlist = _python_command(
        "import sys; print(sys.argv[-1])",
        sentinel,
    )
    sink, buffer = _forced_sink(
        argv_title=True,
        redaction=RedactionPolicy(literals=(sentinel,)),
    )

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = command.run_sync(output=RunOutputOptions(echo=True, sink=sink))

    rendered = buffer.getvalue()
    assert result.stdout == f"{sentinel}\n", "captured output must remain unchanged"
    assert command.argv[-1] == sentinel, "execution arguments must remain unchanged"
    expected_title = " ".join(command.argv_with_program).replace(sentinel, "[REDACTED]")
    assert rendered.startswith(f"::group::{expected_title}\n"), (
        "the emitted group title must redact its configured sentinel"
    )
    assert sentinel not in rendered.partition("\n")[0], (
        "the title must exclude configured secrets"
    )
    assert sentinel in rendered, "mirrored child output must remain unchanged"
    _assert_framed(
        rendered,
        command,
        child_output=sentinel,
        expected_group_title=expected_title,
    )


def test_failing_run_annotates_without_argv() -> None:
    """A real failing run excludes argv from both group and annotation."""
    secret = "cuprum-sentinel-token-0000"  # ruff: ignore[hardcoded-password-string] - synthetic test value, never a real credential.
    command, allowlist = _python_command("raise SystemExit(3)", secret)
    sink, buffer = _forced_sink()

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = command.run_sync(output=RunOutputOptions(echo=True, sink=sink))

    rendered = buffer.getvalue()
    assert result.exit_code == 3, f"the failing run must report 3; got {result!r}"
    group = rendered.splitlines()[0]
    annotation = rendered.split("::error ", 1)[1]
    assert group == f"::group::{_run_label(command)}", (
        f"the default group title must use the bounded label; got {group!r}"
    )
    assert secret not in group, f"argv must not reach the group line; got {group!r}"
    assert secret not in annotation, (
        f"argv must not reach the annotation; got {annotation!r}"
    )
    assert secret not in rendered, (
        f"default workflow output must omit argv; got {rendered!r}"
    )
    assert annotation == (
        f"title={_escape_property(_run_label(command))}::exit_nonzero\n"
    ), "the annotation names the bounded run label and no arguments"


def test_flags_frame_successful_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``group=True`` frames a real command with its bounded run label."""
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    command, allowlist = _python_command("print('inside the group')")

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = command.run_sync(output=_flags_output(group=True))

    assert result.ok is True, f"the framed run must still succeed; got {result!r}"
    assert result.stdout == "inside the group\n", "capture must be unchanged"
    _assert_framed(capsys.readouterr().err, command)


def test_explicit_safe_title_appears_in_real_group_header() -> None:
    """A caller-provided safe title still labels an actual command run."""
    command, allowlist = _python_command("print('inside the group')")
    sink, buffer = _forced_sink(title="Safe operation")

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = command.run_sync(output=RunOutputOptions(echo=False, sink=sink))

    assert result.ok is True, f"the titled command must succeed; got {result!r}"
    assert buffer.getvalue().startswith("::group::Safe operation\n"), (
        "the explicit safe title must appear in the group line; "
        f"got {buffer.getvalue()!r}"
    )


def test_argv_title_opt_in_appears_in_real_group_header() -> None:
    """A caller can deliberately put the plain-joined argv in the job log."""
    command, allowlist = _python_command("print('inside the group')", "--mode", "safe")
    sink, buffer = _forced_sink(argv_title=True)

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = command.run_sync(output=RunOutputOptions(echo=False, sink=sink))

    expected_title = " ".join(command.argv_with_program)
    assert result.ok is True, f"the argv-titled command must succeed; got {result!r}"
    assert buffer.getvalue().startswith(f"::group::{expected_title}\n"), (
        f"argv_title=True must publish the plain-joined argv; got {buffer.getvalue()!r}"
    )
