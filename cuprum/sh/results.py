"""Structured result types returned by ``cuprum.sh`` execution.

``CommandResult`` and ``PipelineResult`` describe the outcome of running a
single command or a pipeline of commands whose streams are decoded as text.
``BytesCommandResult`` and ``BytesPipelineResult`` are their byte-exact
counterparts, returned by the ``run_bytes()`` entry points so that captured
output survives a round trip through values that are not valid UTF-8. The
``cuprum.sh`` package re-exports all four.
"""

# No ``from __future__ import annotations`` here: the result fields are public
# annotations that ``typing.get_type_hints`` must resolve, so they are evaluated
# eagerly and ``Program`` and ``RelayFallback`` are genuine runtime imports.
import dataclasses as dc

from cuprum.echo_events import RelayFallback
from cuprum.program import Program

__all__ = [
    "BytesCommandResult",
    "BytesPipelineResult",
    "CommandResult",
    "PipelineResult",
]


@dc.dataclass(frozen=True, slots=True)
class CommandResult:
    """Structured result returned by command execution.

    Attributes
    ----------
    program:
        Program that was executed.
    argv:
        Argument vector (excluding the program name) passed to the process.
    exit_code:
        Exit status reported by the process.
    pid:
        Process identifier; ``-1`` when unavailable.
    stdout:
        Captured standard output, or ``None`` when capture was disabled.
    stderr:
        Captured standard error, or ``None`` when capture was disabled.
    started_at:
        Wall-clock timestamp at which process execution started.
    duration:
        Monotonic process duration in seconds.
    max_rss_bytes:
        Peak resident set size of the executed child in bytes. Direct commands
        on Linux and macOS report this from the platform ``wait4`` call, which
        attributes the figure to that one child; Linux reports KiB and macOS
        bytes, both normalized to bytes here. It is never derived from the
        process-global ``RUSAGE_CHILDREN`` high-water mark, which cannot be
        attributed safely to one command. ``None`` on Windows, on platforms
        without the child-specific interface, and for every pipeline stage,
        whose concurrently reaped children cannot be separated.
    user_cpu_seconds:
        User CPU time consumed by the executed child in seconds. Direct
        commands on Linux and macOS report this from ``wait4``; elsewhere the
        aggregate ``RUSAGE_CHILDREN`` fallback may supply it, and those deltas
        are approximate under ``run_concurrent``. ``None`` on Windows, on
        platforms without child resource accounting, and for pipeline stages.
    system_cpu_seconds:
        System CPU time consumed by the executed child in seconds. Direct
        commands on Linux and macOS report this from ``wait4``; elsewhere the
        aggregate ``RUSAGE_CHILDREN`` fallback may supply it, and those deltas
        are approximate under ``run_concurrent``. ``None`` on Windows, on
        platforms without child resource accounting, and for pipeline stages.
    relay_fallbacks:
        Handled echo-disablement records from this command's own streams, one
        per affected drain in stdout-then-stderr order. The ordering does not
        reconstruct chronological interleaving between the two streams. Empty
        when no echo write was handled as failed, and an echo failure does not
        change ``exit_code`` or ``ok``.

    """

    program: Program
    argv: tuple[str, ...]
    exit_code: int
    pid: int
    stdout: str | None
    stderr: str | None
    # ``kw_only`` so the seventh positional slot stays ``relay_fallbacks``,
    # which main established and ``test_public_api`` pins. Without it the
    # measurements would take positional slots ahead of it and a
    # seven-argument call would silently bind a relay tuple into
    # ``started_at``. Declared after ``stderr`` so the measurements read
    # beside the other captured-output fields in the generated signature.
    started_at: float = dc.field(default=0.0, kw_only=True)
    duration: float = dc.field(default=0.0, kw_only=True)
    max_rss_bytes: int | None = dc.field(default=None, kw_only=True)
    user_cpu_seconds: float | None = dc.field(default=None, kw_only=True)
    system_cpu_seconds: float | None = dc.field(default=None, kw_only=True)
    relay_fallbacks: tuple[RelayFallback, ...] = ()

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
class PipelineResult:
    """Structured result returned by pipeline execution.

    Attributes
    ----------
    stages:
        Command results for each pipeline stage, in execution order. For stages
        whose stdout is streamed into the next stage, ``stdout`` is ``None``.
        The final stage carries captured stdout when enabled.
    failure_index:
        Index of the stage that triggered fail-fast termination, or ``None``
        when all stages completed successfully.

    """

    stages: tuple[CommandResult, ...]
    failure_index: int | None = None

    @property
    def final(self) -> CommandResult:
        """The result from the final pipeline stage.

        Returns
        -------
        CommandResult
            The last stage's result in execution order.
        """
        return self.stages[-1]

    @property
    def failure(self) -> CommandResult | None:
        """The stage that triggered fail-fast termination, if any.

        Returns
        -------
        CommandResult | None
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
    def stdout(self) -> str | None:
        """Captured output from the final pipeline stage.

        Returns
        -------
        str | None
            The final stage's captured standard output, or ``None`` when
            capture was disabled.
        """
        return self.final.stdout


@dc.dataclass(frozen=True, slots=True)
class BytesCommandResult:
    """Byte-exact result returned by the binary-output command entry points.

    The fields, defaults, and helpers mirror :class:`CommandResult`; only the
    captured-output fields differ, so a caller that switches entry point
    switches the static type of what it reads rather than the shape of the
    record. Bytes reach a caller untouched — no decoding, no surrogate escape —
    which is what lets every byte value survive the round trip.

    Attributes
    ----------
    program:
        Program that was executed.
    argv:
        Argument vector (excluding the program name) passed to the process.
    exit_code:
        Exit status reported by the process.
    pid:
        Process identifier; ``-1`` when unavailable.
    stdout:
        Captured standard output bytes, or ``None`` when capture was disabled.
        A stream that was consumed but produced nothing is ``b""``, which is
        distinguishable from the ``None`` of a run that never captured.
    stderr:
        Captured standard error bytes, with the same ``None``/``b""``
        distinction as ``stdout``.
    started_at:
        Wall-clock timestamp at which process execution started.
    duration:
        Monotonic process duration in seconds.
    max_rss_bytes:
        Peak resident set size of the executed child in bytes, under the same
        platform rules as :class:`CommandResult`.
    user_cpu_seconds:
        User CPU time consumed by the executed child in seconds, under the
        same platform rules as :class:`CommandResult`.
    system_cpu_seconds:
        System CPU time consumed by the executed child in seconds, under the
        same platform rules as :class:`CommandResult`.
    relay_fallbacks:
        Handled echo-disablement records from this command's own streams, in
        the order and with the meaning :class:`CommandResult` documents.

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
    relay_fallbacks: tuple[RelayFallback, ...] = ()

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
    """Byte-exact result returned by the binary-output pipeline entry points.

    Attributes
    ----------
    stages:
        Per-stage results in execution order. Stages whose stdout is streamed
        into the next stage carry ``stdout`` of ``None``; the final stage
        carries captured stdout bytes when capture is enabled.
    failure_index:
        Index of the stage that triggered fail-fast termination, or ``None``
        when all stages completed successfully.

    """

    stages: tuple[BytesCommandResult, ...]
    failure_index: int | None = None

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
