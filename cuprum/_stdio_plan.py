"""Resolved stdio planning for one direct command run.

``SafeCmd`` accepts a ``StdioTarget`` for stdout and stderr and either a
payload or a producer for stdin. This module turns those caller-facing values
into the two things the spawn layer actually needs: a resolved *plan* naming
where each of the child's three standard streams is bound, and, for a stream
cuprum owns, the descriptor to hand the child.

Resolving here, before the child exists, is what keeps the spawn layer a
translation rather than a decision. The one deliberate exception is the owned
path: :func:`_resolve_stdio_binding` records the *path*, and the spawn layer
opens it immediately before the spawn call and closes cuprum's copy
immediately after. Opening any earlier would hold a descriptor across the whole
of the parent's own preparation for no benefit, and a target naming a file that
does not exist yet stays valid configuration until the moment the child needs
it.

The module is small on purpose (ADR-007): it owns one value type per stream,
the two resolvers, and the vocabulary shared by the execution, spawn, and
stream-wiring layers, and nothing about how a run is driven.
"""

from __future__ import annotations

import dataclasses as dc
import os
import typing as typ

from cuprum._constants import (
    STDERR_STREAM,
    STDIN_STREAM,
    STDOUT_STREAM,
    PipeStream,
)
from cuprum.sh.execution import StdinStream

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.sh import RunOutputOptions, StdioTarget

__all__ = [
    "_NoStdin",
    "_PayloadStdin",
    "_PipeStdin",
    "_ResolvedStdio",
    "_StdinPlan",
    "_StdioBinding",
    "_StreamStdin",
    "_open_owned_path",
    "_resolve_stdin_plan",
    "_resolve_stdio",
    "_resolve_stdio_binding",
]


@dc.dataclass(frozen=True, slots=True)
class _NoStdin:
    """The child inherits the parent's stdin.

    This is the default, and it is distinct from an empty payload: a caller who
    supplies ``StdinInput(data=b"")`` gets a pipe that is closed immediately,
    whereas a caller who supplies nothing inherits whatever the parent has.
    """


@dc.dataclass(frozen=True, slots=True)
class _PayloadStdin:
    """One complete payload, already encoded against the run's context."""

    data: bytes


@dc.dataclass(frozen=True, slots=True)
class _StreamStdin:
    """A producer the writer pulls one chunk at a time during the run."""

    stream: StdinStream


@dc.dataclass(frozen=True, slots=True)
class _PipeStdin:
    """A library-owned stdin pipe that nothing writes to.

    Reached when a caller asks for ``StdioTarget.pipe()`` on stdin without
    supplying a source. The child sees an immediately closed pipe rather than
    the parent's stdin — the same child-side view as an empty payload, but
    chosen explicitly instead of inferred from one.
    """


type _StdinPlan = _NoStdin | _PayloadStdin | _StreamStdin | _PipeStdin


@dc.dataclass(frozen=True, slots=True)
class _StdioBinding:
    """Where one of the child's standard streams is bound, resolved.

    The three fields answer three different questions the spawn layer asks
    separately. *descriptor* is what the child is given, or ``None`` to inherit
    the parent's own stream. *is_pipe* is the only thing that may start a
    consumer or a writer task. *owned_path* is the file cuprum owes a close
    once the child has inherited it, and it is a path rather than a descriptor
    precisely so that no descriptor exists until the spawn is about to happen.
    A borrowed descriptor is carried in *descriptor* alone, which is what makes
    "cuprum closes only what it opened" a property of the value rather than a
    rule someone has to remember.
    """

    stream: PipeStream
    descriptor: int | None = None
    is_pipe: bool = False
    owned_path: Path | None = None


