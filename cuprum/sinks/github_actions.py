"""GitHub Actions presentation sink.

:class:`GitHubActionsSink` frames one run's parent-facing output in a workflow
command group, shields the group from workflow-command injection with a
stop-commands lease, and annotates the run when it finishes unsuccessfully.
The adapter writes GitHub Actions workflow commands — ``::group::``,
``::endgroup::``, ``::error::``, and the stop-commands bracket — through the
session's log destination so the runner surfaces them as collapsible groups
and annotations.

Framing order per run:

1. ``::group::<title>`` opens the group (title is the program args).
2. The stop-commands lease opens immediately after the group command, so the
   runner processes the group command itself and then stops interpreting
   child output as workflow commands for the rest of the run.
3. The run's mirrored output and any diagnostics flow into the group.
4. At teardown the session releases the lease *before* writing
   ``::endgroup::``, so the runner processes the group command itself.
5. A failed outcome emits exactly one ``::error::`` annotation with the run's
   bounded label as the title and a categorical detail as the message, after
   the lease has been released so the runner interprets it.

The sink never changes capture, success semantics, or the returned result: it
is a presentation adapter over the same destinations a run already uses. A
caller opts in per invocation with ``RunOutputOptions(sink=GitHubActionsSink())``
and opt-out is simply omitting the sink.
"""

from __future__ import annotations

import secrets
import sys
import typing as typ

from cuprum.sinks.base import (
    SessionOutcome,
    SessionStart,
    TerminalOutcome,
)

__all__ = ["GitHubActionsSession", "GitHubActionsSink"]

_GROUP_PREFIX = "::group::"
_ENDGROUP = "::endgroup::"
_ERROR_PREFIX = "::error "
_STOP_PREFIX = "::stop-commands::"
_STOP_TOKEN_LENGTH = 16


def _escape_data(value: str) -> str:
    """Escape a value used in a workflow-command data position.

    GitHub Actions replaces ``%25``, ``%0D``, and ``%0A`` in the *data*
    segment after ``::command::``; see the workflow-command reference. The
    escaping here mirrors that contract.

    Returns
    -------
    str
        The escaped value safe for a workflow-command data position.
    """
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    """Escape a value used in a workflow-command property position.

    Property values (for example ``title=``) additionally escape ``:`` and
    ``,``, which delimit the property list.

    Returns
    -------
    str
        The escaped value safe for a workflow-command property position.
    """
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


def _new_stop_token() -> str:
    """Return a fresh stop-commands token.

    The token must be unpredictable to the child whose output the lease
    shields: a guessable token would let hostile child output end the lease
    early and resume workflow-command processing. ``secrets.token_hex`` draws
    from the operating system's CSPRNG.

    Returns
    -------
    str
        A fresh unpredictable hexadecimal token.
    """
    return secrets.token_hex(_STOP_TOKEN_LENGTH // 2)


def _stderr() -> typ.IO[str]:
    """Return the parent's stderr as the default workflow-command stream."""
    return sys.stderr


class GitHubActionsSession:
    """One run's framed presentation session on GitHub Actions.

    The session opens the group when the run's first write goes through
    :attr:`log` (or eagerly via :meth:`open_group`), holds a stop-commands
    lease for the run's entire lifetime, and closes the group with an error
    annotation when the run's terminal outcome is a failure.
    """

    def __init__(self, log: typ.IO[str], label: str) -> None:
        """Frame the session; group emission is deferred until first use."""
        self._log = log
        self._label = label
        self._closed = False
        self._group_open = False
        self._stop_token = _new_stop_token()

    @property
    def log(self) -> typ.IO[str]:
        """The ordered parent-facing log destination for this run.

        The first access opens the group, so mirrored child output lands
        inside the framing even when the execution layer never calls
        :meth:`open_group` eagerly.
        """
        self.open_group()
        return self._log

    @property
    def stop_token(self) -> str:
        """The unique stop-commands token leased for this run."""
        return self._stop_token

    def open_group(self) -> None:
        """Emit the group opening command, entering the stop-commands lease.

        Called by the execution layer before the subprocess starts. The lease
        opens *inside* the group, after the ``::group::`` command, so the
        runner processes the group command itself and then stops interpreting
        child output as workflow commands for the rest of the run.
        """
        if self._group_open or self._closed:
            return
        self._log.write(f"{_GROUP_PREFIX}{_escape_property(self._label)}\n")
        self._log.write(f"{_STOP_PREFIX}{self._stop_token}\n")
        self._group_open = True

    def close(self, outcome: SessionOutcome) -> None:
        """Close the group, release the lease, and annotate failures.

        Idempotent: only the first call writes. After the group closes, the
        stop-commands lease is released with the session's own token, so the
        runner resumes workflow-command processing for later output. A
        non-zero exit (or a timeout/cancellation/error without an exit code)
        emits a single ``::error::`` annotation after the lease release so the
        annotation is processed.
        """
        if self._closed:
            return
        self._closed = True
        if self._group_open:
            # Release the lease *before* closing the group: the endgroup
            # command itself must be interpreted by the runner, so it has to
            # be written after the stop-commands bracket has ended.
            self._log.write(f"{_STOP_PREFIX}{self._stop_token}\n")
            self._log.write(f"{_ENDGROUP}\n")
            self._group_open = False
        if outcome.outcome == TerminalOutcome.EXIT_ZERO:
            return
        self._emit_error_annotation(outcome)

    def _emit_error_annotation(self, outcome: SessionOutcome) -> None:
        """Write one ``::error::`` workflow command for a failed run."""
        title = _escape_property(self._label)
        detail = outcome.detail or outcome.outcome.value
        message = _escape_data(detail)
        self._log.write(f"{_ERROR_PREFIX}title={title}::{message}\n")


class GitHubActionsSink:
    """Opt-in presentation sink for GitHub Actions CI callers.

    The sink holds only immutable configuration; every run gets a fresh
    :class:`GitHubActionsSession` from :meth:`open_session`, so one sink
    instance can serve many sequential or concurrent runs.

    Parameters
    ----------
    destination:
        Writable text stream receiving the workflow commands and framed
        output. Defaults to the parent's stderr, matching where GitHub
        Actions reads workflow commands from.
    title:
        Optional display label overriding the derived
        ``"<project>: <program>"`` label.
    """

    def __init__(
        self,
        destination: typ.IO[str] | None = None,
        *,
        title: str | None = None,
    ) -> None:
        """Store immutable configuration; no output happens until a run."""
        self.destination = destination
        self.title = title

    def open_session(self, start: SessionStart) -> GitHubActionsSession:
        """Open a framed session for one run.

        Parameters
        ----------
        start : SessionStart
            Bounded run metadata; ``start.argv`` titles the group so the
            collapsed log entry reads ``<program args>``.

        Returns
        -------
        GitHubActionsSession
            The active session. This adapter never declines activation: a
            caller that opts in wants the framing on every run.
        """
        if self.title is not None:
            label = self.title
        elif start.argv:
            label = " ".join(start.argv)
        else:
            label = start.label
        log = self.destination if self.destination is not None else _stderr()
        return GitHubActionsSession(log=log, label=label)
