"""Per-stream capture policies and the storage metadata a run reports.

Capture is the child's bytes *retained* for the caller, as opposed to the
mirrored copy an echo sink receives. Until this module existed the retention
was a single boolean: capture everything on the parent heap, or capture
nothing. That forces a bad choice on a long-running, noisy child, whose total
output the parent would have to hold in memory for the run's lifetime just to
be able to report why it failed.

Three policies replace the boolean. :class:`FullCapture` is today's behaviour
and stays the default. :class:`BoundedCapture` retains a byte tail — the most
recent ``max_bytes`` of the stream — and reports exactly how much it dropped.
:class:`FileBackedCapture` keeps the stream on the parent heap until it passes
a threshold and then spills it to a temporary file, so a caller can hold a
complete artefact without the parent's memory tracking the child's output.

Two boundaries are load-bearing.

First, a policy governs the *capture tap only*. The bytes a pipeline stage
forwards to the next stage are relayed, not captured, and no policy here
touches them; a bounded stage still hands its consumer a complete stream.

Second, a bound is measured in **bytes**, not characters, because bytes are
what the child wrote and what the parent holds. A byte tail can therefore stop
in the middle of a multi-byte code point, so the retained tail is trimmed
forwards to the next code-point boundary before it is decoded: a truncated
capture never grows a replacement character at its leading edge and never
raises from a strict decoder. Trimming forwards means the retained tail may be
a few bytes shorter than ``max_bytes``; it is never longer, and it is always a
suffix of the child's output.

See :class:`CapturedStream` for what a run reports about the policy it applied.
"""

from __future__ import annotations

import dataclasses as dc
import enum
import pathlib
import typing as typ

from cuprum._constants import DEFAULT_CAPTURE_SPILL_THRESHOLD_BYTES

__all__ = [
    "BoundedCapture",
    "CapturePolicy",
    "CapturePolicyKind",
    "CaptureStorage",
    "CaptureStreamName",
    "CapturedStream",
    "FileBackedCapture",
    "FullCapture",
    "captures_any_stream",
    "policy_kind",
]

type CaptureStreamName = typ.Literal["stdout", "stderr"]
type CaptureStorage = typ.Literal["memory", "file"]


class CapturePolicyKind(enum.StrEnum):
    """Which policy a run applied to one stream, as reported metadata.

    A closed vocabulary rather than a class name, so a consumer of
    :attr:`CapturedStream.policy` can match exhaustively and cannot be broken
    by a policy being renamed.
    """

    FULL = "full"
    BOUNDED = "bounded"
    FILE_BACKED = "file_backed"


@dc.dataclass(frozen=True, slots=True)
class FullCapture:
    """Retain the whole stream on the parent heap.

    The default, and today's behaviour: every byte the child writes is held
    until the run finishes, then decoded and returned on
    ``CommandResult.stdout``/``.stderr``. Use it when the output is known to
    be bounded; use :class:`BoundedCapture` or :class:`FileBackedCapture` when
    it is not.
    """


@dc.dataclass(frozen=True, slots=True)
class BoundedCapture:
    """Retain only the most recent ``max_bytes`` of the stream.

    The oldest bytes are dropped as new ones arrive, so the parent's memory is
    bounded by ``max_bytes`` however much the child writes. The retained tail
    is a suffix of the child's output, trimmed forwards to a code-point
    boundary before decoding, and the drop count is reported exactly on the
    finalized :class:`CapturedStream`.

    The bound is on bytes, not characters: a caller bounding at 4096 gets at
    most 4096 bytes of text, which is between 4096 and 1024 characters
    depending on the script. See the module docstring for why.

    Parameters
    ----------
    max_bytes : int
        Positive byte budget for the retained tail. Must be strictly
        positive. A ``bool`` is rejected too, so ``True`` cannot arrive as a
        one-byte budget the caller never named.

    Examples
    --------
    >>> BoundedCapture(max_bytes=8).max_bytes
    8
    """

    max_bytes: int

    def __post_init__(self) -> None:
        """Reject a bound that cannot retain anything.

        Raises
        ------
        ValueError
            If ``max_bytes`` is not a positive integer.
        """
        is_positive_int = _is_positive_int(self.max_bytes)
        if not is_positive_int:
            msg = (
                "BoundedCapture max_bytes must be a positive integer, "
                f"got {self.max_bytes!r}"
            )
            raise ValueError(msg)