@dc.dataclass(frozen=True, slots=True)
class _ResolvedStdio:
    """Every stream of one run, resolved, as the spawn layer consumes it.

    ``pipes`` is a frozenset of stream names rather than a flag per stream
    because it is the one piece of the resolution the *fallback* backend cannot
    re-derive: ``Popen`` leaves ``stdout``/``stderr`` as ``None`` for anything
    that is not a pipe, but ``asyncio.create_subprocess_exec`` would wrap
    whatever it is handed. Naming the pipes explicitly is what makes both
    backends agree on which streams cuprum owns a reader for.
    """

    stdin: _StdinPlan
    stdout: _StdioBinding
    stderr: _StdioBinding

    @property
    def pipes(self) -> frozenset[PipeStream]:
        """The stream names whose stdio is a library-owned pipe.

        A ``_NoStdin`` plan contributes nothing: the child inherits the
        parent's stdin, so there is no descriptor of cuprum's and nothing to
        write to. Every other plan means cuprum holds the pipe.
        """
        names: set[PipeStream] = (
            {STDIN_STREAM} if not isinstance(self.stdin, _NoStdin) else set()
        )
        names.update(
            binding.stream for binding in (self.stdout, self.stderr) if binding.is_pipe
        )
        return frozenset(names)


def _ensure_redirection_supported(stream: PipeStream, kind: str) -> None:
    """Refuse descriptor redirection where the platform cannot honour it.

    Only the descriptor arms are gated. Inheriting a stream is handled by
    ``Popen`` itself on every platform, and a library-owned pipe is an ordinary
    ``os.pipe``, so neither is a POSIX-specific claim and neither is refused
    here — a supported combination must keep working on a platform that cannot
    redirect.

    Parameters
    ----------
    stream : str
        The stream being redirected, for the message.
    kind : str
        The target variant being refused.

    Raises
    ------
    ValueError
        If redirection is attempted on a platform without POSIX descriptors.
    """
    if os.name == "posix":
        return
    msg = (
        f"RunOutputOptions {stream} cannot be redirected to {kind!r} on this "
        "platform: cuprum opens and closes standard-stream descriptors with "
        "POSIX semantics, which it does not have here. Leave stdout and stderr "
        "unset on this platform."
    )
    raise ValueError(msg)


def _open_owned_path(stream: PipeStream, path: Path) -> int:
    """Open a caller-named file for a child stream, truncating it.

    The mode is fixed rather than taken from the target: cuprum is being asked
    to give the child a stream, and a stream is written from its start. An
    append or read-only file is a different contract, and one the caller can
    already express by handing cuprum a borrowed descriptor instead.

    Parameters
    ----------
    stream : str
        The stream the file is bound to; used only in the message on failure.
    path : Path
        The file to open.

    Returns
    -------
    int
        The open descriptor, which cuprum owns and must close.

    Raises
    ------
    OSError
        If the file cannot be created or opened. Reported against the stream so
        a caller with two redirected streams can tell which one failed.
    """
    try:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    except OSError as exc:
        msg = f"RunOutputOptions {stream} could not open {path}: {exc}"
        raise OSError(msg) from exc


def _resolve_stdio_binding(
    stream: PipeStream,
    target: StdioTarget,
) -> _StdioBinding:
    """Resolve one output target into what the spawn layer binds.

    A borrowed file object is flushed here, immediately before the spawn, so
    bytes the caller has already written but not yet pushed to the kernel are
    visible to the child. Flushing earlier would not be enough — more writes
    could follow — and later would be too late.

    Parameters
    ----------
    stream : str
        ``"stdout"`` or ``"stderr"``, used for diagnostics and as the binding's
        own name.
    target : StdioTarget
        The caller's target for that stream.

    Returns
    -------
    _StdioBinding
        The descriptor to pass to the child (or ``None`` to inherit), whether
        it is a pipe, and the path cuprum owes a close. A ``path`` target
        contributes only the *path*: the descriptor is opened immediately
        before the spawn, so nothing is opened here and no ``OSError`` can
        arise from this call.

    Raises
    ------
    ValueError
        If descriptor redirection is attempted on a platform without POSIX
        descriptors, as decided by :func:`_ensure_redirection_supported`.
    """  # ruff: ignore[docstring-extraneous-exception] - ValueError propagates from _ensure_redirection_supported.
    kind = target.kind
    if kind == "pipe":
        return _StdioBinding(stream=stream, is_pipe=True)
    if kind == "inherit":
        return _StdioBinding(stream=stream)
    _ensure_redirection_supported(stream, kind)
    if kind == "path":
        return _StdioBinding(stream=stream, owned_path=target.path_value)
    borrowed = target.fd_value
    if isinstance(borrowed, int):
        return _StdioBinding(stream=stream, descriptor=borrowed)
    borrowed.flush()
    # The object's descriptor, not the object: the spawn layer wants an ``int``,
    # and taking it here is the same call ``Popen`` would make for itself. The
    # descriptor keeps its owner — the caller's file object is still the only
    # thing that may close it, which is why only *owned_path* is ever closed.
    return _StdioBinding(stream=stream, descriptor=borrowed.fileno())


