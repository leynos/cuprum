"""Sink implementations for tee-path profiling scenarios."""

from __future__ import annotations

import contextlib
import io
import os
import pathlib as pth
import typing as typ

from benchmarks.pty_blackhole import PtyBlackhole

if typ.TYPE_CHECKING:
    import collections.abc as cabc

type SinkKind = typ.Literal["devnull", "text_blackhole", "pty_blackhole"]


class TextBlackhole(io.TextIOBase, typ.IO[str]):
    """Text sink that accepts strings without exposing a byte buffer.

    ``typing.IO[str]`` is a nominal base rather than a protocol, so it is
    listed explicitly: ``ExecutionContext.stdout_sink`` is declared as
    ``IO[str]`` and ``io.TextIOBase`` alone is not a subtype of it.
    ``TextIOBase`` precedes it in the MRO, so every method is served by the
    concrete stdlib implementation.
    """

    @typ.override
    def writable(self) -> bool:
        """Return that this stream accepts writes.

        Returns
        -------
        bool
            Always ``True``; the sink accepts writes.
        """
        return True

    @typ.override
    def write(self, s: str) -> int:
        """Discard text and report the accepted character count.

        Parameters
        ----------
        s : str
            The text to accept and discard; a non-``str`` value raises
            ``TypeError``.

        Returns
        -------
        int
            The number of characters accepted.

        Raises
        ------
        TypeError
            If ``s`` is not a ``str``.
        """
        if not isinstance(s, str):
            msg = f"expected str, got {type(s).__name__}"
            raise TypeError(msg)
        return len(s)

    def flush(self) -> None:
        """Flush the blackhole sink."""


@contextlib.contextmanager
def open_sink(
    kind: SinkKind,
    *,
    encoding: str,
    errors: str,
) -> cabc.Iterator[typ.IO[str]]:
    """Open a profiling sink by kind.

    Parameters
    ----------
    kind:
        Sink variant: ``"devnull"`` (OS null device), ``"text_blackhole"``
        (character counter), or ``"pty_blackhole"`` (pseudo-terminal drained
        by a daemon thread).
    encoding:
        Text encoding forwarded to the underlying stream or PTY slave.
    errors:
        Error-handling scheme forwarded to the underlying stream.

    Yields
    ------
    IO[str]
        Context-managed writable text stream; resources are released on exit.
    """
    if kind == "devnull":
        with pth.Path(os.devnull).open("w", encoding=encoding, errors=errors) as stream:
            yield stream
        return
    if kind == "text_blackhole":
        yield TextBlackhole()
        return
    if kind == "pty_blackhole":
        with PtyBlackhole(encoding=encoding, errors=errors) as stream:
            yield stream
        return
    typ.assert_never(kind)
