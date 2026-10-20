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

    A text-mode bounded capture requires an encoding whose code-point
    boundaries can still be found once the head of the stream has been
    dropped: UTF-8, the fixed-width UTF-16 and UTF-32 codecs, and any
    single-byte encoding. A multi-byte East Asian codec is refused before
    anything is spawned, because a cut between its lead and trail bytes would
    decode the orphaned trail as a character the child never wrote. Byte
    captures are never restricted, since they are never decoded; a
    :class:`~cuprum.capture_policy.FileBackedCapture` or
    :class:`FullCapture` is available to a caller who needs those encodings
    as text.

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
    ``spill_threshold_bytes``; past that the bytes already arrived are written
    to a file and every later chunk is appended there, so retention stops
    growing with the child's output. A stream that never crosses the threshold
    is finalized exactly as :class:`FullCapture` would have finalized it —
    decoded text on the result, ``storage`` reported as ``"memory"`` — so
    raising the threshold is a pure memory decision and never changes what the
    caller reads.

    A spilled text-mode stream is reported as ``None`` on ``stdout``/``stderr``.
    The library created the spill file and removes it *before the result is
    returned* — on success, failure, timeout, and cancellation alike — so the
    bytes are readable through :meth:`CapturedStream.read_text` only when
    ``keep_file=True`` transfers the file to the caller. Without it, the record
    reports the stream's size and the bytes past the threshold are not retained
    for the caller. Byte mode is different: there the file is read back before
    it is removed, so ``run_bytes()`` still returns the child's own bytes.

    Parameters
    ----------
    spill_threshold_bytes : int | None, default=1048576
        Positive byte count at which the stream is moved to a file. ``None``
        spills on the first byte, for a caller who never wants the retention
        on the heap at all. A ``bool`` is rejected, as for
        :class:`BoundedCapture`.
    directory : pathlib.Path | str | None, default=None
        Directory the spill file is created in; ``None`` uses the process's
        temporary directory. Name one when the spill must land on a particular
        filesystem, or when ``keep_file`` means the caller will collect it. A
        ``str`` is normalized to a :class:`~pathlib.Path`. Writes are shielded
        so teardown never unlinks a file under an in-flight write: a stalled
        mount here stalls the run, and a timeout or cancellation cannot settle
        until the blocked call returns, so use a local, responsive filesystem.
    keep_file : bool, default=False
        Leave the spill file in place when the run finishes, transferring it
        to the caller. The path is reported either way. A cancellation landing
        before the initial spill's prefix is written still removes the file:
        until that write lands, the file is the run's, not the caller's.

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
        Bytes the drain received from this stream. For a bounded capture this
        counts the dropped bytes too, so it is the size of the stream rather
        than the size of what was retained. A run that stopped reading early —
        a timeout, say — cannot count bytes the child wrote afterwards, so
        the field describes what reached the parent rather than every byte the
        child wrote.
    dropped_bytes:
        Bytes discarded by the policy, and therefore the count absent from
        the retained text. Zero for a complete capture, and for a spilled one,
        which discards nothing.
    storage:
        ``"memory"`` when the retained bytes are held in the parent and
        reachable through ``stdout``/``stderr``, ``"file"`` when they are at
        ``path`` instead.
    path:
        The spill file, or ``None`` for a memory-backed capture. The path is
        reported even when the run has already removed the file, which it does
        before returning unless ``keep_file=True``: read it through
        :meth:`read_text` only for a kept spill. An unreadable path — one under
        a directory since removed, say — is not detected here.
    owned:
        Whether cuprum still owns this file, and so will remove it. A
        ``keep_file`` policy hands the file to the caller instead, which
        reports ``owned`` as ``False``: the path is given out either way, so
        what this field answers is who must clean up. Always ``False`` for a
        memory-backed capture.
    storage_failed:
        Whether a create, write, flush, or read-back of the spill file failed.
        The run still completes and the child is still reaped; the failure is
        reported through the capture event channel, which names the error
        class. With ``storage="file"`` the file holds the prefix that reached
        it before the failure. With ``storage="memory"`` the file was never
        created and the complete text is on ``stdout``/``stderr``. ``False``
        for every full or bounded capture.

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
        """Read a kept spill file back as text.

        Only a kept spill has a readable file: default ``keep_file=False``
        removes it before return, so use ``keep_file=True`` to read it.
        Memory-backed capture text is on the result's ``stdout``/``stderr``
        field and raises here to distinguish it from an empty spill.

        The child's raw bytes may not be valid text. ``errors="replace"``
        keeps that from raising by default.
        Filesystem and codec errors from ``Path.read_text`` propagate unchanged.

        Parameters
        ----------
        encoding : str, default="utf-8"
            Text encoding to decode the file's bytes with.
        errors : str, default="replace"
            Error policy for undecodable bytes.

        Returns
        -------
        str
            The decoded file contents, including a failed storage capture's
            written prefix.

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
