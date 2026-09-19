"""Tests for the GitHub Actions presentation sink.

The adapter's contract is a sequence of workflow commands: an escaped group
title, the stop-commands lease, framed output, the lease release, the endgroup,
and — for a failed run — one bounded ``::error::`` annotation. A group title is
a *data* segment while an annotation title is a *property*, so the two are
escaped differently and carry different labels.
"""

from __future__ import annotations

import io
import typing as typ

import pytest

from cuprum.sh import RunOutputOptions
from cuprum.sinks import (
    GitHubActionsSink,
    SessionOutcome,
    SessionStart,
    TerminalOutcome,
)
from cuprum.sinks.github_actions import (
    GitHubActionsSession,
    _escape_data,
    _escape_property,
    _new_stop_token,
)
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd


def _python_builder() -> cabc.Callable[..., SafeCmd]:
    """Build a SafeCmd factory for the current interpreter."""
    return build_python_builder()


# ---------------------------------------------------------------------------
# Escaping and tokens
# ---------------------------------------------------------------------------


def test_escape_data_masks_percent_and_newlines() -> None:
    """Data-position escaping covers the runner's three reserved sequences."""
    assert _escape_data("100%") == "100%25"
    assert _escape_data("a\r\nb") == "a%0D%0Ab"


def test_escape_property_additionally_masks_delimiters() -> None:
    """Property-position escaping also covers ``:`` and ``,``."""
    assert _escape_property("a:b,c") == "a%3Ab%2Cc"
    assert _escape_property("50%\n") == "50%25%0A"


def test_stop_tokens_are_unique_and_hex() -> None:
    """Consecutive tokens differ and are lowercase hex of the right length."""
    first = _new_stop_token()
    second = _new_stop_token()
    assert first != second
    assert len(first) == 16
    int(first, 16)


# ---------------------------------------------------------------------------
# Session construction
# ---------------------------------------------------------------------------


def _gha_sink(*, force: bool = False) -> tuple[GitHubActionsSink, io.StringIO]:
    """Build a GitHub Actions sink over an in-memory buffer."""
    buffer = io.StringIO()
    sink = GitHubActionsSink(typ.cast("typ.IO[str]", buffer), force=force)
    return sink, buffer


def _open_gha_session(
    argv: tuple[str, ...],
    *,
    force: bool = True,
) -> tuple[GitHubActionsSession, io.StringIO]:
    """Open one forced-active adapter session and return it with its buffer."""
    buffer = io.StringIO()
    sink = GitHubActionsSink(typ.cast("typ.IO[str]", buffer), force=force)
    session = sink.open_session(SessionStart(label="project: program", argv=argv))
    assert session is not None, "a forced sink must return an active session"
    return session, buffer


# ---------------------------------------------------------------------------
# Environment-gated activation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("github_actions", "force", "expected_active"),
    [
        pytest.param(None, False, False, id="unset-declines"),
        pytest.param("true", False, True, id="true-activates"),
        pytest.param(None, True, True, id="force-activates-without-env"),
        pytest.param("1", False, False, id="one-does-not-activate"),
        pytest.param("TRUE", False, False, id="upper-true-does-not-activate"),
        pytest.param("True", False, False, id="title-true-does-not-activate"),
        pytest.param("false", False, False, id="false-does-not-activate"),
        pytest.param("", False, False, id="empty-does-not-activate"),
    ],
)
def test_sink_activation_follows_environment_and_force(
    monkeypatch: pytest.MonkeyPatch,
    github_actions: str | None,
    force: bool,
    *,
    expected_active: bool,
) -> None:
    """Only an exact ``true`` or an explicit ``force`` activates the sink.

    Every other value — including the runner-adjacent spellings ``1``,
    ``TRUE``, and ``True`` — leaves the sink inactive and silent, so runs keep
    their plain parent-facing output.
    """
    if github_actions is None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    else:
        monkeypatch.setenv("GITHUB_ACTIONS", github_actions)
    sink, buffer = _gha_sink(force=force)

    session = sink.open_session(SessionStart(label="project: program", argv=("hi",)))

    if not expected_active:
        assert session is None, "a non-enabling configuration must decline"
        assert buffer.getvalue() == "", "an inactive sink must write nothing"
        return
    assert session is not None, "an enabling configuration must return a session"
    assert buffer.getvalue() == (
        f"::group::hi\n::stop-commands::{session.stop_token}\n"
    )


def test_inactive_sink_leaves_runner_output_unframed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An inactive sink keeps echoed output unframed and results intact."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    command = _python_builder()("-c", "print('unframed')")

    result = command.run_sync(
        output=RunOutputOptions(echo=True, sink=_gha_sink()[0]),
    )

    captured = capsys.readouterr()
    assert result.ok is True
    assert result.stdout == "unframed\n"
    assert "::group::" not in captured.out
    assert "::group::" not in captured.err
    assert "::stop-commands::" not in captured.err
    assert "::endgroup::" not in captured.err
    assert "::error" not in captured.err
    assert captured.out.strip() == "unframed"


