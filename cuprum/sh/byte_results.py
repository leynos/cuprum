"""Byte-exact result types returned by the ``run_bytes()`` entry points.

``BytesCommandResult`` and ``BytesPipelineResult`` are the byte-exact
counterparts of :class:`~cuprum.sh.results.CommandResult` and
:class:`~cuprum.sh.results.PipelineResult`. They exist so captured output
survives a round trip through values that are not valid UTF-8: bytes reach a
caller untouched, with no decoding and no surrogate escape.

The module is split from :mod:`cuprum.sh.results` for the 400-line module
ceiling, and the boundary is the one the types already draw: the text records
report decoded output and the byte records never decode, so no piece of
behaviour straddles the two. ``cuprum.sh`` re-exports both classes from here,
and ``cuprum.sh.results`` keeps their import path working.

Like the text records, this module omits ``from __future__ import
annotations``: the fields are public annotations that ``typing.get_type_hints``
must resolve, so they are evaluated eagerly.
"""

import dataclasses as dc

from cuprum.capture_policy import CapturedStream
from cuprum.echo_events import RelayFallback
from cuprum.program import Program

__all__ = [
    "BytesCommandResult",
    "BytesPipelineResult",
]


@dc.dataclass(frozen=True, slots=True)
class BytesCommandResult:
    """Byte-exact result returned by the binary-output command entry points.

    Fields and defaults mirror :class:`~cuprum.sh.results.CommandResult`, but
    ``require_captured()`` is available only on text result types. Captured
    bytes remain untouched, so every byte value survives the round trip without
    decoding or surrogate escapes.

    Attributes
    ----------
    stdout:
        Captured bytes. ``b""`` means captured and empty; ``None`` means
        capture was disabled.
    stderr:
        Captured bytes, with the same ``b""``/``None`` distinction as
        ``stdout``.
    captures:
        One record per captured stream, in stdout-then-stderr order, with the
        meaning :class:`~cuprum.sh.results.CommandResult` documents. A byte run
        has no text to point at, so these records are the only way it can
        report that a policy discarded bytes, that a stream spilled, or that a
        storage call failed: the payload alone cannot tell a complete capture
        from a shortened one. Empty when the run captured nothing.

    """

    program: Program
    argv: tuple[str, ...]
    exit_code: int
    pid: int
    stdout: bytes | None
    stderr: bytes | None
    # Kept in step with ``CommandResult``: ``kw_only`` holds the seventh
    # positional slot for ``relay_fallbacks`` so a seven-argument call cannot
    # bind a measurements default into the relay tuple.
    started_at: float = dc.field(default=0.0, kw_only=True)
    duration: float = dc.field(default=0.0, kw_only=True)
    max_rss_bytes: int | None = dc.field(default=None, kw_only=True)
    user_cpu_seconds: float | None = dc.field(default=None, kw_only=True)
    system_cpu_seconds: float | None = dc.field(default=None, kw_only=True)
    resolved_path: str | None = dc.field(default=None, kw_only=True)
    relay_fallbacks: tuple[RelayFallback, ...] = ()
    # ``kw_only`` so ``relay_fallbacks`` keeps the seventh positional slot, as
    # it does in ``CommandResult``; declared after it so the two classes read
    # the same way.
    captures: tuple[CapturedStream, ...] = dc.field(default=(), kw_only=True)

    @property
    def ok(self) -> bool:
        """Whether the command exited successfully.

        Returns
        -------
        bool
            ``True`` exactly when ``exit_code`` is zero.
        """
        return self.exit_code == 0


@dc.dataclass(frozen=True, slots=True)
class BytesPipelineResult:
    """Binary-output pipeline result with ``PipelineResult`` semantics.

    Attributes
    ----------
    stages:
        Per-stage byte results in execution order. Interior ``stdout`` is
        ``None``; the final stage carries captured bytes when enabled.
    failure_index:
        Index of the stage that triggered fail-fast termination, or ``None``
        when all stages completed successfully.

    """

    stages: tuple[BytesCommandResult, ...]
    failure_index: int | None = None

    @property
    def captures(self) -> tuple[CapturedStream, ...]:
        """The final stage's capture records.

        Interior stages relay their stdout to the next stage; their stderr,
        when captured, is recorded on that stage's own
        ``stages[i].captures``.

        Returns
        -------
        tuple[CapturedStream, ...]
            One record per stream the final stage captured, in
            stdout-then-stderr order, with the meaning
            :class:`~cuprum.sh.results.CommandResult` documents; empty when it
            captured none.
        """
        return self.final.captures

    @property
    def final(self) -> BytesCommandResult:
        """The result from the final pipeline stage.

        Returns
        -------
        BytesCommandResult
            The last stage's result in execution order.
        """
        return self.stages[-1]

    @property
    def failure(self) -> BytesCommandResult | None:
        """The stage that triggered fail-fast termination, if any.

        Returns
        -------
        BytesCommandResult | None
            The failing stage result, or ``None`` when no stage triggered
            fail-fast termination.
        """
        if self.failure_index is None:
            return None
        return self.stages[self.failure_index]

    @property
    def ok(self) -> bool:
        """Whether every pipeline stage exited successfully.

        Returns
        -------
        bool
            ``True`` when every stage result is successful; otherwise
            ``False``.
        """
        return all(stage.ok for stage in self.stages)

    @property
    def stdout(self) -> bytes | None:
        """Captured output from the final pipeline stage.

        Returns
        -------
        bytes | None
            The final stage's captured standard output bytes, or ``None`` when
            capture was disabled.
        """
        return self.final.stdout
