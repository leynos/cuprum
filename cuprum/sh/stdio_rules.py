"""The rules that police ``RunOutputOptions``' stdio targets.

`cuprum.sh.stdio` owns the ``StdioTarget`` vocabulary — what each variant
*means*, and which payloads it may carry. This module owns the other half: the
combinations the vocabulary cannot honour once a caller has assembled a set of
targets into real options. Every rule here reads a whole ``RunOutputOptions``,
because that is the smallest thing able to express a contradiction: two
answers to where stdin comes from, a stream told to be both captured and
redirected, or one file named for both streams.

The split is the same one ADR-007 records for the surrounding modules. Making a
target is a statement about a target; refusing a *combination* is a statement
about a run, and keeping the two apart is what lets each module stay small
enough to read in one sitting. ``cuprum.sh.output`` calls
:func:`_validate_stdio_targets` from its own ``__post_init__``, so the rules
still fire at construction rather than at spawn.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from cuprum._constants import PipeStream
    from cuprum.sh.output import RunOutputOptions
    from cuprum.sh.stdio import StdioTarget

__all__ = [
    "_validate_stdio_targets",
]

# The variants that describe a stream rather than naming a destination for data
# cuprum is given. Only these may stand for stdin.
_STDIN_KINDS: frozenset[str] = frozenset({"pipe", "inherit"})


def _validate_stdio_targets(options: RunOutputOptions) -> None:
    """Reject stdio targets this options object cannot honour.

    Three rules are enforced here, all at construction rather than at spawn,
    because all three are contradictions in the caller's *intent* rather than
    runtime conditions:

    Capture reads from a parent-side pipe. A stream bound to a file, a
    borrowed descriptor, or the parent's own stream has no such pipe, so
    "capture it" and "point it somewhere else" cannot both be satisfied. The
    same applies to echo, which mirrors what the consumer read. Either way the
    caller is asking for something cuprum cannot do, and silently doing one of
    them would be worse than refusing.

    Sharing one path between stdout and stderr opens the same file twice
    with two independent offsets, so the two streams interleave into the file
    in an order neither owns. Distinct paths, or a borrowed descriptor the
    caller manages, are the supported forms.

    ``stdin`` accepts only the two variants that describe the stream itself.
    ``path`` and ``fd`` are rejected not because cuprum could not open or use
    them — it redirects output that way — but because an input source has to
    carry the bytes as well as the destination, and both live on
    ``SafeCmd.run``'s ``stdin=`` argument: a payload resolves against the
    context's encoding and a producer is pulled during the run. Accepting an
    input descriptor here would create a second, competing way to say where
    stdin comes from, and the combination that lost would fail silently.

    The two accepted variants are not equally at home beside a source. An
    explicit ``inherit`` beside a payload or a producer is that same competing
    pair — both answer where stdin comes from — and is refused for the same
    reason; see :func:`_reject_contested_stdin`.

    Parameters
    ----------
    options : RunOutputOptions
        The options whose ``stdin``, ``stdout``, and ``stderr`` targets are
        checked.

    Raises
    ------
    ValueError
        If a target names a non-pipe for a stream that capture or echo
        requires, if ``stdin`` names a file or descriptor, if an inherited
        ``stdin`` is accompanied by a stdin source, or if stdout and stderr
        share one path.
    """  # ruff: ignore[docstring-extraneous-exception] - ValueError propagates from the _reject_* helpers.
    _reject_redirected_stdin(options.stdin)
    _reject_captured_redirection("stdout", options)
    _reject_captured_redirection("stderr", options)
    _reject_shared_owned_path(options)


def _reject_redirected_stdin(stdin_target: StdioTarget | None) -> None:
    """Refuse a stdin target that names a destination rather than a stream."""
    if stdin_target is None or stdin_target.kind in _STDIN_KINDS:
        return
    msg = (
        f"RunOutputOptions stdin cannot be redirected to "
        f"{stdin_target.kind!r}: pass the input itself as "
        f"stdin=StdinInput(...) or stdin=StdinStream(...) on the run call, "
        f"and use RunOutputOptions.stdin only to choose "
        f"{sorted(_STDIN_KINDS)}."
    )
    raise ValueError(msg)


def _reject_contested_stdin(
    stdin_target: StdioTarget | None,
    *,
    has_source: bool,
) -> None:
    """Refuse two answers to where the child's stdin comes from.

    ``RunOutputOptions.stdin`` and the run call's ``stdin=`` each claim to say
    what the child reads, so supplying both leaves no rule that does not
    discard something the caller wrote. The resolver reads them in one order —
    a source wins, and the target is only consulted when there is no source —
    which would make ``stdin=StdinInput(...)`` beside an explicit
    ``StdioTarget.inherit()`` deliver the payload and ignore the target.

    A ``pipe`` beside a source is not the same case: it names the pipe the
    payload is written through, so the source that won had already chosen the
    same thing. Only ``inherit`` is a genuine second answer, and it is the one
    this refuses.

    This is the one rule here that *cannot* run at construction, which is why
    it is called from the run's preparation rather than from
    ``RunOutputOptions.__post_init__``: the options are built before the run
    call, so whether a source was also supplied is not yet known.

    Parameters
    ----------
    stdin_target : StdioTarget | None
        The ``stdin`` target from the run's options, or ``None`` when unset.
    has_source : bool
        Whether the run call also supplied a stdin source.

    Raises
    ------
    ValueError
        If an inherited stdin target is accompanied by a stdin source.
    """
    if not has_source or stdin_target is None:
        return
    if stdin_target.kind != "inherit":
        return
    msg = (
        "RunOutputOptions stdin names StdioTarget.inherit(), but the run call "
        "also supplies a stdin source; the two disagree about where the "
        "child's input comes from. Pass the input or inherit the parent's "
        "stream, not both."
    )
    raise ValueError(msg)


def _reject_captured_redirection(
    name: PipeStream,
    options: RunOutputOptions,
) -> None:
    """Refuse a redirected stream that capture or echo must read."""
    target = options.stdout if name == "stdout" else options.stderr
    if target is None or target.kind == "pipe":
        return
    captures = options.capture or (
        options.echo_stdout if name == "stdout" else options.echo_stderr
    )
    if not captures:
        return
    msg = (
        f"RunOutputOptions {name} cannot be redirected to "
        f"{target.kind!r} while capture or echo is enabled: there is no "
        f"parent-side pipe to read. Set capture=False (and echo=False) "
        f"or leave {name} unset."
    )
    raise ValueError(msg)


def _reject_shared_owned_path(options: RunOutputOptions) -> None:
    """Refuse one file named for both streams cuprum would have to open."""
    if not _share_one_owned_path(options.stdout, options.stderr):
        return
    msg = (
        "RunOutputOptions stdout and stderr cannot share one path: each "
        "open starts at offset 0, so the two streams would interleave "
        "unpredictably. Use distinct paths, or StdioTarget.fd for a "
        "descriptor you manage."
    )
    raise ValueError(msg)


def _share_one_owned_path(
    stdout: StdioTarget | None,
    stderr: StdioTarget | None,
) -> bool:
    """Whether both streams name the same file for cuprum to own.

    Only ``path`` targets are cuprum's to open, so only they can collide: two
    ``fd`` targets naming one descriptor is the caller's own arrangement, and
    ``inherit`` carries no file at all. Two path targets are the same file when
    the targets compare equal, which for this variant means the same path.

    Parameters
    ----------
    stdout : StdioTarget | None
        The stdout target, or ``None`` when unspecified.
    stderr : StdioTarget | None
        The stderr target, or ``None`` when unspecified.

    Returns
    -------
    bool
        ``True`` when both name the same path target.
    """
    if stdout is None or stderr is None:
        return False
    return stdout.kind == "path" and stdout == stderr
