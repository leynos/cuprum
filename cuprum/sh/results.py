"""Structured text result types returned by ``cuprum.sh`` execution.

``CommandResult`` and ``PipelineResult`` describe the outcome of running a
single command or a pipeline of commands whose streams are decoded as text.
``CapturedResult`` narrows the text streams for a single command whose output
was captured.

The byte-exact counterparts live in :mod:`cuprum.sh.byte_results`, which this
module re-exports so that ``cuprum.sh.results`` remains a complete import path
for the result vocabulary. They are re-exported rather than redefined because a
second definition would be a second identity for the same public classes.
"""

# No ``from __future__ import annotations`` here: the result fields are public
# annotations that ``typing.get_type_hints`` must resolve, so they are evaluated
# eagerly and ``Program`` and ``RelayFallback`` are genuine runtime imports.
import dataclasses as dc

from cuprum.capture_policy import CapturedStream
from cuprum.echo_events import RelayFallback
from cuprum.program import Program

# Re-exported under the names they were defined with: the `as` form marks each
# as an intentional re-export, so the import checker sees a used name while a
# caller still reaches these classes through `cuprum.sh.results`.
from cuprum.sh.byte_results import (
    BytesCommandResult as BytesCommandResult,
)
from cuprum.sh.byte_results import (
    BytesPipelineResult as BytesPipelineResult,
)

__all__ = [
    "BytesCommandResult",
    "BytesPipelineResult",
    "CapturedResult",
    "CommandResult",
    "PipelineResult",
]


def _missing_text_streams(stdout: object, stderr: object) -> str:
    """Return the unavailable text stream names joined with ``and``."""
    missing = [
        name
        for name, value in (("stdout", stdout), ("stderr", stderr))
        if not isinstance(value, str)
    ]
    return " and ".join(missing)


@dc.dataclass(frozen=True, slots=True)
class CommandResult:
    """Structured result returned by command execution.

    Attributes
    ----------
    program:
        Program that was executed, as the catalogue identity policy was
        checked against.
    resolved_path:
        The executable that actually ran, when a scope bound ``program`` to a
        specific path. ``None`` when no binding applied, in which case the
        child ran under the catalogued name. Filesystem replacement after
        validation is not detected; see ``cuprum.executable_binding``.
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
        Categorical echo-failure and bounded-delivery loss records from this
        command's own streams, grouped in stdout-then-stderr order. A
        ``DROPPED`` record is emitted for each rejected chunk; ``UNDELIVERED``
        byte counts are grouped by stream at worker close. The ordering does
        not reconstruct chronological interleaving between streams. Empty
        when no output was lost or handled as an echo failure; these diagnostic
        records do not alter captured output or the child's exit status.
    captures:
        One record per captured stream, in stdout-then-stderr order, saying
        which policy the run applied, how many bytes the drain received, how
        many the policy discarded, and where the retained bytes are. Empty
        when the run captured nothing. A file-backed record reports
        ``storage`` of ``"file"``: its text is at ``path`` rather than on
        ``stdout``/``stderr``, which are ``None`` for that stream. The run
        removes the file before returning it unless the policy was built with
        ``keep_file=True``, so ``path`` is a locator for a kept spill and a
        record of what happened for one the run cleaned up.

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
    # ``kw_only`` for the same reason the measurements are: ``relay_fallbacks``
    # holds the seventh positional slot, which main established and
    # ``test_public_api`` pins, so a new field must not take a positional slot
    # beside it. Declared before ``relay_fallbacks`` rather than after it so
    # that field stays the declaration tail as well as the positional one;
    # ``test_public_api`` pins both, and a caller reading ``dc.fields()`` sees
    # the same ordering the generated signature uses.
    resolved_path: str | None = dc.field(default=None, kw_only=True)
    relay_fallbacks: tuple[RelayFallback, ...] = ()
    # ``kw_only`` for the same reason every field after ``stderr`` is: the
    # seventh positional slot belongs to ``relay_fallbacks``, which main
    # established and ``test_public_api`` pins. Declared *after* it rather than
    # before it, because a trailing declaration reads as the additive tail
    # this is; ``relay_fallbacks`` keeps the positional slot either way.
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

    def require_captured(self) -> "CapturedResult":
        """Narrow to ``CapturedResult`` after checking both text streams.

        Return this result unchanged if it is already captured; otherwise
        return a copy when both streams are strings. Empty output is ``""``.
        Request capture with ``RunOutputOptions(capture=True)``.

        Returns
        -------
        CapturedResult
            This result or a captured copy with the same fields.

        Raises
        ------
        ValueError
            If either stream is unavailable.

        Examples
        --------
        >>> from cuprum import CommandResult, ECHO
        >>> result = CommandResult(ECHO, (), 0, 1, "output", "")
        >>> result.require_captured().stdout
        'output'
        """
        stdout = self.stdout
        stderr = self.stderr
        missing = _missing_text_streams(stdout, stderr)
        if missing:
            was_were = "were" if " and " in missing else "was"
            msg = (
                f"{missing} {was_were} not captured as text; use "
                "RunOutputOptions(capture=True) to request captured output"
            )
            raise ValueError(msg)

        if isinstance(self, CapturedResult):
            return self

        result_fields = {
            field.name: getattr(self, field.name) for field in dc.fields(CommandResult)
        }
        return CapturedResult(**result_fields)


@dc.dataclass(frozen=True, slots=True)
class CapturedResult(CommandResult):
    """Single-command result with both decoded output streams present.

    Captured streams are always strings; an empty stream is ``""``. Other
    fields, positional layout, and keyword-only measurements match
    :class:`CommandResult`.

    Attributes
    ----------
    stdout, stderr:
        Captured text, or ``""`` when the child wrote nothing.

    Examples
    --------
    >>> from cuprum import CapturedResult, ECHO
    >>> CapturedResult(ECHO, (), 0, 1, "", "").stdout
    ''

    """

    stdout: str
    stderr: str

    def __post_init__(self) -> None:
        """Enforce the captured-stream invariant at every construction path."""
        missing = _missing_text_streams(self.stdout, self.stderr)
        if missing:
            msg = f"CapturedResult requires captured {missing} text"
            raise TypeError(msg)


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

    @property
    def captures(self) -> tuple[CapturedStream, ...]:
        """Capture metadata for the final pipeline stage's streams.

        This is the same stage :attr:`stdout` reads. Interior stages relay
        their stdout to the next stage; their stderr, when captured, is
        recorded on that stage's own ``stages[i].captures``.

        Returns
        -------
        tuple[CapturedStream, ...]
            The final stage's capture records, in stdout-then-stderr order,
            empty when that stage captured nothing.
        """
        return self.final.captures
