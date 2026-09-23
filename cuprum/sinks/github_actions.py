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

Steps 1, 2, and 4 are the *group* half of that sequence and step 5 is the
*annotation* half; ``emit_group`` and ``emit_annotation`` switch each half off
independently. A suppressed group takes its lease with it: the lease exists
only to shield an open group, so a lease with no group would silence
workflow-command interpretation for the rest of the step and show nothing for
it.

The sink never changes capture, success semantics, or the returned result: it
is a presentation adapter over the same destinations a run already uses. A
caller opts in per invocation with ``RunOutputOptions(sink=GitHubActionsSink())``
and opt-out is simply omitting the sink. ``RunOutputOptions.group`` and
``RunOutputOptions.annotate_failure`` are the zero-ceremony spelling of the
same opt-in: they construct this adapter with the matching toggles and store it
as the run's sink, so the workflow commands stay here and the execution layer
learns nothing about them.

Activation is environment-gated to honour that opt-in outside CI: a sink
passed without ``force`` stays inactive unless the parent process runs on
GitHub Actions (``GITHUB_ACTIONS=true``, read at :meth:`open_session` time).
Pass ``force=True`` to frame local or non-standard-runner runs deliberately.
"""

from __future__ import annotations

import dataclasses as dc
import os
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
_GITHUB_ACTIONS_ENV = "GITHUB_ACTIONS"
_GITHUB_ACTIONS_TRUE = "true"


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


@dc.dataclass(frozen=True, slots=True)
class _Annotation:
    """One run's annotation title and the two halves of its frame.

    Bundling these keeps them travelling together: the annotation title is
    only meaningful beside the toggle that decides whether it is written, and
    the two toggles are set together from
    :class:`~cuprum.sh.RunOutputOptions`, so a partial bundle would be a
    caller mistake rather than a configuration.

    Attributes
    ----------
    label:
        Title for a failure annotation, always the run's bounded label.
    emit_group:
        Whether the session writes the group command, its stop-commands
        lease, and the endgroup. ``False`` suppresses all three together: the
        lease shields an open group, so it must not outlive the group it was
        taken for.
    emit_annotation:
        Whether a failed outcome emits its ``::error::`` annotation.
        Independent of ``emit_group``: annotation without framing is a valid
        combination for a caller who wants a run summary entry but not
        collapsible logs.
    """

    label: str
    emit_group: bool = True
    emit_annotation: bool = True


def _stderr() -> typ.IO[str]:
    """Return the parent's stderr as the default workflow-command stream."""
    return sys.stderr


def _reject_non_bool(name: str, value: object) -> None:
    """Reject a value that is not the documented ``bool`` for a toggle."""
    if not isinstance(value, bool):
        msg = f"{name} must be a bool, got {value!r}"
        raise TypeError(msg)


class GitHubActionsSession:
    """One run's framed presentation session on GitHub Actions.

    Framing is complete the moment the session exists: the group opening and
    the stop-commands lease are written before the sink returns it, so child
    output can never appear above the group opening. The session holds that
    lease for the run's entire lifetime and closes the group with an error
    annotation when the run's terminal outcome is a failure.

    Both halves of that frame are separately switchable. A session opened with
    ``emit_group=False`` writes no group command and takes no lease — it is a
    plain pass-through destination that may still annotate. A session opened
    with ``emit_annotation=False`` frames normally but never annotates.
    """

    def __init__(
        self,
        log: typ.IO[str],
        label: str,
        annotation: _Annotation,
    ) -> None:
        """Frame the run: the group opening, then the stop-commands lease.

        Parameters
        ----------
        log : typ.IO[str]
            The parent-facing destination for the framing and framed output.
        label : str
            The group title. Bounded by the caller and escaped here.
        annotation : _Annotation
            Which halves of the frame to write: the group opening with its
            stop-commands lease, and a failure annotation. The annotation also
            carries the title that failure uses, which is kept separate from
            *label* so a group titled with the run's argv never republishes
            those arguments in an annotation.
        """
        self._log = log
        self._label = label
        self._annotation_label = annotation.label
        self._emit_group = annotation.emit_group
        self._emit_annotation = annotation.emit_annotation
        self._closed = False
        self._stop_token = _new_stop_token()
        if not annotation.emit_group:
            return
        self._log.write(f"{_GROUP_PREFIX}{_escape_data(self._label)}\n")
        # The lease opens *inside* the group, after the ``::group::`` command,
        # so the runner processes that command itself and then stops
        # interpreting child output as workflow commands for the rest of the
        # run.
        self._log.write(f"{_STOP_PREFIX}{self._stop_token}\n")

    @property
    def log(self) -> typ.IO[str]:
        """The ordered parent-facing log destination for this run."""
        return self._log

    @property
    def redirects_echo(self) -> bool:
        """Whether echoed child output should use this session's log."""
        return self._emit_group

    @property
    def stop_token(self) -> str:
        """The unique stop-commands token leased for this run.

        Drawn for every session, whether or not a lease was written, so a
        group-less session's token is simply unused rather than absent: the
        alternative would make the property's contract depend on the toggle.
        """
        return self._stop_token

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
        if self._emit_group:
            # Release the lease *before* closing the group: the endgroup
            # command itself must be interpreted by the runner, so it has to
            # be written after the stop-commands bracket has ended. The runner
            # resumes on reading the token as a command of its own, so this is
            # ``::<token>::``, not the stop command repeated.
            self._log.write(f"::{self._stop_token}::\n")
            self._log.write(f"{_ENDGROUP}\n")
        if not self._emit_annotation:
            return
        if outcome.outcome == TerminalOutcome.EXIT_ZERO:
            return
        self._emit_error_annotation(outcome)

    def _emit_error_annotation(self, outcome: SessionOutcome) -> None:
        """Write one ``::error::`` workflow command for a failed run.

        The title is the session's bounded annotation label — never the argv
        a group may be titled with — and the message is the categorical
        detail, so neither argument values nor exception text reach the
        workflow log.
        """
        title = _escape_property(self._annotation_label)
        detail = outcome.detail or outcome.outcome.value
        message = _escape_data(detail)
        self._log.write(f"{_ERROR_PREFIX}title={title}::{message}\n")