@dc.dataclass(frozen=True, slots=True)
class FileBackedCapture:
    """Keep the stream in memory, then spill it to a temporary file.

    The parent heap holds the stream only until it exceeds
    ``spill_threshold_bytes``; past that point the bytes that have already
    arrived are written to a file and every later chunk is appended there, so
    retention stops growing with the child's output. A stream that never
    crosses the threshold is finalized exactly as :class:`FullCapture` would
    have finalized it — decoded text on the result, ``storage`` reported as
    ``"memory"`` — so raising the threshold is a pure memory decision and
    never changes what the caller reads.

    A spilled stream is reported as ``None`` on ``stdout``/``stderr``; the
    bytes are at :attr:`CapturedStream.path`, readable through
    :meth:`CapturedStream.read_text`. The library created that file and
    removes it when the run finishes — on success, failure, timeout, and
    cancellation alike — unless ``keep_file`` transfers it to the caller.

    Parameters
    ----------
    spill_threshold_bytes : int | None, default=1048576
        Positive byte count at which the stream is moved to a file. ``None``
        spills on the first byte, for a caller who never wants the retention
        on the heap at all. A ``bool`` is rejected, as for
        :class:`BoundedCapture`.
    directory : pathlib.Path | str | None, default=None
        Directory the spill file is created in. ``None`` uses the process's
        temporary directory, which is the right default; name one when the
        spill must land on a particular filesystem, or when ``keep_file``
        means the caller will collect it. A ``str`` is normalized to a
        :class:`~pathlib.Path`, mirroring how
        ``RunOutputOptions.broken_pipe_policy`` accepts either a member or its
        string value.
    keep_file : bool, default=False
        Leave the spill file in place when the run finishes, transferring it
        to the caller. The path is reported either way, so a caller who keeps
        the file knows where it is and a caller who does not never has to
        clean up.

    Examples
    --------
    >>> FileBackedCapture(spill_threshold_bytes=1024).spill_threshold_bytes
    1024
    >>> FileBackedCapture().directory is None
    True
    """

    spill_threshold_bytes: int | None = DEFAULT_CAPTURE_SPILL_THRESHOLD_BYTES
    directory: pathlib.Path | str | None = None
    keep_file: bool = False

    def __post_init__(self) -> None:
        """Normalize the directory and reject a non-positive threshold.

        Raises
        ------
        ValueError
            If ``spill_threshold_bytes`` is not ``None`` and is not a
            positive integer.
        """
        if isinstance(self.directory, str):
            object.__setattr__(self, "directory", pathlib.Path(self.directory))
        threshold = self.spill_threshold_bytes
        if threshold is None:
            return
        if not _is_positive_int(threshold):
            msg = (
                "FileBackedCapture spill_threshold_bytes must be a positive "
                f"integer or None, got {threshold!r}"
            )
            raise ValueError(msg)


type CapturePolicy = FullCapture | BoundedCapture | FileBackedCapture

type CapturePolicyShorthand = CapturePolicy | bool
"""A capture policy, or the ``True``/``False`` shorthand for both streams.

The declared type of a ``RunOutputOptions`` per-stream field before its
constructor resolves it, so a predicate that asks "is this stream captured?"
accepts either spelling. ``bool`` tests exactly as the policy would:
``False`` is no policy and ``True`` is the default one.
"""


@dc.dataclass(frozen=True, slots=True)
class CapturedStream:
    """What one stream's capture produced, and where it is now.

    Reported as one entry of ``CommandResult.captures``, one per captured
    stream, so a caller can tell a complete capture from a truncated or
    spilled one without parsing the text. A run that captured nothing reports
    an empty tuple.

    Attributes
    ----------
    stream:
        Which stream this record describes.
    policy:
        Which policy the run applied to ``stream``.
    total_bytes:
        Bytes the child wrote to this stream, as read by the drain. For a
        bounded capture this counts the dropped bytes too, so it is the size
        of the stream rather than the size of what was retained.
    dropped_bytes:
        Bytes discarded by the policy, and therefore the count absent from
        the retained text. Zero for a complete capture, and for a spilled one,
        which discards nothing.
    storage:
        ``"memory"`` when the retained bytes are held in the parent and
        reachable through ``stdout``/``stderr``, ``"file"`` when they are at
        ``path`` instead.
    path:
        The spill file, or ``None`` for a memory-backed capture. An
        unreadable path — one under a directory since removed, say — is not
        detected here.
    owned:
        Whether cuprum still owns this file, and so will remove it. A
        ``keep_file`` policy hands the file to the caller instead, which
        reports ``owned`` as ``False``: the path is given out either way, so
        what this field answers is who must clean up. Always ``False`` for a
        memory-backed capture.
    storage_failed:
        Whether a write or flush to the spill file failed. The run still
        completes, the child is still reaped, and the file holds whatever
        reached it before the failure; the failure is reported through the
        capture event channel, which names the error class. ``False`` for
        every capture that did not spill.

    Examples
    --------
    A bounded capture reports the exact drop count::

        result = cmd.run_sync(output=RunOutputOptions(
            capture_stdout=BoundedCapture(max_bytes=8),
        ))
        assert result.captures[0].dropped_bytes == len(payload) - 8
    """

    stream: CaptureStreamName
    policy: CapturePolicyKind
    total_bytes: int
    dropped_bytes: int = 0
    storage: CaptureStorage = "memory"
    path: pathlib.Path | None = None
    owned: bool = False
    storage_failed: bool = False

    @property
    def truncated(self) -> bool:
        """Whether the policy discarded any bytes from this stream."""
        return self.dropped_bytes > 0

    def read_text(self, *, encoding: str = "utf-8", errors: str = "replace") -> str:
        """Read a spilled capture's bytes back as text.

        Only a spilled capture has a file to read. A memory-backed capture's
        text is on the result's ``stdout``/``stderr`` field instead, and this
        raises rather than returning something ambiguous — an empty string
        would be indistinguishable from a spilled stream that held nothing.

        The decoding defaults differ from a run's capture defaults on purpose:
        a spilled stream is the child's raw bytes, so ``errors="replace"``
        keeps a read from raising on a payload that was never valid text.

        Parameters
        ----------
        encoding : str, default="utf-8"
            Text encoding to decode the file's bytes with.
        errors : str, default="replace"
            Error policy for undecodable bytes.

        Returns
        -------
        str
            The file's contents decoded, holding whatever reached the file —
            which for a ``storage_failed`` capture is the prefix written
            before the failure.

        Raises
        ------
        ValueError
            If this capture is memory-backed, and so has no file to read.

        """
        if self.path is None:
            msg = (
                f"capture of {self.stream} is {self.storage}-backed; read its "
                "text from the result's stdout/stderr instead of read_text()"
            )
            raise ValueError(msg)
        return self.path.read_text(encoding=encoding, errors=errors)


