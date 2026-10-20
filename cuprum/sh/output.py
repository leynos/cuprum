"""Captured/echoed output configuration for ``cuprum.sh`` runs.

``RunOutputOptions`` configures how a command or pipeline's stdout and stderr
are captured, mirrored, and optionally reframed through a presentation sink.
The ``cuprum.sh`` package re-exports the public name. The deprecated
``IOOptions`` alias lives in :mod:`cuprum.sh._deprecated`; the deprecated flat
``capture``/``echo`` keywords that still reach this vocabulary are owned by
:mod:`cuprum.sh.pipeline_output`.

The stdio target vocabulary lives in :mod:`cuprum.sh.stdio` and the rules
policing it in :mod:`cuprum.sh.stdio_rules`; ``StdioTarget`` is re-exported
here so ``cuprum.sh.output`` remains a usable import path for it. Field rules
from :mod:`cuprum.sh.output_rules` are re-exported under their original names.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum._bounded_delivery import BoundedDelivery, _validate_delivery_options
from cuprum._constants import DEFAULT_ECHO_MAX_LINE_BYTES, DEFAULT_MAX_LINE_CHARS
from cuprum._idle_heartbeat import _validate_idle_options
from cuprum.echo_events import BrokenPipePolicy, _parse_broken_pipe_policy
from cuprum.sh._capture_options import _resolve_capture as _resolve_capture
from cuprum.sh._capture_options import _resolved_capture as _resolved_capture
from cuprum.sh._convenience_sink import synthesize_sink
from cuprum.sh._resolved_options import _ResolvedOptionsMixin
from cuprum.sh.output_rules import (
    _validate_convenience_flags as _validate_convenience_flags,
)
from cuprum.sh.output_rules import (
    _validate_max_line_chars as _validate_max_line_chars,
)
from cuprum.sh.output_rules import _validate_positive_bound
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

    # Type-only like ``LineHook`` below: the capture policies appear in field
    # and property annotations and in docstring examples, never in run-time
    # expressions — ``__post_init__`` delegates the resolution that used them
    # to :mod:`cuprum.sh._capture_options`.
    from cuprum.capture_policy import CapturePolicy
    from cuprum.lines import LineHook

    # Name-compatible rather than type-accurate: this module publishes
    # ``IOOptions`` at run time only, through ``__getattr__`` below, because
    # the alias lives in :mod:`cuprum.sh._deprecated` and that module imports
    # this one. The block gives the export checker a name to resolve without
    # making the import real.
    from cuprum.sh._deprecated import IOOptions

__all__ = [
    "BoundedDelivery",
    "IOOptions",
    "RunOutputOptions",
    "StdioTarget",
]


def __getattr__(name: str) -> object:
    """Resolve the deprecated ``IOOptions`` alias on first use.

    The alias lived in this module before :mod:`cuprum.sh._deprecated` took it
    over, and callers still import it from here. A module-level import would
    be a cycle — ``_deprecated`` imports ``RunOutputOptions`` from this module
    — so the name is resolved when it is asked for instead, which is also why
    it cannot be a plain re-export.

    Parameters
    ----------
    name : str
        The attribute being resolved.

    Returns
    -------
    object
        The compatibility name, when this module supplies one.

    Raises
    ------
    AttributeError
        If *name* is not a compatibility name this module supplies.
    """
    if name == "IOOptions":
        from cuprum.sh._deprecated import IOOptions

        return IOOptions
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


@dc.dataclass(frozen=True, slots=True)
class RunOutputOptions(_ResolvedOptionsMixin):
    """Configure captured and mirrored command output.

    Parameters
    ----------
    capture : bool, default=True
        Store stdout and stderr on the returned result. Capture is independent
        of echoing, so a captured stream can remain silent and an echoed stream
        can be left uncaptured.
    capture_stdout : CapturePolicy | bool | None, keyword-only, default=None
        How stdout is captured. ``None`` inherits ``capture``; a policy is
        used as given; ``True`` and ``False`` are the shorthand's spelling for
        a complete capture and no capture. A
        :class:`~cuprum.capture_policy.BoundedCapture` retains the most recent
        ``max_bytes`` of the stream and reports the exact dropped byte count,
        and a :class:`~cuprum.capture_policy.FileBackedCapture` spills to a
        temporary file past a threshold, reporting the path instead of the
        text. A bounded *text* capture is refused before spawning under an
        encoding whose code-point boundaries a truncated tail cannot repair,
        which is every multi-byte codec except UTF-8 and fixed-width UTF-16
        or UTF-32; byte captures are not restricted. The policy governs the
        capture tap only: bytes a pipeline stage relays to the next stage are
        never affected.
    capture_stderr : CapturePolicy | bool | None, keyword-only, default=None
        How stderr is captured, resolved exactly as ``capture_stdout`` is and
        independently of it, so the two streams can be configured differently
        in one run.
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
        fast-path dispatcher stays out of the way whenever this is set. How
        much of an unterminated line is retained before it is published in
        pieces is governed by ``max_line_chars``.
    max_line_chars : int | None, keyword-only, default=64 * 1024
        Maximum number of decoded characters retained for one observed line.
        The count covers line content only: terminators are held separately
        and never counted. A longer line is published as fragments of exactly
        this many characters through the same line sink, so the ``lines()``
        driver's backpressure still applies and no event exceeds the bound.
        Fragments can split a grapheme cluster or a multi-byte character's
        decoded form; they are not otherwise distinguished from complete
        lines. The bound is independent of ``capture`` and ``echo``:
        ``max_echo_line_bytes`` bounds only the mirrored copy in bytes.
        ``None`` disables bounded retention. This field is keyword-only
        because a positional value would take ``on_line``'s slot and shift
        every field after it. Non-integers, including ``bool``, raise
        ``TypeError``; zero and negative integers raise ``ValueError``.
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
    delivery : BoundedDelivery | None, keyword-only, default=None
        Opt into one byte-bounded delivery worker per run. Sink writes happen
        away from the execution loop; dropped output is reported while capture
        remains complete.
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
    # Keyword-only although they read beside ``capture``: ``RunOutputOptions``
    # is a public, non-``kw_only`` dataclass whose first six fields are
    # positional, and ``test_public_api`` pins them. Declaring these two
    # positionally would take the slots of ``max_echo_line_bytes`` and
    # ``on_line`` and rebind every existing positional caller.
    capture_stdout: CapturePolicy | bool | None = dc.field(default=None, kw_only=True)
    capture_stderr: CapturePolicy | bool | None = dc.field(default=None, kw_only=True)
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
    max_line_chars: int | None = dc.field(
        default=DEFAULT_MAX_LINE_CHARS,
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
    delivery: BoundedDelivery | None = dc.field(default=None, kw_only=True)
    # `dataclasses.replace` forwards init fields. Keep the generated identity
    # separate so reusing its sink in a new options object remains explicit.
    _synthesized_sink: sinks.OutputSink | None = dc.field(
        default=None,
        repr=False,
        compare=False,
        kw_only=True,
    )
    # Non-init, for the reason ``_synthesized_sink`` above is: a resolution
    # stored over the declared fields would be re-passed by
    # ``dataclasses.replace`` as an explicit per-stream declaration.
    _resolved_capture: tuple[CapturePolicy | None, CapturePolicy | None] = dc.field(
        init=False,
        repr=False,
        compare=False,
        default=(None, None),
    )

    def __post_init__(self) -> None:
        """Resolve the echo and capture shorthands, then synthesize any sink."""
        object.__setattr__(
            self,
            "_resolved_capture",
            _resolved_capture(
                shorthand=self.capture,
                capture_stdout=self.capture_stdout,
                capture_stderr=self.capture_stderr,
            ),
        )
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
        object.__setattr__(
            self,
            "delivery",
            _validate_delivery_options(self.delivery),
        )
        _validate_convenience_flags(self)
        _validate_stdio_targets(self)
        synthesize_sink(self)

        _validate_max_line_chars(self.max_line_chars)
        _validate_positive_bound("max_echo_line_bytes", self.max_echo_line_bytes)

    # The resolved views -- ``resolved_capture``, ``captures_any_stream``,
    # ``resolved_echo``, and ``resolved_broken_pipe_policy`` -- are contributed
    # by ``_ResolvedOptionsMixin``; the class docstring's examples above are
    # still resolved through them, because the mixin's properties bind on the
    # instance exactly as methods declared here would.
