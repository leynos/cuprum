"""Tests for presentation sinks (tee destinations and output adapters)."""

from __future__ import annotations

import errno
import io
import os
import threading
import typing as typ
from unittest import mock

import pytest

from benchmarks import PtyBlackholeStateError, sinks
from cuprum.sh import RunOutputOptions, TimeoutExpired
from cuprum.sinks import (
    GitHubActionsSink,
    OutputSession,
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
# Output protocol conformance helpers
# ---------------------------------------------------------------------------


class _RecordingSession:
    """Minimal OutputSession recording writes and the close outcome."""

    def __init__(self) -> None:
        """Start with an in-memory log and no closed outcome."""
        self.log_io = io.StringIO()
        self.closed_with: SessionOutcome | None = None

    @property
    def log(self) -> typ.IO[str]:
        """The in-memory log destination."""
        return self.log_io

    def close(self, outcome: SessionOutcome) -> None:
        """Record the terminal outcome."""
        self.closed_with = outcome


class _RecordingSink:
    """Minimal OutputSink returning a recording session."""

    title: str | None

    def __init__(self, *, decline: bool = False) -> None:
        """Configure whether the adapter declines activation."""
        self.decline = decline
        self.title = None
        self.started_with: SessionStart | None = None
        self.opened = 0
        self.last_session: _RecordingSession | None = None

    def open_session(self, start: SessionStart) -> OutputSession | None:
        """Record the start and return a fresh recording session."""
        self.started_with = start
        self.opened += 1
        if self.decline:
            return None
        self.last_session = _RecordingSession()
        return self.last_session


# ---------------------------------------------------------------------------
# Run-level sink session lifecycle
# ---------------------------------------------------------------------------


def test_run_output_options_sink_defaults_to_none() -> None:
    """RunOutputOptions leaves the sink unset so runs stay unchanged by default."""
    options = RunOutputOptions()

    assert options.sink is None


def test_no_sink_keeps_plain_destinations() -> None:
    """A run without a sink captures output exactly as before."""
    command = _python_builder()("-c", "print('plain')")

    result = command.run_sync()

    assert result.ok is True
    assert result.stdout == "plain\n"


def test_sink_declining_activation_is_a_no_op() -> None:
    """A sink that returns None from open_session leaves the run unchanged."""
    adapter = _RecordingSink(decline=True)
    command = _python_builder()("-c", "print('declined')")

    result = command.run_sync(output=RunOutputOptions(sink=adapter))

    assert result.ok is True
    assert result.stdout == "declined\n"
    assert adapter.opened == 1
    assert adapter.started_with is not None
    assert adapter.started_with.label.endswith("python")


def test_sink_session_opens_once_and_closes_on_success() -> None:
    """The sink session is opened once per run and closed with exit_zero."""
    adapter = _RecordingSink()
    command = _python_builder()("-c", "print('framed')")

    result = command.run_sync(output=RunOutputOptions(sink=adapter))

    assert result.ok is True
    assert adapter.opened == 1
    assert adapter.started_with is not None
    session = adapter.last_session
    assert session is not None
    assert session.closed_with is not None
    assert session.closed_with.outcome == TerminalOutcome.EXIT_ZERO
    assert session.closed_with.exit_code == 0


def test_sink_session_closes_on_nonzero_exit() -> None:
    """A failing command still closes its session with exit_nonzero."""
    adapter = _RecordingSink()
    command = _python_builder()("-c", "raise SystemExit(3)")

    result = command.run_sync(output=RunOutputOptions(sink=adapter))

    assert result.ok is False
    assert result.exit_code == 3
    session = adapter.last_session
    assert session is not None
    assert session.closed_with is not None
    assert session.closed_with.outcome == TerminalOutcome.EXIT_NONZERO
    assert session.closed_with.exit_code == 3


def test_sink_session_closes_on_timeout() -> None:
    """A timed-out command still closes its session with the timeout outcome."""
    adapter = _RecordingSink()
    command = _python_builder()("-c", "import time; time.sleep(2)")

    with pytest.raises(TimeoutExpired, match=r"timed out"):
        command.run_sync(
            output=RunOutputOptions(sink=adapter),
            timeout=0.1,
        )

    session = adapter.last_session
    assert session is not None
    assert session.closed_with is not None
    assert session.closed_with.outcome == TerminalOutcome.TIMEOUT
    assert session.closed_with.exit_code is None


def test_sink_session_label_prefers_title() -> None:
    """An adapter's title attribute overrides the catalogue-derived label."""
    adapter = _RecordingSink()
    adapter.title = "Custom title"
    command = _python_builder()("-c", "print('titled')")

    result = command.run_sync(output=RunOutputOptions(sink=adapter))

    assert result.ok is True
    assert adapter.started_with is not None
    assert adapter.started_with.label == "Custom title"


def test_sink_session_label_omits_argv() -> None:
    """The derived label never contains arguments, only the program name."""
    adapter = _RecordingSink()
    secret = "s3cret-token-9f2aXq7"  # ruff: ignore[hardcoded-password-string] - synthetic test token, never a real credential.
    command = _python_builder()("-c", f"print('{secret}')")

    command.run_sync(output=RunOutputOptions(sink=adapter))

    assert adapter.started_with is not None
    assert secret not in adapter.started_with.label


# ---------------------------------------------------------------------------
# GitHub Actions adapter: escaping and tokens
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
# GitHub Actions adapter: framing lifecycle
# ---------------------------------------------------------------------------


def _gha_sink(*, force: bool = False) -> tuple[GitHubActionsSink, io.StringIO]:
    """Build a GitHub Actions sink over an in-memory buffer."""
    buffer = io.StringIO()
    sink = GitHubActionsSink(typ.cast("typ.IO[str]", buffer), force=force)
    return sink, buffer


def _open_session(argv: tuple[str, ...]) -> tuple[GitHubActionsSink, io.StringIO]:
    """Open one adapter session over a fresh in-memory buffer."""
    buffer = io.StringIO()
    sink = GitHubActionsSink(typ.cast("typ.IO[str]", buffer))
    sink.open_session(
        SessionStart(label="project: program", argv=argv),
    )
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
# GitHub Actions adapter: environment-gated activation
# ---------------------------------------------------------------------------


def test_unsetting_github_actions_keeps_sink_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside Actions, a non-forced sink declines and writes nothing."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    sink, buffer = _gha_sink()

    session = sink.open_session(SessionStart(label="project: program", argv=("cmd",)))

    assert session is None
    assert buffer.getvalue() == ""


def test_github_actions_true_activates_the_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GITHUB_ACTIONS=true activates the sink without force."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    sink, buffer = _gha_sink()

    session = sink.open_session(SessionStart(label="project: program", argv=("hi",)))

    assert session is not None
    session.open_group()
    token = session.stop_token
    assert buffer.getvalue() == f"::group::hi\n::stop-commands::{token}\n"


def test_force_activates_the_sink_outside_github_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """force=True activates the sink even with GITHUB_ACTIONS unset."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    sink, buffer = _gha_sink(force=True)

    session = sink.open_session(SessionStart(label="project: program", argv=("hi",)))

    assert session is not None
    session.open_group()
    token = session.stop_token
    assert buffer.getvalue() == f"::group::hi\n::stop-commands::{token}\n"


@pytest.mark.parametrize("value", ["1", "TRUE", "True", "false", ""])
def test_non_enabling_environment_values_keep_sink_inactive(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """Only the runner's exact 'true' value activates; others decline."""
    monkeypatch.setenv("GITHUB_ACTIONS", value)
    sink, buffer = _gha_sink()

    session = sink.open_session(SessionStart(label="project: program", argv=("cmd",)))

    assert session is None
    assert buffer.getvalue() == ""


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


def test_group_opens_with_title_then_lease() -> None:
    """open_group emits the titled group then the stop-commands bracket."""
    session, buffer = _open_gha_session(("echo", "hi"))
    token = session.stop_token

    session.open_group()

    assert buffer.getvalue() == (f"::group::echo hi\n::stop-commands::{token}\n")


def test_open_group_is_idempotent_while_open() -> None:
    """A second open_group call does not nest another group."""
    session, buffer = _open_gha_session(("echo", "hi"))

    session.open_group()
    session.open_group()

    assert buffer.getvalue().count("::group::") == 1


def test_successful_close_releases_lease_without_annotation() -> None:
    """A zero exit releases the lease, closes the group, and stays silent."""
    session, buffer = _open_gha_session(("echo", "hi"))
    token = session.stop_token

    session.open_group()
    buffer.seek(0)
    buffer.truncate()
    session.close(SessionOutcome(TerminalOutcome.EXIT_ZERO, exit_code=0))

    assert buffer.getvalue() == (f"::stop-commands::{token}\n::endgroup::\n")


def test_nonzero_close_emits_error_annotation() -> None:
    """A non-zero exit adds one error annotation after the lease release."""
    session, buffer = _open_gha_session(("false",))

    session.open_group()
    buffer.seek(0)
    buffer.truncate()
    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=3))

    value = buffer.getvalue()
    assert value.startswith("::stop-commands::")
    assert "::endgroup::\n" in value
    assert value.count("::error ") == 1
    assert value.endswith("::error title=false::exit_nonzero\n")


