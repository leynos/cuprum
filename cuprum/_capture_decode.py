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
import sys
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum._streams import _StreamConfig

__all__ = [
    "_BOM_MAX_BYTES",
    "_DecodeSettings",
    "_bounded_settings",
    "_render",
    "_trim_to_code_point",
]

# The longest byte-order mark among the codecs that carry one, so an
# accumulator knows how much of the stream's head it must remember to resolve
# the codec after a bound has discarded that head.
_BOM_MAX_BYTES = len(codecs.BOM_UTF32_LE)

# Family name to ``(little-endian codec, big-endian codec)`` for the codecs
# whose byte order is selected by a mark rather than by the caller's spelling.
# ``utf-8-sig`` names the same codec twice: its mark carries no order, it only
# announces the encoding. Only these need the mark; every other encoding's
# name already fixes its order.
_BOM_SELECTED_CODECS: dict[str, tuple[str, str]] = {
    "utf-16": ("utf-16-le", "utf-16-be"),
    "utf-32": ("utf-32-le", "utf-32-be"),
    "utf-8-sig": ("utf-8", "utf-8"),
}

# Each family's little- then big-endian mark, with UTF-32 before UTF-16: the
# UTF-32 little-endian mark begins with the UTF-16 one, so testing the narrower
# mark first would read a UTF-32 stream as UTF-16 with trailing NUL bytes. This
# is the order marks must be tried in, not the order the families are listed
# above.
_BOM_MARKS: tuple[tuple[str, bytes, bytes], ...] = (
    ("utf-32", codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE),
    ("utf-16", codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE),
    ("utf-8-sig", codecs.BOM_UTF8, codecs.BOM_UTF8),
)

# UTF-8's widest code point. A tail can begin at most three bytes inside one,
# which is the limit the UTF-8 leading trim scans; the other encodings find
# their boundaries by code-unit alignment rather than by scanning.
_UTF8_MAX_CODE_POINT_BYTES = 4

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
    """How a finalized capture becomes the value the caller reads."""

    encoding: str
    errors: str
    capture_bytes: bool

    @classmethod
    def from_config(cls, config: _StreamConfig) -> _DecodeSettings:
        """Read the decode settings a drain config carries."""
        return cls(
            encoding=config.encoding,
            errors=config.errors,
            capture_bytes=config.capture_bytes,
        )


def _mark_selected_codec(family: str, prefix: bytes) -> str | None:
    """Return the concrete codec *prefix* selects for *family*, if any."""
    for marked_family, little_mark, big_mark in _BOM_MARKS:
        if marked_family != family:
            continue
        little, big = _BOM_SELECTED_CODECS[family]
        if prefix.startswith(little_mark):
            return little
        if prefix.startswith(big_mark):
            return big
        return None
    return None


def _bounded_settings(settings: _DecodeSettings, prefix: bytes) -> _DecodeSettings:
    """Resolve the codec a truncated tail can still be read with.

    Call this only when the bound discarded the stream's head, because both
    resolutions below assume the mark can no longer be retained; applying them
    while it survives would leave a leading U+FEFF in the payload. A head that
    is gone also means the retained tail must be trimmed for the resolved
    codec rather than the one the caller named. Two rules need a concrete
    codec name. The trim does not recognize ``utf-8-sig`` as UTF-8, so a cut
    inside a multi-byte character would survive at the tail's leading edge. The
    ``utf-16``/``utf-32`` family names carry a dropped mark into the decode: a
    tail whose mark selected the order other than the platform's would be
    decoded in native order and produce wrong text.

    Parameters
    ----------
    settings : _DecodeSettings
        The run's decoding, whose encoding may need resolving.
    prefix : bytes
        The stream's first bytes, as captured before any bound discarded them.

    Returns
    -------
    _DecodeSettings
        *settings* with its encoding resolved to the concrete codec the
        stream's head selects: the mark's endianness where there is one, the
        platform's native order where the family name carried no mark, and
        plain UTF-8 for ``utf-8-sig``, whose mark cannot have survived a
        dropped head. An encoding that is not mark-selected is returned
        unchanged.
    """
    family = _canonical_encoding(settings.encoding)
    if family == "utf-8-sig":
        return dc.replace(settings, encoding="utf-8")
    if family not in _BOM_SELECTED_CODECS:
        return settings
    codec = _mark_selected_codec(family, prefix) or _native_codec(family)
    return dc.replace(settings, encoding=codec)


def _native_codec(family: str) -> str:
    """Return the concrete codec *family* assumes when its stream has no mark.

    A whole-stream ``utf-16`` or ``utf-32`` decode falls back to the platform's
    byte order when no mark is present, so a dropped head must not discard that
    rule and invent one. The tail path cannot rely on the fallback twice over:
    it decodes a cut tail rather than a whole stream, and the incremental
    decoder it consults for a trailing partial code unit rejects a first chunk
    with no mark. Naming the concrete codec keeps the rule and gives the trim a
    fixed width to realign by.

    Returns
    -------
    str
        The concrete little- or big-endian codec the platform's whole-stream
        decoder would select for *family*.
    """
    little, big = _BOM_SELECTED_CODECS[family]
    return little if sys.byteorder == "little" else big


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
        A suffix of *tail*, never longer than the input, so the byte bound
        still holds. It loses at most one partial code point at each edge:
        three bytes at most at the leading edge of a UTF-8 tail, and for a
        fixed-width tail the fractional code unit plus, in UTF-16, any
        low-surrogate half the alignment lands on. The trailing edge drops the
        incomplete code point there, if any. Nothing is removed from the
        middle.

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
    limit = min(len(tail), _UTF8_MAX_CODE_POINT_BYTES - 1)
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
