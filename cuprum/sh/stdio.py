"""Stdio target vocabulary and the rules that police it.

`StdioTarget` names where one of a child's standard streams is bound, and this
module owns the validation that rejects the combinations cuprum cannot honour.
Those rules live beside the type rather than in ``cuprum.sh.output`` because
every one of them is a statement about a target: whether a variant carries a
path cuprum must open, and whether two variants collide. The options class that
consumes them stays in ``cuprum.sh.output``.

The four variants differ in *ownership*:

- :meth:`StdioTarget.pipe` --- a library-owned pipe cuprum creates and closes.
- :meth:`StdioTarget.inherit` --- the parent's stream, passed straight through.
- :meth:`StdioTarget.path` --- a file cuprum opens before the spawn and closes
  immediately after it, so the descriptor never outlives the run.
- :meth:`StdioTarget.fd` --- a borrowed descriptor or file object the caller
  owns and cuprum never closes.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    from cuprum._constants import PipeStream
    from cuprum.sh.output import RunOutputOptions

__all__ = [
    "StdioTarget",
]


type _StdioKind = typ.Literal["pipe", "inherit", "path", "fd"]

_STDIO_KINDS: frozenset[str] = frozenset({"pipe", "inherit", "path", "fd"})

# The variants that describe a stream rather than naming a destination for data
# cuprum is given. Only these may stand for stdin.
_STDIN_KINDS: frozenset[str] = frozenset({"pipe", "inherit"})

# The variants that name a binding and carry no payload of their own. The
# remaining two each require one: a path for ``path``, a resource for ``fd``.
_PAYLOADLESS_KINDS: frozenset[str] = frozenset({"pipe", "inherit"})


def _reject_unknown_kind(kind: str) -> None:
    """Refuse a variant name the vocabulary does not define.

    Parameters
    ----------
    kind : str
        The variant name to check.

    Raises
    ------
    ValueError
        If *kind* is not one of the four defined variants.
    """
    if kind in _STDIO_KINDS:
        return
    msg = f"StdioTarget kind must be one of {sorted(_STDIO_KINDS)}, got {kind!r}"
    raise ValueError(msg)


def _reject_unexpected_payload(
    kind: str,
    value: Path | int | typ.IO[bytes] | typ.IO[str] | None,
) -> None:
    """Refuse a variant carrying the shape its own kind cannot honour.

    Only the two payloadless kinds are checked: their payload must be absent,
    so anything present is a caller who meant a different variant. The two
    payload-bearing kinds are validated when their payload is normalised, and
    a ``str`` is caught there rather than here.

    Parameters
    ----------
    kind : str
        The variant name, already known to be one of the four.
    value : Path | int | IO[bytes] | IO[str] | None
        The payload the variant was built with.

    Raises
    ------
    ValueError
        If a payloadless kind was given a payload.
    """
    if kind not in _PAYLOADLESS_KINDS or value is None:
        return
    msg = f"StdioTarget.{kind}() takes no payload, got {value!r}"
    raise ValueError(msg)


def _normalise_payload(target: StdioTarget) -> None:
    """Check a payload-bearing variant and store what the kind requires.

    An ``fd`` target needs a resource and cuprum keeps it as given. A ``path``
    target needs a path, and the target stores a ``Path`` whichever form it was
    given, so equality between two targets is equality of paths.

    Parameters
    ----------
    target : StdioTarget
        The target being initialised, whose ``kind`` is one of the four.

    Raises
    ------
    ValueError
        If the ``fd`` variant carries no resource, or the ``path`` variant
        carries something other than a path.
    """
    if target.kind == "fd":
        if target.value is None:
            msg = "StdioTarget.fd requires a descriptor or file object"
            raise ValueError(msg)
        return
    if target.kind != "path":
        return
    if not isinstance(target.value, str | Path):
        msg = f"StdioTarget.path requires a path, got {target.value!r}"
        # A wrong-shaped payload here is a caller error, not an internal type
        # fault, and the four variants are one vocabulary whose refusals all
        # read as ValueError; `StdioTarget.fd` above is refused the same way.
        raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]


@dc.dataclass(frozen=True, slots=True)
class StdioTarget:
    """Where a child's standard stream is bound.

    The four variants differ in *ownership*, which is the whole contract:

    - :meth:`pipe` — a library-owned pipe, the default for stdout and stderr
      whenever capture, echo, idle reporting, or line observation needs it.
      Cuprum creates it, consumes or writes it, and closes it.
    - :meth:`inherit` — the parent's stream, passed straight through. This is
      the default for stdin, and for stdout and stderr when nothing needs to
      read them. Cuprum touches nothing.
    - :meth:`path` — a file **cuprum owns**. The file is opened immediately
      before the spawn, its descriptor is handed to the child, and cuprum's
      copy is closed in a ``finally`` right after the spawn returns, so the
      descriptor never outlives the run. The caller names a path and never
      manages a descriptor. Parent and child write at independent offsets
      because the descriptor starts at offset 0, so redirecting two streams to
      one path interleaves them and is rejected.
    - :meth:`fd` — a **borrowed** descriptor or open file object. The caller
      owns it: cuprum never closes it, and a borrowed file object is flushed
      before spawn so buffered caller-side bytes reach the child. A caller who
      reuses one descriptor across two runs shares a single file offset, so
      the second run writes wherever the first left off.

    Instances are values: two targets built the same way compare equal, so
    ``RunOutputOptions`` stays value-like and ``dataclasses.replace`` behaves.
    """

    kind: _StdioKind = "pipe"
    value: Path | int | typ.IO[bytes] | typ.IO[str] | None = None

    def __post_init__(self) -> None:
        """Reject a variant that carries the wrong payload.

        Each kind's own requirement is checked by one helper below, so this
        method stays a dispatch: reject an undefined kind, refuse a payload the
        named kind cannot carry, then normalise what the kind does carry.
        """
        _reject_unknown_kind(self.kind)
        _reject_unexpected_payload(self.kind, self.value)
        _normalise_payload(self)

    @property
    def path_value(self) -> Path:
        """The path this target owns.

        Returns
        -------
        Path
            The file cuprum opens before spawn and closes immediately after.

        Raises
        ------
        ValueError
            If this target is not a ``path`` variant.
        """
        if self.kind != "path":
            msg = f"StdioTarget.{self.kind}() carries no path"
            raise ValueError(msg)
        return typ.cast("Path", self.value)

    @property
    def fd_value(self) -> int | typ.IO[bytes] | typ.IO[str]:
        """The borrowed descriptor or file object this target names.

        Returns
        -------
        int | IO[bytes] | IO[str]
            The caller-owned resource cuprum will never close.

        Raises
        ------
        ValueError
            If this target is not an ``fd`` variant.
        """
        if self.kind != "fd":
            msg = f"StdioTarget.{self.kind}() carries no descriptor"
            raise ValueError(msg)
        return typ.cast("int | typ.IO[bytes] | typ.IO[str]", self.value)

    @property
    def is_owned_path(self) -> bool:
        """Whether this target names a file cuprum must open and close."""
        return self.kind == "path"

    @staticmethod
    def pipe() -> StdioTarget:
        """Return the library-owned pipe variant.

        Returns
        -------
        StdioTarget
            A target selecting a pipe cuprum creates and owns.
        """
        return StdioTarget(kind="pipe")

    @staticmethod
    def inherit() -> StdioTarget:
        """Return the inherit-the-parent's-stream variant.

        Returns
        -------
        StdioTarget
            A target selecting the parent's own stream.
        """
        return StdioTarget(kind="inherit")

    @staticmethod
    def path(path: str | Path) -> StdioTarget:
        """Return the cuprum-owned file variant.

        Parameters
        ----------
        path : str | Path
            The file to open before spawn and close immediately afterwards.
            Cuprum does not create intermediate directories, so the parent
            must already exist.

        Returns
        -------
        StdioTarget
            A target naming the file cuprum will own for the run.
        """
        return StdioTarget(kind="path", value=Path(path))

    @staticmethod
    def fd(fd: int | typ.IO[bytes] | typ.IO[str]) -> StdioTarget:
        """Return the borrowed descriptor or file-object variant.

        Parameters
        ----------
        fd : int | IO[bytes] | IO[str]
            A descriptor or an open file object the *caller* owns. Cuprum
            never closes it; a file object is flushed immediately before the
            spawn so buffered caller-side bytes reach the child.

        Returns
        -------
        StdioTarget
            A target borrowing the caller's descriptor or file object.
        """
        return StdioTarget(kind="fd", value=fd)


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

    Parameters
    ----------
    options : RunOutputOptions
        The options whose ``stdin``, ``stdout``, and ``stderr`` targets are
        checked.

    Raises
    ------
    ValueError
        If a target names a non-pipe for a stream that capture or echo
        requires, if ``stdin`` names a file or descriptor, or if stdout and
        stderr share one path.
    """  # ruff: ignore[docstring-extraneous-exception] - ValueError propagates from the _reject_* helpers.
    _reject_redirected_stdin(options.stdin)
    _reject_captured_redirection("stdout", options)
    _reject_captured_redirection("stderr", options)
    _reject_shared_owned_path(options)


