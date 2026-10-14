"""Execution context, timeout, and stdin types for ``cuprum.sh``.

These types describe how a command or pipeline stage is run rather than what
it does: the environment overlay, working directory, process-group ownership,
cancellation grace periods, echo sinks, and encoding used to run it, alongside
the timeout exception and stdin payload types that accompany execution. The
``cuprum.sh`` package re-exports them.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import enum
import typing as typ
from pathlib import Path

from cuprum.context import EnvMode, UnsetType, _validate_timeout

type _EnvMapping = cabc.Mapping[str, str | UnsetType] | None
type _CwdType = str | Path | None

_DEFAULT_CANCEL_GRACE = 0.5
_DEFAULT_NATIVE_PUMP_CLEANUP_GRACE = 0.5


_DEFAULT_ENCODING = "utf-8"
_DEFAULT_ERROR_HANDLING = "replace"

__all__ = [
    "ExecutionContext",
    "ProcessGroupPolicy",
    "StdinInput",
    "TimeoutExpired",
]


class ProcessGroupPolicy(enum.StrEnum):
    """Choose whether a run owns the process group its child starts.

    ``INHERIT`` is the default. The child joins the parent's process group and
    session, and teardown signals the direct child alone, so a run terminates
    exactly the process it spawned.

    ``OWN_GROUP`` gives the run ownership of a process group of its own. On
    POSIX the child is spawned with ``start_new_session=True``, which makes it
    the leader of a new session and a new process group, so the group is
    addressed by the child's own process identifier. Teardown then signals
    every member of that group, which lets a child's descendants reach
    end-of-file on inherited pipes instead of holding a timed-out run's drain
    open.

    Ownership is bounded. A descendant that leaves the group deliberately —
    with ``setsid()`` or ``setpgid()`` — is outside it and outside the
    guarantee, as are the run's ancestors, its siblings, and any process that
    predates the spawn. Callers needing containment for a descendant that
    detaches its own session require a supervisor, a control group, or a
    container.

    ``OWN_GROUP`` is rejected with a :class:`ValueError` at spawn on Windows,
    which has no POSIX process group and no way to assign a Job Object
    atomically with process creation.
    """

    INHERIT = "inherit"
    OWN_GROUP = "own_group"


def _parse_process_group_policy(
    raw: ProcessGroupPolicy | str,
) -> ProcessGroupPolicy:
    """Coerce a raw policy value to a :class:`ProcessGroupPolicy` member.

    The pure parsing core behind :meth:`ExecutionContext.__post_init__`. It
    takes the raw value directly so a caller spelling the policy as a string
    gets the same validation a member does, and an unrecognized spelling is
    rejected here rather than silently resolving to a different policy.

    Parameters
    ----------
    raw:
        A :class:`ProcessGroupPolicy` member or its string value.

    Returns
    -------
    ProcessGroupPolicy
        The parsed policy.

    Raises
    ------
    ValueError
        If ``raw`` is not a known policy value.
    """
    try:
        return ProcessGroupPolicy(raw)
    except ValueError:
        valid = ", ".join(member.value for member in ProcessGroupPolicy)
        msg = f"invalid process_group {raw!r}; expected one of: {valid}"
        raise ValueError(msg) from None


@dc.dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Execution parameters for SafeCmd runtime control.

    Attributes
    ----------
    env:
        Environment variable overlay applied to the subprocess.
    cwd:
        Working directory for the subprocess.
    cancel_grace:
        Seconds to wait after SIGTERM before escalating to SIGKILL.
    native_pump_cleanup_grace:
        Seconds to wait for a cancelled native-pump worker before its
        descriptor cleanup is deferred to its completion callback.
    timeout:
        Optional runtime timeout in seconds. ``None`` means no override.
    stdout_sink:
        Text sink for echoing stdout; defaults to the active ``sys.stdout``.
    stderr_sink:
        Text sink for echoing stderr; defaults to the active ``sys.stderr``.
        When no ``on_idle`` callback is supplied, it also receives the idle
        heartbeat's keepalive line, written and flushed synchronously on the
        run's event loop, so its ``write`` and ``flush`` must return promptly:
        a sink that blocks delays the run's stream reads, timeout handling,
        and cancellation. Hand a slow destination to a worker thread, an
        executor, or a genuinely non-blocking drain such as a queue fed with
        ``put_nowait``. A separate asyncio task on the run's own loop is not
        enough: draining that queue still competes with the parent's stream
        reads.
    encoding:
        Character encoding used when decoding subprocess output.
    errors:
        Error handling strategy applied during decoding.
    tags:
        Optional metadata attached to structured execution events.
    env_mode:
        Environment policy applied when rendering ``env`` for the subprocess.
    process_group:
        Whether the run owns the child's process group. ``INHERIT`` keeps the
        child in the parent's group and signals only the direct child on
        teardown; ``OWN_GROUP`` spawns the child as the leader of its own
        session and process group on POSIX and terminates that whole group.
        A member may be given either as a :class:`ProcessGroupPolicy` or as
        the string spelling of one, which ``__post_init__`` resolves to the
        member it names and rejects if it names none. See
        :class:`ProcessGroupPolicy`.

    """

    env: _EnvMapping = None
    cwd: _CwdType = None
    cancel_grace: float = _DEFAULT_CANCEL_GRACE
    native_pump_cleanup_grace: float = _DEFAULT_NATIVE_PUMP_CLEANUP_GRACE
    timeout: float | None = None
    stdout_sink: typ.IO[str] | None = None
    stderr_sink: typ.IO[str] | None = None
    encoding: str = _DEFAULT_ENCODING
    errors: str = _DEFAULT_ERROR_HANDLING
    tags: cabc.Mapping[str, object] | None = None
    env_mode: EnvMode = EnvMode.OVERLAY
    process_group: ProcessGroupPolicy | str = ProcessGroupPolicy.INHERIT

    def __post_init__(self) -> None:
        """Normalize the process-group policy, then validate the cleanup grace.

        The field accepts a plain string so a caller can spell the policy as
        its value; ``__post_init__`` narrows it to a member, which every
        consumer in the execution layer then compares with ``is``. Without
        this step a string would compare unequal to every member and silently
        select the default, so a caller asking for an owned group would get
        the inherited one and no error to show for it.

        Raises
        ------
        ValueError
            If ``process_group`` names no member, or
            ``native_pump_cleanup_grace`` is not a valid grace period.
        """
        object.__setattr__(
            self,
            "process_group",
            _parse_process_group_policy(self.process_group),
        )
        cleanup_grace = _validate_timeout(
            self.native_pump_cleanup_grace,
            "ExecutionContext native_pump_cleanup_grace",
        )
        if cleanup_grace is None:
            msg = "ExecutionContext native_pump_cleanup_grace must not be None"
            raise ValueError(msg)
        object.__setattr__(self, "native_pump_cleanup_grace", cleanup_grace)

    @property
    def resolved_process_group(self) -> ProcessGroupPolicy:
        """The policy behind the declared ``ProcessGroupPolicy | str`` field.

        ``__post_init__`` normalizes the field, but the declared type stays
        wide because a caller may spell the policy as a string. This is the
        narrow view the execution layer reads, so it never has to parse.

        Returns
        -------
        ProcessGroupPolicy
            The normalized policy member, never a string.
        """
        return _parse_process_group_policy(self.process_group)