class GitHubActionsSink:
    """Opt-in presentation sink for GitHub Actions CI callers.

    The sink holds only immutable configuration; every run gets a fresh
    :class:`GitHubActionsSession` from :meth:`open_session`, so one sink
    instance can serve many sequential or concurrent runs.

    ``emit_group`` and ``emit_annotation`` independently control whether the
    session writes the collapsible group frame and failure annotation.

    Parameters
    ----------
    destination:
        Writable text stream receiving the workflow commands and framed
        output. Defaults to the parent's stderr, matching where GitHub
        Actions reads workflow commands from.
    title:
        Optional display label overriding the derived
        ``"<project>: <program>"`` label, used for both the group title and a
        failure annotation's title.
    force:
        Activate the sink even when the parent process does not run on
        GitHub Actions. Intended for local reproduction of CI framing and
        non-standard runners; when ``False`` (the default) the sink defers
        to the environment check.
    emit_group:
        Whether a session writes the group commands and their stop-commands
        lease. ``False`` leaves the run's log destination unframed, which is
        what ``RunOutputOptions(annotate_failure=True)`` asks for: an
        annotation without collapsible logs.
    emit_annotation:
        Whether a failed outcome emits its ``::error::`` annotation. ``False``
        keeps the framing and drops the annotation, which is what
        ``RunOutputOptions(group=True)`` asks for.

    Raises
    ------
    TypeError
        If ``emit_group`` or ``emit_annotation`` is not a ``bool``. They gate
        workflow commands, so a merely truthy value would frame a run on the
        strength of something the caller never documented as a flag.
    """

    def __init__(  # ruff: ignore[too-many-arguments] - every argument after the destination is keyword-only, so there is no positional order to confuse
        self,
        destination: typ.IO[str] | None = None,
        *,
        title: str | None = None,
        force: bool = False,
        emit_group: bool = True,
        emit_annotation: bool = True,
    ) -> None:
        """Store immutable configuration; no output happens until a run."""
        _reject_non_bool("emit_group", emit_group)
        _reject_non_bool("emit_annotation", emit_annotation)
        self.destination = destination
        self.title = title
        self.force = force
        self.emit_group = emit_group
        self.emit_annotation = emit_annotation

    def open_session(self, start: SessionStart) -> GitHubActionsSession | None:
        """Open a framed session for one run.

        Parameters
        ----------
        start : SessionStart
            Bounded run metadata; ``start.argv`` titles the group so the
            collapsed log entry reads ``<program args>``, while
            ``start.label`` titles a failure annotation — an annotation is a
            workflow-command property, so it never publishes argument
            values.

        Returns
        -------
        GitHubActionsSession | None
            The active session, or ``None`` when the sink stays inactive: the
            parent process is not GitHub Actions (``GITHUB_ACTIONS`` unset or
            not ``true``) and ``force`` was not requested. An inactive sink
            writes nothing and the run keeps its plain destinations.
        """
        if not self._is_active():
            return None
        if self.title is not None:
            label = self.title
        elif start.argv:
            label = " ".join(start.argv)
        else:
            label = start.label
        # The annotation keeps the bounded label even when the group is titled
        # with argv: an annotation is a workflow command property, so publishing
        # arguments there would leak them into the run summary.
        log = self.destination if self.destination is not None else _stderr()
        return GitHubActionsSession(
            log,
            label,
            _Annotation(
                label=self.title or start.label,
                emit_group=self.emit_group,
                emit_annotation=self.emit_annotation,
            ),
        )

    def _is_active(self) -> bool:
        """Return whether this run's environment demands Actions framing.

        The environment value is read per run, not cached at import or
        construction time, so a run started after the variable is set (or
        removed) sees the current environment. ``GITHUB_ACTIONS=true`` is the
        runner's own signal; any other value — including ``1`` or ``TRUE`` —
        keeps the sink inactive because only the documented runner value may
        trigger framing.

        Returns
        -------
        bool
            ``True`` when the sink should open a framed session this run.
        """
        return self.force or (
            os.environ.get(_GITHUB_ACTIONS_ENV) == _GITHUB_ACTIONS_TRUE
        )