def _is_positive_int(value: object) -> bool:
    """Whether *value* is a positive integer rather than a ``bool``.

    ``bool`` is an ``int`` subclass, so ``True`` would otherwise pass as a
    one-byte budget for something the caller never documented as a number.
    ``RunOutputOptions.max_echo_line_bytes`` draws the same line for the same
    reason.

    Returns
    -------
    bool
        Whether *value* is an ``int``, is not a ``bool``, and is positive.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def captures_any_stream(
    capture_stdout: CapturePolicy | CapturePolicyShorthand | None,
    capture_stderr: CapturePolicy | CapturePolicyShorthand | None,
) -> bool:
    """Whether a ``(stdout, stderr)`` capture pair captures anything.

    The single-bool reading of a capture pair, in one place because both the
    run options and the pipeline configuration tag their observation events
    with "is this a capturing run?" and the two must not disagree. A run that
    captures anything at all is a capturing run; a run that named no policy for
    either stream captures nothing.

    A stream is captured when it holds a policy, or the ``True`` shorthand the
    options constructor has not resolved yet. ``False`` is the shorthand for
    "no policy", so it must be tested as such: a caller may reach this with a
    field the constructor has not yet resolved.

    Parameters
    ----------
    capture_stdout : CapturePolicy | CapturePolicyShorthand | None
        The stdout policy, shorthand, or ``None`` when stdout is uncaptured.
    capture_stderr : CapturePolicy | CapturePolicyShorthand | None
        The stderr policy, shorthand, or ``None`` when stderr is uncaptured.

    Returns
    -------
    bool
        ``True`` when either stream is captured.

    Examples
    --------
    >>> captures_any_stream(FullCapture(), None)
    True
    >>> captures_any_stream(False, None)
    False
    >>> captures_any_stream(False, True)
    True
    """
    return _is_captured(capture_stdout) or _is_captured(capture_stderr)


def _is_captured(capture: CapturePolicyShorthand | None) -> bool:
    """Whether one stream's capture setting retains anything.

    Returns
    -------
    bool
        ``False`` for ``None`` and ``False``; ``True`` for a policy or ``True``.
    """
    return capture is not None and capture is not False


def policy_kind(policy: CapturePolicy) -> CapturePolicyKind:
    """Classify a resolved policy for the metadata a run reports.

    The one place a policy object becomes the kind a
    :class:`CapturedStream` carries, so the mapping cannot drift between the
    sites that build metadata for a command and for a pipeline stage.

    An uncaptured stream never reaches here: a record exists only where a
    policy did, so ``None`` is the absence of a call rather than a value this
    function classifies. Keeping the parameter concrete is what lets
    :attr:`CapturedStream.policy` be a non-optional kind.

    Parameters
    ----------
    policy : CapturePolicy
        A resolved policy for one stream.

    Returns
    -------
    CapturePolicyKind
        The policy's kind.

    Raises
    ------
    TypeError
        If ``policy`` is not one of the three policy types. The parameter is
        typed, so this can only be reached by a caller that bypassed the type
        checker; saying so beats silently reporting the wrong kind.

    Examples
    --------
    >>> policy_kind(BoundedCapture(max_bytes=8))
    <CapturePolicyKind.BOUNDED: 'bounded'>
    """
    match policy:
        case FullCapture():
            return CapturePolicyKind.FULL
        case BoundedCapture():
            return CapturePolicyKind.BOUNDED
        case FileBackedCapture():
            return CapturePolicyKind.FILE_BACKED
    msg = f"not a capture policy: {policy!r}"
    raise TypeError(msg)