def _resolve_stdin_plan(
    stdin: bytes | StdinStream | None,
    output: RunOutputOptions,
) -> _StdinPlan:
    """Resolve a run's stdin into the plan its spawn will bind.

    Parameters
    ----------
    stdin : bytes | StdinStream | None
        The run's already-resolved stdin: an encoded payload, a producer left
        unpulled, or ``None`` for "the caller supplied no source".
    output : RunOutputOptions
        The run's options, consulted only for an explicitly requested stdin
        pipe when no source was supplied.

    Returns
    -------
    _StdinPlan
        The plan the spawn layer maps to a stdio value.
    """
    if isinstance(stdin, StdinStream):
        return _StreamStdin(stream=stdin)
    if stdin is not None:
        return _PayloadStdin(data=stdin)
    return _PipeStdin() if _wants_stdin_pipe(output) else _NoStdin()


def _wants_stdin_pipe(output: RunOutputOptions) -> bool:
    """Whether the options explicitly ask for a stdin pipe with no source."""
    target = output.stdin
    return target is not None and target.kind == "pipe"


def _resolve_stdio(
    stdin: bytes | StdinStream | None,
    output: RunOutputOptions,
) -> _ResolvedStdio:
    """Resolve every standard stream of one run, before its child exists.

    Parameters
    ----------
    stdin : bytes | StdinStream | None
        The run's already-resolved stdin.
    output : RunOutputOptions
        The run's options, carrying its stdin, stdout, and stderr targets.

    Returns
    -------
    _ResolvedStdio
        The plan and bindings the spawn layer consumes. No descriptor is opened
        here, so a ``path`` target contributes only its path and the spawn
        layer performs the open.

    Raises
    ------
    ValueError
        If a target cannot be honoured on this platform, as decided by the
        per-stream resolvers rather than raised here.
    """  # ruff: ignore[docstring-extraneous-exception] - ValueError propagates from _resolve_output_binding.
    return _ResolvedStdio(
        stdin=_resolve_stdin_plan(stdin, output),
        stdout=_resolve_output_binding(STDOUT_STREAM, output.stdout),
        stderr=_resolve_output_binding(STDERR_STREAM, output.stderr),
    )


def _resolve_output_binding(
    stream: PipeStream,
    target: StdioTarget | None,
) -> _StdioBinding:
    """Resolve one output stream's target, defaulting to a library pipe.

    An unset target and an explicit ``StdioTarget.pipe()`` resolve the same
    way here. That is not a coincidence to preserve by accident: the execution
    layer is what decides whether a pipe is really wanted, by folding capture,
    echo, idle observation, and line observation into its ``consumes_*``
    properties, and a caller's explicit target only chooses *between* a pipe
    and something else.

    Parameters
    ----------
    stream : str
        ``"stdout"`` or ``"stderr"``.
    target : StdioTarget | None
        The caller's target, or ``None`` when unspecified.

    Returns
    -------
    _StdioBinding
        The resolved binding for that stream.
    """
    if target is None:
        return _StdioBinding(stream=stream, is_pipe=True)
    return _resolve_stdio_binding(stream, target)