# ---------------------------------------------------------------------------
# Framing lifecycle
# ---------------------------------------------------------------------------


def test_session_frames_group_then_lease() -> None:
    """Opening a session writes the titled group and then the lease."""
    session, buffer = _open_gha_session(("echo", "hi"))

    assert buffer.getvalue() == (
        f"::group::echo hi\n::stop-commands::{session.stop_token}\n"
    )


def test_session_frames_the_group_exactly_once() -> None:
    """One session opens one group, however often it is closed."""
    session, buffer = _open_gha_session(("echo", "hi"))

    session.close(SessionOutcome(TerminalOutcome.EXIT_ZERO, exit_code=0))
    session.close(SessionOutcome(TerminalOutcome.EXIT_ZERO, exit_code=0))

    assert buffer.getvalue().count("::group::") == 1


def test_successful_close_releases_lease_without_annotation() -> None:
    """A zero exit releases the lease, closes the group, and stays silent."""
    session, buffer = _open_gha_session(("echo", "hi"))
    token = session.stop_token
    buffer.seek(0)
    buffer.truncate()

    session.close(SessionOutcome(TerminalOutcome.EXIT_ZERO, exit_code=0))

    assert buffer.getvalue() == (f"::{token}::\n::endgroup::\n")


def test_nonzero_close_emits_error_annotation() -> None:
    """A non-zero exit adds one error annotation after the lease release."""
    session, buffer = _open_gha_session(("false",))
    buffer.seek(0)
    buffer.truncate()

    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=3))

    value = buffer.getvalue()
    assert value.startswith(f"::{session.stop_token}::")
    assert "::endgroup::\n" in value
    assert value.count("::error ") == 1
    assert value.endswith("::error title=project%3A program::exit_nonzero\n")


def test_timeout_close_annotates_without_exit_code() -> None:
    """A timeout emits the categorical detail, never a synthesized code."""
    session, buffer = _open_gha_session(("sleeper",))
    buffer.seek(0)
    buffer.truncate()

    session.close(
        SessionOutcome(TerminalOutcome.TIMEOUT, exit_code=None, detail="timeout"),
    )

    assert buffer.getvalue().endswith("::error title=project%3A program::timeout\n")


def test_failure_annotation_omits_argv() -> None:
    """A group titled with argv must not republish those arguments.

    The annotation title is a workflow-command property, so reusing the argv
    group title would copy arguments — including secrets — into the run's
    summary.
    """
    secret = "s3cret-token-9f2aXq7"  # ruff: ignore[hardcoded-password-string] - synthetic test token, never a real credential.
    session, buffer = _open_gha_session(("deploy", "--token", secret))

    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=1))

    value = buffer.getvalue()
    assert f"::group::deploy --token {secret}\n" in value
    assert value.endswith("::error title=project%3A program::exit_nonzero\n")
    assert secret not in value.split("::error ", 1)[1]


def test_title_override_labels_group_and_annotation() -> None:
    """An explicit title replaces the argv group title and the annotation title."""
    buffer = io.StringIO()
    sink = GitHubActionsSink(
        typ.cast("typ.IO[str]", buffer),
        title="Deploy",
        force=True,
    )
    session = sink.open_session(
        SessionStart(label="project: program", argv=("deploy", "--token")),
    )
    assert session is not None, "a forced sink must return an active session"

    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=1))

    value = buffer.getvalue()
    assert value.startswith("::group::Deploy\n")
    assert value.endswith("::error title=Deploy::exit_nonzero\n")


def test_close_is_idempotent() -> None:
    """A second close performs no further writes."""
    session, buffer = _open_gha_session(("echo", "hi"))
    buffer.seek(0)
    buffer.truncate()
    session.close(SessionOutcome(TerminalOutcome.EXIT_ZERO, exit_code=0))
    first = buffer.getvalue()
    session.close(SessionOutcome(TerminalOutcome.EXIT_ZERO, exit_code=0))

    assert buffer.getvalue() == first


def test_group_title_uses_program_args() -> None:
    """The group title is the joined program args, escaped as command data."""
    _session, buffer = _open_gha_session(("brew", "install", "wget"))

    assert "::group::brew install wget\n" in buffer.getvalue()


def test_group_title_escapes_delimiters_as_data() -> None:
    """Delimiters stay literal in a group title: the title is a data segment.

    Property escaping would rewrite ``:`` and ``,`` to ``%3A`` and ``%2C``,
    which the runner does not decode in the data position, so a default
    ``"<project>: <program>"`` label would surface mangled.
    """
    _session, buffer = _open_gha_session(("echo: a,b",))

    assert "::group::echo: a,b\n" in buffer.getvalue()
