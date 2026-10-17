"""How a retained capture becomes the value the caller reads.

Two concerns live here, and both are about *decoding* rather than retaining.

:class:`_DecodeSettings` is the decode half of a drain config: an accumulator
is told once, at construction, how to render, which is also what lets it emit
its events already knowing which stream they describe.

:func:`_trim_to_code_point` is the one place a byte bound meets a text
encoding. A bounded capture keeps the most recent bytes of a stream, and the
cut that keeps it bounded can land inside a code point, so the retained tail
has to be trimmed before it is decoded; :func:`_render` then walks the same
path the drain's own seam walks, so an accumulator's payload and the drain's
cannot disagree about type or decoding.

Keeping these apart from the accumulators is what stops the optional imports
pulling backwards: only this module needs the codec registry.
"""

from __future__ import annotations

import codecs
import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum._streams import _StreamConfig

__all__ = [
    "_DecodeSettings",
    "_render",
    "_trim_to_code_point",
]

# Widest code point, in bytes, for each encoding the codec registry names.
# Only the width is needed: it bounds how far a mid-stream cut can land inside
# one code point, and so how many leading bytes of a tail may be its interior.
_MAX_CODE_POINT_BYTES = {
    "utf-8": 4,
    "utf-16-be": 4,
    "utf-16-le": 4,
    "utf-32-be": 4,
    "utf-32-le": 4,
    "ascii": 1,
    "latin-1": 1,
}
_DEFAULT_MAX_CODE_POINT_BYTES = 4

# A byte that is neither ASCII nor a UTF-8 lead byte continues the code point
# the cut landed inside.
_CONTINUATION_MASK = 0xC0
_CONTINUATION_VALUE = 0x80

# Fixed-width code units, in bytes. Their edges are found by alignment rather
# than by inspecting byte values, because a UTF-16 low byte can fall in the
# same 0x80-0xBF range a UTF-8 continuation byte occupies.
_CODE_UNIT_BYTES = {
    "utf-16-be": 2,
    "utf-16-le": 2,
    "utf-32-be": 4,
    "utf-32-le": 4,
}

# A UTF-16 low surrogate, in the halfword's high byte, and the width of the
# halfwords that carry one.
_SURROGATE_LOW_MIN = 0xDC
_SURROGATE_LOW_MAX = 0xDF
_UTF16_UNIT_BYTES = 2


@dc.dataclass(frozen=True, slots=True)
class _DecodeSettings:
    """How a finalized capture becomes the value the caller reads.

    ``max_code_point_bytes`` is derived from :attr:`encoding` rather than
    declared, so a caller cannot pair an encoding with the wrong width.
    """

    encoding: str
    errors: str
    capture_bytes: bool
    max_code_point_bytes: int = dc.field(init=False)

    def __post_init__(self) -> None:
        """Derive the code-point width of :attr:`encoding`."""
        try:
            canonical = codecs.lookup(self.encoding).name
        except LookupError:
            # An encoding the registry does not know is reported when the
            # decode runs, exactly as it was before this module existed.
            width = _DEFAULT_MAX_CODE_POINT_BYTES
        else:
            width = _MAX_CODE_POINT_BYTES.get(canonical, _DEFAULT_MAX_CODE_POINT_BYTES)
        object.__setattr__(self, "max_code_point_bytes", width)

    @classmethod
    def from_config(cls, config: _StreamConfig) -> _DecodeSettings:
        """Read the decode settings a drain config carries."""
        return cls(
            encoding=config.encoding,
            errors=config.errors,
            capture_bytes=config.capture_bytes,
        )


def _render(retained: bytes, settings: _DecodeSettings) -> str | bytes:
    """Render retained bytes the way the drain's own seam renders them.

    Replaces the drain's former ``_captured_payload`` seam, and every
    accumulator routes through it, so a payload from any policy cannot
    disagree about type or decoding with a payload from any other.

    Returns
    -------
    str | bytes
        The child's bytes untouched in byte mode, otherwise the decoded text.
        This is exactly the widened payload the drain itself carries, so a
        caller that already types its result as ``str | bytes | None`` can
        store this without a cast; an ``object`` return would have forced one
        at every site that reads it.
    """
    if settings.capture_bytes:
        return retained
    return retained.decode(settings.encoding, errors=settings.errors)


