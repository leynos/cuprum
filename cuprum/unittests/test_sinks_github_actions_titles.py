"""Title precedence and argument disclosure tests for the GitHub Actions sink."""

from __future__ import annotations

import typing as typ

import pytest

from cuprum.sinks import GitHubActionsSink, SessionOutcome, TerminalOutcome
from cuprum.unittests.test_sinks_github_actions import _open_gha_session


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(
            ("brew", "install", "cuprum-sentinel-token-0000"),
            id="argument-value",
        ),
        pytest.param(
            ("deploy", "--token", "cuprum-sentinel-token-0000"),
            id="token-shaped-argument",
        ),
    ],
)
def test_group_title_defaults_to_bounded_run_label(argv: tuple[str, ...]) -> None:
    """Default group titles omit ordinary and token-shaped argv values."""
    _session, buffer = _open_gha_session(argv)

    group = buffer.getvalue().splitlines()[0]
    assert group == "::group::project: program", (
        f"the group line must contain only the bounded run label; got {group!r}"
    )
    assert "cuprum-sentinel-token-0000" not in group, (
        f"argv values must not reach the group line; got {group!r}"
    )


def test_failure_annotation_omits_argv() -> None:
    """The default group and annotation titles both omit argv values."""
    secret = "cuprum-sentinel-token-0000"  # ruff: ignore[hardcoded-password-string] - synthetic test value, never a real credential.
    session, buffer = _open_gha_session(("deploy", "--token", secret))

    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=1))

    value = buffer.getvalue()
    group = value.splitlines()[0]
    assert group == "::group::project: program", (
        f"the group title must use the bounded run label; got {group!r}"
    )
    assert secret not in group, (
        f"the default group title must not publish argv; got {group!r}"
    )
    assert value.endswith("::error title=project%3A program::exit_nonzero\n"), (
        f"the annotation must carry the bounded label; got {value!r}"
    )
    annotation = value.split("::error ", 1)[1]
    assert secret not in annotation, (
        f"the annotation must not publish argv; got {annotation!r}"
    )


def test_title_override_wins_over_argv_title() -> None:
    """An explicit safe title wins over argv and labels the annotation."""
    session, buffer = _open_gha_session(
        ("deploy", "--token", "cuprum-sentinel-token-0000"),
        sink_options={"title": "Safe operation", "argv_title": True},
    )

    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=1))

    value = buffer.getvalue()
    assert value.startswith("::group::Safe operation\n"), (
        f"the explicit title must label the group; got {value!r}"
    )
    assert value.endswith("::error title=Safe operation::exit_nonzero\n"), (
        f"the explicit title must label the annotation; got {value!r}"
    )
    assert "cuprum-sentinel-token-0000" not in value, (
        f"the explicit title must take precedence over argv; got {value!r}"
    )


def test_empty_title_keeps_group_empty_and_annotation_bounded() -> None:
    """An empty title stays on the group and falls back for annotation."""
    session, buffer = _open_gha_session(
        ("deploy", "--token", "secret"),
        sink_options={"title": "", "argv_title": True},
    )

    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=1))

    lines = buffer.getvalue().splitlines()
    assert lines[0] == "::group::", f"the explicit group title stays empty: {lines!r}"
    assert lines[-1] == "::error title=project%3A program::exit_nonzero", (
        f"the empty title falls back to the bounded annotation label: {lines!r}"
    )


def test_argv_title_opt_in_joins_arguments_and_keeps_annotation_bounded() -> None:
    """Explicit argv titles join argv while annotations keep the run label."""
    token = "cuprum-sentinel-token-0000"  # ruff: ignore[hardcoded-password-string] - synthetic regression value, never a real credential.
    session, buffer = _open_gha_session(
        ("deploy", "--token", token),
        sink_options={"argv_title": True},
    )

    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=1))

    value = buffer.getvalue()
    assert value.startswith(f"::group::deploy --token {token}\n"), (
        f"argv_title=True must use a plain space join; got {value!r}"
    )
    assert value.endswith("::error title=project%3A program::exit_nonzero\n"), (
        f"the annotation must retain the bounded label; got {value!r}"
    )


def test_explicit_title_preserves_literal_group_delimiters() -> None:
    """Delimiters in a caller title stay literal group data."""
    _session, buffer = _open_gha_session(
        (),
        sink_options={"title": "echo: a,b"},
    )

    assert "::group::echo: a,b\n" in buffer.getvalue(), (
        f"':' and ',' remain literal in a group data segment; got {buffer.getvalue()!r}"
    )


@pytest.mark.parametrize("invalid", [1, 0, "yes", None, 1.0])
def test_argv_title_rejects_non_bool_values(invalid: object) -> None:
    """The argv-title policy accepts only literal booleans."""
    with pytest.raises(TypeError, match="argv_title must be a bool"):
        GitHubActionsSink(argv_title=typ.cast("bool", invalid))
