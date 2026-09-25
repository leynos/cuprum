"""Captured/echoed output configuration for ``cuprum.sh`` runs.

``RunOutputOptions`` (and its deprecated ``IOOptions`` alias) configure how a
command or pipeline's stdout and stderr are captured, mirrored, and
optionally reframed through a presentation sink. This module also hosts the
deprecated flat ``capture``/``echo`` keyword resolution used by
``Pipeline.run``/``run_sync``. The ``cuprum.sh`` package re-exports the
public names.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ
import warnings

from cuprum._constants import DEFAULT_ECHO_MAX_LINE_BYTES
from cuprum._idle_heartbeat import _validate_idle_options
from cuprum.echo_events import BrokenPipePolicy, _parse_broken_pipe_policy

# ``GitHubActionsSink`` comes from the same package surface rather than its
# ``github_actions`` submodule: the package publishes it in ``__all__``, and the
# convenience flags synthesize it as part of this class's documented contract.
from cuprum.sinks import GitHubActionsSink

# ``RunOutputOptions.sink`` is public, so ``sinks`` must resolve at runtime too.
from cuprum.sinks import base as sinks

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.lines import LineHook

__all__ = [
    "IOOptions",
    "RunOutputOptions",
]


def _validate_convenience_flags(options: RunOutputOptions) -> None:
    """Reject non-``bool`` values for the convenience opt-in flags.

    Both flags gate whether workflow commands are written, so a value that
    merely coerces to a truthy bool — the integer ``1``, say — would frame a
    run on the strength of something the caller never documented as a flag.
    ``max_echo_line_bytes`` draws the same line for the same reason.

    Parameters
    ----------
    options : RunOutputOptions
        The options whose ``group`` and ``annotate_failure`` are checked.

    Raises
    ------
    ValueError
        If either flag is not a ``bool``.
    """
    for name, value in (
        ("group", options.group),
        ("annotate_failure", options.annotate_failure),
    ):
        if not isinstance(value, bool):
            msg = f"RunOutputOptions {name} must be a bool, got {value!r}"
            # Issue #375 expressly specifies ValueError for these flags.
            raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]


@dc.dataclass(frozen=True, slots=True)
class RunOutputOptions:
    """Configure captured and mirrored command output.

    Parameters
    ----------
    capture : bool, default=True
        Store stdout and stderr on the returned result. Capture is independent
        of echoing, so a captured stream can remain silent and an echoed stream
        can be left uncaptured.
    echo : bool, default=False
        Shorthand for enabling both ``echo_stdout`` and ``echo_stderr`` unless
        either stream has an explicit override.
    echo_stdout : bool | None, default=None
        Whether stdout is mirrored to the execution context's stdout sink.
        ``None`` inherits ``echo``.
    echo_stderr : bool | None, default=None
        Whether stderr is mirrored to the execution context's stderr sink.
        ``None`` inherits ``echo``.
    max_echo_line_bytes : int | None, default=64 * 1024
        Inclusive byte bound for every echoed line, including its retained
        bytes, truncation marker, and terminator. ``None`` restores unbounded,
        chunk-for-chunk mirroring; captured output always remains complete.
    broken_pipe_policy : BrokenPipePolicy | str, default=BrokenPipePolicy.STRICT
        How echoing responds when a presentation sink reports
        ``BrokenPipeError``, which is what a destination that has closed under
        the run looks like from inside the drain. ``STRICT`` propagates the
        error and aborts the run, preserving the behaviour of every caller
        that does not name a policy. ``BEST_EFFORT`` disables echoing for the
        affected stream only, so capture, line observation, and child reaping
        continue and the caller still receives a result; the transition is
        reported once through ``CommandResult.relay_fallbacks``, the echo
        observation channel, and a structured ``cuprum.stream`` warning.
        Either the member or its string value is accepted, and anything else
        raises ``ValueError``. Only ``BrokenPipeError`` is affected: any other
        sink ``OSError`` propagates under both policies, so an unreachable
        device is never mistaken for a closed reader.
    on_line : LineHook | None, default=None
        Optional synchronous callback invoked once per decoded output line
        with a ``LineEvent`` carrying the stream name, the monotonic seconds
        since the command started, and the line text. Independent of
        ``capture`` and ``echo``; lines are delivered in arrival order per
        stream. Lines are observed on the Python pathway, so the Rust
        fast-path dispatcher stays out of the way whenever this is set.
    idle_after : float | None, default=None
        Seconds of silence, measured across both streams, before the run
        reports that it is still running. ``None`` disables idle reporting and
        costs nothing: no watchdog, no timer, no extra pipe. The interval must
        be finite and strictly positive. Reporting describes the absence of
        observed output — never a deadlock diagnosis — and can neither
        terminate the child nor extend its timeout.
    on_idle : cabc.Callable[[float, float], None] | None, default=None
        Synchronous ``(elapsed_total, elapsed_idle)`` callback, in seconds,
        invoked once per idle interval in place of the built-in stderr
        keepalive. It must not block for long: it runs on the run's own event
        loop. Requires ``idle_after``.
    sink : sinks.OutputSink | None, default=None
        Optional presentation adapter (:mod:`cuprum.sinks` protocol). When
        given, it may reframe the parent-facing output of this run (for
        example, a GitHub Actions group); the default ``None`` keeps the
        plain two-stream behaviour. An adapter is inactive for a run when it
        declines activation, in which case output is unchanged.
    group : bool, default=False
        Convenience opt-in for a GitHub Actions collapsible log group around
        this run's echoed output. It is a shim over ``sink``: setting it
        constructs ``GitHubActionsSink(emit_group=True)`` and stores it in
        ``sink`` when the caller supplied none, so the framing, the
        stop-commands lease that shields it from child output, and the
        workflow-command syntax all stay inside the adapter and the execution
        layer learns nothing new. A pipeline emits one group for the whole
        pipeline, not one per stage. Values other than ``bool`` raise
        ``ValueError``.
    annotate_failure : bool, default=False
        Convenience opt-in for a single ``::error::`` annotation when the run
        ends in a non-zero exit, a timeout, or an error. Synthesizes
        ``GitHubActionsSink(emit_annotation=True)`` the same way ``group`` does,
        and composes with it: either flag alone synthesizes the adapter with
        only its own half enabled. Values other than ``bool`` raise
        ``ValueError``.

    Notes
    -----
    The two convenience flags reuse the same environment gate as any other
    sink: the adapter they construct carries no ``force``, so they take effect
    only when the parent process runs on GitHub Actions
    (``GITHUB_ACTIONS == "true"``). Outside that environment they synthesize a
    sink that declines activation and the run keeps its plain destinations
    byte-for-byte — which is the intended behaviour, not a failure to apply
    them. Callers who want the framing locally, on a non-standard runner, or
    through a custom destination or title pass ``sink=`` explicitly, possibly
    with ``force=True``.

    An explicit ``sink`` always wins: when one is supplied the flags are
    recorded but ignored, and no adapter is synthesized. Put the sink on a
    new ``RunOutputOptions`` or use ``dataclasses.replace`` when adapting
    shared options; ``run`` methods do not override ``output.sink`` per call.
    Passing a sink obtained from another options object to a new
    ``RunOutputOptions(sink=...)`` also makes it explicit, even when the sink
    was originally synthesized by the convenience flags.
    Replacing either flag on flag-generated options rebuilds their adapter to
    match the new values, while an explicitly supplied sink remains untouched.

    The default GitHub Actions sink does not serialize overlapping sessions.
    Do not run grouped commands concurrently when they write to the same parent
    stderr; run them sequentially so their workflow frames cannot interleave.

    Workflow commands are written to the parent's stderr by default. Neither
    flag changes capture, exit codes, or the returned result.

    Examples
    --------
    >>> options = RunOutputOptions(capture=True, echo=True)
    >>> options.resolved_echo
    (True, True)
    >>> RunOutputOptions(capture=True, echo=True, echo_stdout=False).resolved_echo
    (False, True)
    >>> RunOutputOptions(capture=False, idle_after=30.0).capture
    False
    >>> RunOutputOptions(group=True).sink is not None
    True
    >>> RunOutputOptions().sink is None
    True
    """

    capture: bool = True
    echo: bool = False
    echo_stdout: bool | None = None
    echo_stderr: bool | None = None
    max_echo_line_bytes: int | None = DEFAULT_ECHO_MAX_LINE_BYTES
    broken_pipe_policy: BrokenPipePolicy | str = BrokenPipePolicy.STRICT
    on_line: LineHook | None = None
    idle_after: float | None = None
    on_idle: cabc.Callable[[float, float], None] | None = None
    sink: sinks.OutputSink | None = None
    group: bool = False
    annotate_failure: bool = False
    # `dataclasses.replace` forwards init fields. Keep the generated identity
    # separate so reusing its sink in a new options object remains explicit.
    _synthesized_sink: sinks.OutputSink | None = dc.field(
        default=None,
        repr=False,
        compare=False,
        kw_only=True,
    )

    def __post_init__(self) -> None:
        """Resolve the echo shorthand and synthesize a sink from the flags."""
        object.__setattr__(
            self,
            "echo_stdout",
            self.echo if self.echo_stdout is None else self.echo_stdout,
        )
        object.__setattr__(
            self,
            "echo_stderr",
            self.echo if self.echo_stderr is None else self.echo_stderr,
        )
        # Stored normalized, so the schedule's arithmetic sees the float the
        # contract promises rather than whatever coerced to one here.
        object.__setattr__(
            self,
            "idle_after",
            _validate_idle_options(self.idle_after, self.on_idle),
        )
        # Normalized rather than merely checked, so the drain compares against
        # a member even when the caller spelled the policy as a string, and an
        # unknown value fails here rather than after a child has spawned.
        object.__setattr__(
            self,
            "broken_pipe_policy",
            _parse_broken_pipe_policy(self.broken_pipe_policy),
        )
        _validate_convenience_flags(self)
        self._synthesize_sink_from_flags()

        if self.max_echo_line_bytes is None:
            return
        bound = self.max_echo_line_bytes
        is_positive_int = isinstance(bound, int) and not isinstance(bound, bool)
        if not is_positive_int or bound <= 0:
            msg = (
                "RunOutputOptions max_echo_line_bytes must be a positive "
                f"integer or None, got {bound!r}"
            )
            raise ValueError(msg)

    def _synthesize_sink_from_flags(self) -> None:
        """Materialize the convenience flags as this run's presentation sink.

        Both flags are spelled as a configuration of the existing
        :class:`~cuprum.sinks.GitHubActionsSink` rather than as new framing
        code, which is what keeps workflow-command syntax inside the adapter.
        The synthesized sink carries no ``force``, so it stays subject to the
        adapter's own ``GITHUB_ACTIONS`` gate. Private identity metadata tracks
        the adapter created by this options object so
        :func:`dataclasses.replace` can resynthesize it when flags change.
        An adapter supplied to a new options object remains explicit regardless
        of its concrete type.
        """
        is_synthesized_sink = self.sink is self._synthesized_sink
        if self.sink is not None and not is_synthesized_sink:
            object.__setattr__(self, "_synthesized_sink", None)
            return
        if not (self.group or self.annotate_failure):
            if is_synthesized_sink:
                object.__setattr__(self, "sink", None)
            object.__setattr__(self, "_synthesized_sink", None)
            return
        synthesized_sink = GitHubActionsSink(
            emit_group=self.group,
            emit_annotation=self.annotate_failure,
        )
        object.__setattr__(
            self,
            "sink",
            synthesized_sink,
        )
        object.__setattr__(self, "_synthesized_sink", synthesized_sink)

    @property
    def resolved_broken_pipe_policy(self) -> BrokenPipePolicy:
        """The policy behind the declared ``BrokenPipePolicy | str`` field.

        ``__post_init__`` normalizes the field, but the declared type stays
        wide because a caller may spell the policy as a string. This is the
        narrow view the execution layer reads, so it never has to parse.
        """
        return _parse_broken_pipe_policy(self.broken_pipe_policy)

    @property
    def resolved_echo(self) -> tuple[bool, bool]:
        """The resolved ``(echo_stdout, echo_stderr)`` gates.

        ``__post_init__`` fills any ``None`` per-stream field from ``echo``,
        so the returned pair is always concrete and reflects an explicit
        per-stream override where one was supplied.
        """
        # The dataclass is frozen and ``__post_init__`` resolves both fields,
        # but the declared field types stay ``bool | None``; the resolved pair
        # is the constructor's contract, not something the declared types can
        # express to the type checker.
        return (self.echo_stdout, self.echo_stderr)  # ty: ignore[invalid-return-type]


@dc.dataclass(frozen=True, slots=True)
class IOOptions(RunOutputOptions):
    """Deprecated alias for command output stream options."""

    def __post_init__(self) -> None:
        """Resolve the inherited options, then warn about the deprecated alias."""
        # Zero-arg ``super()`` breaks under ``slots=True``: the decorator
        # rebuilds the class, so the method's ``__class__`` cell refers to the
        # pre-rebuild class and the instance fails the ``super`` type check.
        RunOutputOptions.__post_init__(self)
        warnings.warn(
            "IOOptions is deprecated; use RunOutputOptions instead",
            DeprecationWarning,
            stacklevel=2,
        )


class _DeprecatedOutputFlags(typ.TypedDict, total=False):
    """Deprecated flat ``capture``/``echo`` flags for ``Pipeline.run``."""

    capture: bool
    echo: bool


def _resolve_pipeline_output(
    output: RunOutputOptions | None,
    flags: _DeprecatedOutputFlags,
) -> RunOutputOptions:
    """Resolve pipeline output options, deprecating flat ``capture``/``echo``."""
    # Callers forward their ``Unpack[_DeprecatedOutputFlags]`` kwargs verbatim,
    # so the parameter keeps the precise ``TypedDict`` surface. Unknown keys
    # can still arrive at runtime (a ``TypedDict`` is open), and are rejected
    # here to preserve the strict keyword surface.
    unknown = set(flags) - {"capture", "echo"}
    if unknown:
        joined = ", ".join(sorted(unknown))
        msg = f"Pipeline.run/run_sync got unexpected keyword arguments: {joined}"
        raise TypeError(msg)
    if not flags:
        return output or RunOutputOptions()
    if output is not None:
        # Reject combining the deprecated flat flags with ``output``: the
        # caller's intent would otherwise be ambiguous.
        msg = "Pass either 'output' or the deprecated 'capture'/'echo' flags, not both"
        raise ValueError(msg)
    warnings.warn(
        "Pipeline.run/run_sync 'capture' and 'echo' keyword arguments are "
        "deprecated; pass output=RunOutputOptions(...) instead",
        DeprecationWarning,
        stacklevel=3,
    )
    return RunOutputOptions(
        capture=flags.get("capture", True),
        echo=flags.get("echo", False),
    )