class TimeoutExpired(TimeoutError):  # ruff: ignore[error-suffix-on-exception-name] - match subprocess.TimeoutExpired naming.
    """Raised when command execution exceeds the configured timeout."""

    def __init__(
        self,
        *,
        cmd: cabc.Sequence[str] | object,
        timeout: float,
        output: str | bytes | None = None,
        stderr: str | bytes | None = None,
    ) -> None:
        """Store the command, timeout, and any captured output."""
        super().__init__(f"Command {cmd!r} timed out after {timeout} seconds")
        self.cmd = cmd
        self.timeout = timeout
        self.output = output
        self.stderr = stderr

    @property
    def stdout(self) -> str | bytes | None:
        """Captured stdout, mirroring ``subprocess.TimeoutExpired``.

        Returns
        -------
        str | bytes | None
            Captured standard output, or ``None`` when no output was
            captured before expiry.
        """
        return self.output


@dc.dataclass(frozen=True, slots=True)
class StdinInput:
    """Caller-provided data to write to a subprocess's stdin pipe.

    Exactly one of *text* or *data* may be supplied.
    """

    text: str | None = None
    data: bytes | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous stdin payloads."""
        if self.text is not None and self.data is not None:
            msg = "text and data cannot both be provided"
            raise ValueError(msg)

    def resolve(self, ctx: ExecutionContext) -> bytes | None:
        """Return the bytes payload, encoding *text* with *ctx* when needed.

        Parameters
        ----------
        ctx : ExecutionContext
            The execution context whose ``encoding`` and ``errors`` encode
            ``text`` when no raw ``data`` is set.

        Returns
        -------
        bytes | None
            The raw *data* payload, or *text* encoded with ``ctx.encoding``
            and ``ctx.errors``; ``None`` when neither field is set.

        Raises
        ------
        UnicodeEncodeError
            If ``text`` cannot be encoded with ``ctx.encoding`` under
            ``ctx.errors`` (for example, ``errors="strict"``).
        """  # ruff: ignore[docstring-extraneous-exception] - UnicodeEncodeError propagates from str.encode
        if self.text is not None:
            return self.text.encode(ctx.encoding, ctx.errors)
        return self.data