def test_timeout_close_annotates_without_exit_code() -> None:
    """A timeout emits the categorical detail, never a synthesized code."""
    session, buffer = _open_gha_session(("sleeper",))

    session.open_group()
    buffer.seek(0)
    buffer.truncate()
    session.close(
        SessionOutcome(TerminalOutcome.TIMEOUT, exit_code=None, detail="timeout"),
    )

    assert buffer.getvalue().endswith("::error title=sleeper::timeout\n")


def test_close_is_idempotent() -> None:
    """A second close performs no further writes."""
    session, buffer = _open_gha_session(("echo", "hi"))

    session.open_group()
    buffer.seek(0)
    buffer.truncate()
    session.close(SessionOutcome(TerminalOutcome.EXIT_ZERO, exit_code=0))
    first = buffer.getvalue()
    session.close(SessionOutcome(TerminalOutcome.EXIT_ZERO, exit_code=0))

    assert buffer.getvalue() == first


def test_group_title_uses_program_args() -> None:
    """The group title is the joined program args, escaped for properties."""
    session, buffer = _open_gha_session(("brew", "install", "wget"))

    session.open_group()

    assert "::group::brew install wget\n" in buffer.getvalue()


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_enter_cleans_up_when_fdopen_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PTY sink setup closes open file descriptors if fdopen fails."""
    master_fd, slave_fd = os.openpty()
    monkeypatch.setattr(sinks.pty, "openpty", lambda: (master_fd, slave_fd))

    def fail_fdopen(*_args: object, **_kwargs: object) -> typ.NoReturn:
        """Raise RuntimeError to simulate os.fdopen failing."""
        msg = "fdopen failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(sinks.os, "fdopen", fail_fdopen)
    blackhole = sinks.PtyBlackhole(encoding="utf-8", errors="replace")

    with pytest.raises(RuntimeError, match="fdopen failed"):
        blackhole.__enter__()

    def fstat_error(fd: int) -> OSError:
        """Return the OSError raised when fstat is called on a closed fd."""
        try:
            os.fstat(fd)
        except OSError as exc:
            return exc
        pytest.fail(f"expected closed fd {fd} to raise OSError")

    for fd in (master_fd, slave_fd):
        exc = fstat_error(fd)
        assert exc.errno == errno.EBADF, (
            f"expected EBADF for closed fd {fd}, got {exc.errno}"
        )


# ---------------------------------------------------------------------------
# TextBlackhole
# ---------------------------------------------------------------------------


def test_text_blackhole_is_writable() -> None:
    """TextBlackhole reports itself as writable."""
    bh = sinks.TextBlackhole()
    assert bh.writable() is True


def test_text_blackhole_write_returns_char_count() -> None:
    """TextBlackhole.write returns the length of the string written."""
    bh = sinks.TextBlackhole()
    assert bh.write("hello") == 5
    assert bh.write("") == 0
    assert bh.write("x" * 1000) == 1000


def test_text_blackhole_write_rejects_non_str() -> None:
    """TextBlackhole.write raises TypeError for non-str input."""
    bh = sinks.TextBlackhole()
    # The cast documents the deliberately wrong-typed argument under test.
    with pytest.raises(TypeError):
        bh.write(typ.cast("str", b"bytes"))


# ---------------------------------------------------------------------------
# PtyBlackhole happy path
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_enter_returns_writable_stream() -> None:
    """PtyBlackhole.__enter__ returns a writable text IO stream."""
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    with bh as stream:
        assert stream.writable()


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_drains_written_bytes() -> None:
    """Data written to the PtyBlackhole slave FD is consumed by the drainer."""
    # Deliberately no newline: the PTY line discipline expands "\n" to "\r\n"
    # on write, which would make the drained byte count platform-dependent.
    payload = "héllo from tėst"
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    with bh as stream:
        stream.write(payload)
        stream.flush()
    # __exit__ joins the drainer thread, so drained_bytes is safe to read here.
    assert bh.drained_bytes == len(payload.encode("utf-8")), (
        "the drainer must publish exactly the UTF-8 byte count after exit"
    )


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_resets_the_count_when_reused() -> None:
    """A completed PtyBlackhole context can count a subsequent drain."""
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    first_payload = "first"
    with bh as stream:
        stream.write(first_payload)
        stream.flush()
    assert bh.drained_bytes == len(first_payload.encode("utf-8")), (
        "the first completed context must publish its UTF-8 byte count"
    )

    second_payload = "sécond"
    with bh as stream:
        stream.write(second_payload)
        stream.flush()
    assert bh.drained_bytes == len(second_payload.encode("utf-8")), (
        "reusing the sink must publish only the second context's byte count"
    )


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_exit_clears_internal_state() -> None:
    """PtyBlackhole.__exit__ clears _master_fd, _slave, and _thread."""
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    with bh:
        pass
    assert bh._master_fd is None, "exit must clear the PTY master descriptor"
    assert bh._slave is None, "exit must clear the PTY slave stream"
    assert bh._thread is None, "exit must clear a joined drainer thread"


def test_pty_blackhole_hides_drain_count_until_the_drainer_stops() -> None:
    """A timed-out join must not publish a drainer-owned counter."""
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    still_running = mock.Mock(spec=threading.Thread)
    still_running.is_alive.return_value = True
    bh._thread = still_running
    bh._drained_bytes = 42

    bh.__exit__(None, None, None)

    still_running.join.assert_called_once_with(timeout=5.0)
    assert bh._thread is still_running, (
        "a timed-out join must retain the drainer for a later lifecycle boundary"
    )
    assert bh.drained_bytes is None, (
        "the count must remain unavailable while the drainer is still running"
    )


def test_pty_blackhole_publishes_count_after_a_late_drainer_exit() -> None:
    """A drainer that stops after the bounded join eventually publishes its count."""
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    eventually_stopped = mock.Mock(spec=threading.Thread)
    eventually_stopped.is_alive.side_effect = (True, False, False)
    bh._thread = eventually_stopped
    bh._drained_bytes = 42

    bh.__exit__(None, None, None)

    bh._drainer_finished.set()

    assert bh.drained_bytes == 42, (
        "a completed drainer must publish its count without mutating lifecycle state"
    )
    assert bh._thread is eventually_stopped, (
        "reading the count must not join or clear the retained drainer"
    )
    bh._publish_finished_drainer()
    assert eventually_stopped.join.call_args_list == [
        mock.call(timeout=5.0),
        mock.call(),
    ], "a late drainer must be joined only at the explicit lifecycle boundary"
    assert bh._thread is None, "the lifecycle boundary must clear a joined drainer"


def test_pty_blackhole_rejects_reuse_while_the_drainer_is_running() -> None:
    """PtyBlackhole reports an active previous drainer with its domain error."""
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    still_running = mock.Mock(spec=threading.Thread)
    still_running.is_alive.return_value = True
    bh._thread = still_running

    with pytest.raises(PtyBlackholeStateError, match="cannot reuse PtyBlackhole"):
        bh.__enter__()


# ---------------------------------------------------------------------------
# open_sink factory
# ---------------------------------------------------------------------------


def test_open_sink_devnull_yields_writable_stream() -> None:
    """open_sink('devnull') yields a writable text stream."""
    with sinks.open_sink("devnull", encoding="utf-8", errors="replace") as stream:
        assert stream.writable()
        n = stream.write("test")
        assert n > 0


def test_open_sink_text_blackhole_yields_text_blackhole() -> None:
    """open_sink('text_blackhole') yields a TextBlackhole instance."""
    with sinks.open_sink(
        "text_blackhole",
        encoding="utf-8",
        errors="replace",
    ) as stream:
        assert isinstance(stream, sinks.TextBlackhole)
        assert stream.write("hello") == 5


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_open_sink_pty_blackhole_yields_writable_stream() -> None:
    """open_sink('pty_blackhole') yields a writable text stream."""
    with sinks.open_sink("pty_blackhole", encoding="utf-8", errors="replace") as stream:
        assert stream.writable()