def _reject_redirected_stdin(stdin_target: StdioTarget | None) -> None:
    """Refuse a stdin target that names a destination rather than a stream.

    Parameters
    ----------
    stdin_target : StdioTarget | None
        The options'``stdin`` target, or ``None`` when unspecified.

    Raises
    ------
    ValueError
        If the target names a file or a descriptor instead of a stream.
    """
    if stdin_target is None or stdin_target.kind in _STDIN_KINDS:
        return
    msg = (
        f"RunOutputOptions stdin cannot be redirected to "
        f"{stdin_target.kind!r}: pass the input itself as "
        f"stdin=StdinInput(...) or stdin=StdinStream(...) on the run call, "
        f"which selects a pipe, and use RunOutputOptions.stdin only to "
        f"choose {sorted(_STDIN_KINDS)}."
    )
    raise ValueError(msg)


def _reject_captured_redirection(
    name: PipeStream,
    options: RunOutputOptions,
) -> None:
    """Refuse a redirected stream that capture or echo must read.

    Capture reads from a parent-side pipe, and a stream bound elsewhere has no
    such pipe; echo mirrors what the consumer read, so it fails the same way.
    A pipe target is untouched, and so is one that was left unset.

    Parameters
    ----------
    name : str
        Which stream to check: ``"stdout"`` or ``"stderr"``.
    options : RunOutputOptions
        The options holding the target and the capture and echo flags.

    Raises
    ------
    ValueError
        If the named stream is redirected while capture or echo is on.
    """
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
    """Refuse one file named for both streams cuprum would have to open.

    Parameters
    ----------
    options : RunOutputOptions
        The options whose two output targets are checked.

    Raises
    ------
    ValueError
        If both streams name the same path for cuprum to own.
    """
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
