"""Stdio target vocabulary: what each standard-stream binding *means*.

`StdioTarget` names where one of a child's standard streams is bound, and this
module owns the vocabulary and the per-variant rules: which payload each kind
carries, and how a ``path`` payload is normalized. The rules that police
*combinations* of targets — a stream both captured and redirected, one file
named for two streams, two answers to where stdin comes from — read a whole
``RunOutputOptions`` and live in :mod:`cuprum.sh.stdio_rules` instead. The
options class those rules validate stays in ``cuprum.sh.output``.

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

__all__ = [
    "StdioTarget",
]


type _StdioKind = typ.Literal["pipe", "inherit", "path", "fd"]

_STDIO_KINDS: frozenset[str] = frozenset({"pipe", "inherit", "path", "fd"})

# The variants that name a binding and carry no payload of their own. The
# remaining two each require one: a path for ``path``, a resource for ``fd``.
_PAYLOADLESS_KINDS: frozenset[str] = frozenset({"pipe", "inherit"})


def _reject_unknown_kind(kind: str) -> None:
    """Refuse a variant name the vocabulary does not define."""
    if kind in _STDIO_KINDS:
        return
    msg = f"StdioTarget kind must be one of {sorted(_STDIO_KINDS)}, got {kind!r}"
    raise ValueError(msg)


def _reject_unexpected_payload(
    kind: str,
    value: Path | int | typ.IO[bytes] | typ.IO[str] | None,
) -> None:
    """Refuse a payload given to a kind that carries none.

    Only the payloadless kinds are checked here; the two payload-bearing kinds
    are validated by ``_normalize_payload``.

    Raises
    ------
    ValueError
        If a payloadless kind was given a payload.
    """
    if kind not in _PAYLOADLESS_KINDS or value is None:
        return
    msg = f"StdioTarget.{kind}() takes no payload, got {value!r}"
    raise ValueError(msg)


def _normalize_payload(target: StdioTarget) -> None:
    """Check a payload-bearing variant and store what the kind requires.

    The ``path`` variant is normalized as well as checked: ``str`` and ``Path``
    are both accepted, and both are stored as a ``Path``, so two spellings of
    one file build equal targets. That matters beyond tidiness, because
    ``_share_one_owned_path`` decides whether stdout and stderr name the same
    file by comparing targets, and an unnormalized ``str`` would compare
    unequal to its own ``Path`` spelling and slip past the shared-path refusal.

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
    if not isinstance(target.value, Path):
        object.__setattr__(target, "value", Path(target.value))


@dc.dataclass(frozen=True, slots=True)
class StdioTarget:
    """Where a child's standard stream is bound.

    The four variants differ in *ownership*, which is the whole contract:

    - :meth:`pipe` — a library-owned pipe, the default for stdout and stderr
      whenever capture, echo, idle reporting, or line observation needs it.
      Cuprum creates it, consumes or writes it, and closes it.
    - :meth:`inherit` — the parent's stream, passed straight through. This is
      the default for stdin, and the explicit way to let an unobserved stdout
      or stderr through: an unset stdout or stderr whose pipe nobody reads
      resolves to ``/dev/null`` instead, so the child cannot write into the
      caller's terminal. Cuprum touches nothing either way.
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

        Each kind's own requirement is checked by one helper, so this method
        stays a dispatch: reject an undefined kind, refuse a payload the named
        kind cannot carry, then normalize what the kind does carry.
        """
        _reject_unknown_kind(self.kind)
        _reject_unexpected_payload(self.kind, self.value)
        _normalize_payload(self)

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
