"""The shapes a capture tap is written against.

These are the pieces the drain and the accumulators must agree on before either
needs the other: the capability the drain requires, the finalized output it
receives, and the spill file teardown may have to remove. Holding them in a
leaf keeps the accumulators from importing each other and, more usefully, keeps
the file-backed accumulator's use of :class:`SpillHandle` from being a cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import pathlib

    from cuprum.capture_policy import CapturedStream

__all__ = [
    "CaptureFinal",
    "CaptureSink",
    "CaptureSinkRegistry",
    "SpillHandle",
]


class CaptureSink(typ.Protocol):
    """What the drain requires of an accumulator, whichever policy it applies.

    A structural type rather than a base class, so each accumulator stays a
    small standalone object with its own slots and the drain names the
    capability it uses rather than three concrete classes.
    """

    def absorb(self, chunk: bytes) -> None:
        """Take one raw chunk of the child's output."""
        ...

    def finalize(self) -> CaptureFinal:
        """Return the retained payload and the record describing it."""
        ...

    async def cleanup(self) -> None:
        """Release memory and remove any spill file this sink still owns."""
        ...


@dc.dataclass(slots=True)
class CaptureSinkRegistry:
    """The capture accumulators one pipeline run created, for teardown.

    A pipeline's spill files are normally removed by the result-construction
    site, which holds every drain result the run produced. That site is
    unreachable for any drain that was cancelled while awaiting its line sink:
    the drain re-raises without finalizing, so no result is built for it and
    the handle it would have carried never arrives. Two paths would then race
    to remove the file, and only one of them can be relied on.

    The registry closes that gap without a second drain-results channel. A
    stage's consumer is created here, so here is the only place that knows both
    that the consumer exists and which accumulator it will retain into; a stage
    whose consumer was created after the last successful registration is the
    mirror image, and cannot report a sink it never received.

    A registered sink is remembered once, by identity: a stage whose streams
    are already consumed holds the same accumulator a previous stage
    registered, and ``_FileAccumulator`` does not define value equality, so a
    membership test would register a backed stream twice and teardown would
    clean one sink once per stage that shared it. The identity test is written
    out rather than left to ``in`` so that stays true if an accumulator ever
    gains an ``__eq__``.
    """

    sinks: list[CaptureSink] = dc.field(default_factory=list)

    def register(self, sink: CaptureSink | None) -> None:
        """Record *sink* as this run's to clean up, once.

        Parameters
        ----------
        sink : CaptureSink | None
            The accumulator a newly created consumer will retain into, or
            ``None`` for a config that captures nothing.
        """
        if sink is None or any(sink is known for known in self.sinks):
            return
        self.sinks.append(sink)


@dc.dataclass(frozen=True, slots=True)
class CaptureFinal:
    """One accumulator's finalized output: payload, metadata, and spill handle.

    ``payload`` carries the same widened ``str | bytes`` the drain's own seam
    produces, which is what lets :class:`cuprum._stream_drain_finish._CapturedDrain`
    store it under its ``str | bytes | None`` annotation without a cast. An
    ``object`` here would have read as "anything goes" and forced every reader
    to narrow what the accumulator had already guaranteed.

    ``handle`` is the spill file this final is still responsible for, which is
    what makes cleanup reachable from teardown: the final travels out with the
    result, so a normal path removes the file from there, while a cancellation
    that never builds a result still holds the sink and removes it from
    :meth:`CaptureSink.cleanup`.
    """

    # ``None`` is a real answer, not an absent one: a spilled capture in text
    # mode deliberately retains nothing on the heap, so the caller reads the
    # file at ``capture.path`` instead. Only a byte-exact spill reads the file
    # back, because there the child's own bytes are what was asked for.
    payload: str | bytes | None
    capture: CapturedStream
    handle: SpillHandle | None = None


@dc.dataclass(slots=True)
class SpillHandle:
    """A spill file this run created, and so must remove.

    Mutable and shared by both paths that can reach it — result materialisation
    and teardown reconciliation — so closing is idempotent whichever runs
    first.
    """

    path: pathlib.Path
    # ``IO[bytes]`` rather than ``BufferedRandom``: the file comes from
    # ``NamedTemporaryFile(delete=False)``, whose typeshed type is
    # ``_TemporaryFileWrapper[bytes]`` — a proxy that forwards to the
    # underlying buffered file rather than being one. Typing this as the
    # concrete class would promise a ``seek``/``truncate`` surface nothing here
    # uses and that the wrapper does not actually declare.
    file: typ.IO[bytes]
    keep: bool
    closed: bool = False

    @property
    def owned(self) -> bool:
        """Whether this run is still responsible for removing the file.

        A kept file is closed but not unlinked, so it outlives the run by the
        caller's request; the metadata reports it as unowned either way.
        """
        return not self.keep

    async def close(self, *, remove: bool = True) -> None:
        """Flush, close, and — unless the caller kept it — remove the file.

        Parameters
        ----------
        remove : bool, default=True
            Whether to unlink the file. Cleanup passes ``False`` when the
            caller asked to keep it; the close still happens, so no descriptor
            outlives the run.
        """
        if not self.closed:
            self.closed = True
            with contextlib.suppress(OSError):
                self.file.close()
        if remove and not self.keep:
            await asyncio.to_thread(self.path.unlink, missing_ok=True)
