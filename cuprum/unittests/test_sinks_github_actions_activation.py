"""Environment activation tests for the GitHub Actions presentation sink."""

from __future__ import annotations

import pytest

from cuprum.sh import RunOutputOptions
from cuprum.sinks import SessionStart
from cuprum.unittests.test_sinks_github_actions import (
    _gha_sink,
    _python_builder,
)

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
        assert not buffer.getvalue(), "an inactive sink must write nothing"
        return
    assert session is not None, "an enabling configuration must return a session"
    written = buffer.getvalue()
    assert written == (
        f"::group::project: program\n::stop-commands::{session.stop_token}\n"
    ), f"activation must write the bounded label then its lease; got {written!r}"


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
    assert result.ok is True, f"the run must still succeed; got {result!r}"
    assert result.stdout == "unframed\n", (
        f"capture must be unchanged by an inactive sink; got {result.stdout!r}"
    )
    assert "::group::" not in captured.out, (
        f"an inactive sink must not open a group on stdout; got {captured.out!r}"
    )
    assert "::group::" not in captured.err, (
        f"an inactive sink must not open a group on stderr; got {captured.err!r}"
    )
    assert "::stop-commands::" not in captured.err, (
        f"an inactive sink must not take the lease; got {captured.err!r}"
    )
    assert "::endgroup::" not in captured.err, (
        f"an inactive sink must not close a group; got {captured.err!r}"
    )
    assert "::error" not in captured.err, (
        f"an inactive sink must not annotate; got {captured.err!r}"
    )
    assert captured.out.strip() == "unframed", (
        f"echoed output must stay unframed; got {captured.out!r}"
    )