def _trim_to_code_point(
    tail: bytes,
    settings: _DecodeSettings,
    *,
    stream_offset: int,
) -> bytes:
    r"""Return the longest suffix of *tail* that decodes cleanly from its start.

    A byte tail can begin inside a code point, and a truncated capture must not
    grow a replacement character at its leading edge or raise from a strict
    decoder. Two steps are needed because either edge alone leaves a case
    behind: the leading edge is moved to a code-point boundary, then any
    trailing *incomplete* code point is dropped.

    The leading step depends on how the encoding marks a boundary. UTF-8 is
    self-describing, so any leading continuation byte is skipped. A fixed-width
    encoding (UTF-16, UTF-32) is not: its bytes carry no such marker, and an
    ordinary low byte can occupy the same ``0x80``-``0xBF`` range a UTF-8
    continuation byte does. Those are realigned by *stream* offset instead —
    the position the retained tail starts at decides how far into a code unit
    the cut landed — and a UTF-16 tail that still begins on a low surrogate
    drops that half of the pair. Any other encoding gets no leading adjustment;
    the trailing step remains the one rule that holds for all of them.

    The trailing step asks the codec rather than counting bytes. The decoder
    reports what it has seen but not emitted, which it drops without needing an
    error policy; a single-byte encoding never reports pending bytes, so this
    cannot mangle an encoding whose bytes are not self-delimiting.

    Parameters
    ----------
    tail : bytes
        The most recent bytes of the child's stream.
    settings : _DecodeSettings
        The run's decoding, whose encoding decides where a cut is safe.
    stream_offset : int
        The stream position *tail* starts at, in bytes from the child's first
        byte. Needed only by the fixed-width encodings, which realign by
        absolute position; a caller that keeps the whole stream passes ``0``.

    Returns
    -------
    bytes
        A suffix of *tail*, shortened by at most
        ``max_code_point_bytes - 1`` bytes: never longer than the input, so the
        byte bound still holds, and never shorter than a strict decoder could
        have produced.

    Examples
    --------
    >>> settings = _DecodeSettings("utf-8", "strict", capture_bytes=False)
    >>> tail = "aé中".encode()
    >>> _trim_to_code_point(tail[:-1], settings, stream_offset=0)
    b'a\xc3\xa9'

    A fixed-width tail cut mid-unit is realigned by offset, so the leading
    partial unit is dropped rather than mistaken for a UTF-8 continuation
    byte and mangled:

    >>> utf16 = _DecodeSettings("utf-16-le", "strict", capture_bytes=False)
    >>> _trim_to_code_point("ab中".encode("utf-16-le")[1:], utf16, stream_offset=1)
    b'b\x00-N'
    """
    canonical = _canonical_encoding(settings.encoding)
    cut = _leading_trim(tail, canonical, stream_offset)
    retained = tail[cut:]
    pending = _pending_bytes(retained, settings.encoding)
    return retained[: len(retained) - pending] if pending else retained


def _canonical_encoding(encoding: str) -> str:
    """Return the codec registry's name for *encoding*, or the raw name."""
    try:
        return codecs.lookup(encoding).name
    except LookupError:
        return encoding


def _leading_trim(tail: bytes, canonical: str, stream_offset: int) -> int:
    """Return how many leading bytes of *tail* are the interior of a code point."""
    unit = _CODE_UNIT_BYTES.get(canonical)
    if unit is not None:
        cut = (unit - stream_offset % unit) % unit
        if unit == _UTF16_UNIT_BYTES and _starts_with_low_surrogate(
            tail, cut, canonical
        ):
            cut += unit
        return min(cut, len(tail))
    if canonical != "utf-8":
        return 0
    cut = 0
    limit = min(len(tail), _MAX_CODE_POINT_BYTES["utf-8"] - 1)
    while cut < limit and tail[cut] & _CONTINUATION_MASK == _CONTINUATION_VALUE:
        cut += 1
    return cut


def _starts_with_low_surrogate(tail: bytes, cut: int, canonical: str) -> bool:
    """Whether the UTF-16 halfword at *cut* is a low surrogate."""
    if cut + 2 > len(tail):
        return False
    high = tail[cut + 1] if canonical == "utf-16-le" else tail[cut]
    return _SURROGATE_LOW_MIN <= high <= _SURROGATE_LOW_MAX


def _pending_bytes(retained: bytes, encoding: str) -> int:
    """Bytes of an incomplete trailing code point in *retained*, if any."""
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    decoder.decode(retained, final=False)
    pending, _flag = decoder.getstate()
    return len(pending)
