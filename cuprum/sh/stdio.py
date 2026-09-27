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
    from cuprum.sh.output import RunOutputOptions

__all__ = [
    "StdioTarget",
]


type _StdioKind = typ.Literal["pipe", "inherit", "path", "fd"]

_STDIO_KINDS: frozenset[str] = frozenset({"pipe", "inherit", "path", "fd"})


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
        """Reject a variant that carries the wrong payload."""
        if self.kind not in _STDIO_KINDS:
            msg = (
                f"StdioTarget kind must be one of {sorted(_STDIO_KINDS)}, "
                f"got {self.kind!r}"
            )
            raise ValueError(msg)
        if self.kind == "path":
            if not isinstance(self.value, str | Path):
                msg = f"StdioTarget.path requires a path, got {self.value!r}"
                raise ValueError(msg)
            object.__setattr__(self, "value", Path(self.value))
            return
        if self.kind == "fd":
            if self.value is None:
                msg = "StdioTarget.fd requires a descriptor or file object"
                raise ValueError(msg)
            return
        if self.value is not None:
            msg = f"StdioTarget.{self.kind}() takes no payload, got {self.value!r}"
            raise ValueError(msg)

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

    Two rules are enforced here, both at construction rather than at spawn,
    because both are contradictions in the caller's *intent* rather than
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

    Parameters
    ----------
    options : RunOutputOptions
        The options whose ``stdout`` and ``stderr`` targets are checked.

    Raises
    ------
    ValueError
        If a target names a non-pipe for a stream that capture or echo
        requires, or if stdout and stderr share one path.
    """
    for name, target in (("stdout", options.stdout), ("stderr", options.stderr)):
        if target is None or target.kind == "pipe":
            continue
        captures = options.capture or (
            options.echo_stdout if name == "stdout" else options.echo_stderr
        )
        if captures:
            msg = (
                f"RunOutputOptions {name} cannot be redirected to "
                f"{target.kind!r} while capture or echo is enabled: there is no "
                f"parent-side pipe to read. Set capture=False (and echo=False) "
                f"or leave {name} unset."
            )
            raise ValueError(msg)
    if _share_one_owned_path(options.stdout, options.stderr):
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
