"""Captured/echoed output configuration for ``cuprum.sh`` runs.

``RunOutputOptions`` configures how a command or pipeline's stdout and stderr
are captured, mirrored, and optionally reframed through a presentation sink.
The ``cuprum.sh`` package re-exports the public name; the deprecated
``IOOptions`` alias and the flat ``capture``/``echo`` keyword resolution that
still reach this vocabulary live in :mod:`cuprum.sh._deprecated`.

The stdio target vocabulary lives in :mod:`cuprum.sh.stdio` and the rules
policing it in :mod:`cuprum.sh.stdio_rules`; ``StdioTarget`` is re-exported
here so ``cuprum.sh.output`` remains a usable import path for it.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum._constants import DEFAULT_ECHO_MAX_LINE_BYTES
from cuprum._idle_heartbeat import _validate_idle_options
from cuprum.echo_events import BrokenPipePolicy, _parse_broken_pipe_policy
from cuprum.sh._convenience_sink import (
    _validate_convenience_flags,
    synthesize_sink,
)
from cuprum.sh.stdio import (
    StdioTarget as StdioTarget,
)
from cuprum.sh.stdio_rules import (
    _validate_stdio_targets as _validate_stdio_targets,
)

# ``RunOutputOptions.sink`` is public and its annotation names ``sinks``, so
# ``typing.get_type_hints`` must resolve ``sinks`` from this module's globals —
# a type-checking block would break that. Ruff reads an annotation-only use as
# type-only and suggests moving it; the run-time sink construction that used to
# keep this import visibly run-time now lives in ``cuprum.sh._convenience_sink``.
from cuprum.sinks import base as sinks  # ruff: ignore[typing-only-first-party-import]

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.lines import LineHook

__all__ = [
    "RunOutputOptions",
    "StdioTarget",
]


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
    broken_pipe_policy : BrokenPipePolicy | str, keyword-only, default=STRICT
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
        device is never mistaken for a closed reader. The field is keyword-only
        because it was inserted mid-list: accepting it positionally would have
        taken ``on_line``'s slot and shifted every field after it.
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
    stdin : StdioTarget | None, default=None
        Where the child's standard input is bound, for the two variants that
        describe the stream itself. ``StdioTarget.pipe()`` asks for a
        library-owned pipe that nothing writes to, so a child reading stdin
        sees EOF instead of the parent's terminal; ``StdioTarget.inherit()``
        asks for the parent's stdin, which is the default. The actual input is
        supplied through ``SafeCmd.run``'s ``stdin=`` argument --- a
        ``StdinInput`` payload or a ``StdinStream`` producer --- and either
        form implies a pipe. A ``path`` or ``fd`` target is rejected here: an
        input descriptor belongs to the source, not to these options.
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
    # Keyword-only: inserted mid-list, it would otherwise take the position of
    # ``on_line`` and shift every field after it, silently re-binding the
    # positional arguments existing callers already pass.
    broken_pipe_policy: BrokenPipePolicy | str = dc.field(
        default=BrokenPipePolicy.STRICT,
        kw_only=True,
    )
    on_line: LineHook | None = None
    idle_after: float | None = None
    on_idle: cabc.Callable[[float, float], None] | None = None
    sink: sinks.OutputSink | None = None
    group: bool = False
    annotate_failure: bool = False
    # The three standard streams default to ``None``. ``stdin`` then inherits
    # the parent's stream; ``stdout`` and ``stderr`` resolve to a pipe when
    # capture, echo, idle reporting, or line observation needs one, and to
    # ``/dev/null`` when it does not --- never to the parent's own descriptor,
    # which would echo an unread child into the caller's terminal. Naming a
    # target explicitly overrides that choice; see ``StdioTarget``. ``stdin``
    # accepts only the two variants that describe the *pipe* itself ---
    # ``pipe()`` and ``inherit()`` --- because a file or a borrowed descriptor
    # for stdin belongs in a ``StdinInput``/``StdinStream`` source, not here.
    stdin: StdioTarget | None = None
    stdout: StdioTarget | None = None
    stderr: StdioTarget | None = None
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
        _validate_stdio_targets(self)
        synthesize_sink(self)

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

    @property
    def resolved_broken_pipe_policy(self) -> BrokenPipePolicy:
        """The policy behind the declared ``BrokenPipePolicy | str`` field.

        ``__post_init__`` normalizes the field, but the declared type stays
        wide because a caller may spell the policy as a string. This is the
        narrow view the execution layer reads, so it never has to parse.

        Returns
        -------
        BrokenPipePolicy
            The normalized policy member, never a string and never ``None``.
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
