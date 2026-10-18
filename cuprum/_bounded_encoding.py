r"""Which text encodings a byte-bounded capture can safely render.

A bounded capture discards the oldest bytes as new ones arrive, so the retained
tail can begin inside a code point. The leading edge is repaired at
finalization by :func:`cuprum._capture_decode._trim_to_code_point`, and that
repair only knows how to find a boundary in two families: UTF-8, whose
continuation bytes announce themselves, and the fixed-width UTF-16/UTF-32
codecs, whose edges are found by realigning against the stream offset. For
every other encoding it gives up and keeps the tail whole.

Giving up is safe for a single-byte encoding, where every byte *is* a boundary
and there is nothing to repair. It is not safe for the multi-byte East Asian
families, whose trail bytes are ordinary byte values that cannot be told from a
lead byte without decoding from the start of the stream — a head the bound has
already thrown away. A Shift-JIS stream cut between ``b"\\x81"`` and
``b"a"`` would decode the orphaned trail byte on its own and put a character on
the result the child never wrote.

So a text-mode bounded capture is accepted only for encodings this module can
classify as safe, and refused before anything is spawned otherwise. Refusing
early is the honest answer: the alternative is a truncation that silently
invents a character, and no caller can want that. The bound is still exactly a
byte bound; what is restricted is which codecs a truncated tail can be read
under.

A byte-exact capture is never trimmed and is therefore never restricted: bytes
have no code-point boundaries to repair, and the encoding names only how the
drain renders text it no longer produces.

Nothing here caches a verdict. The registry is fixed, but a codec can be
replaced at run time by ``codecs.register``, and a cached verdict would then
outlive the codec it described.
"""

from __future__ import annotations

import codecs

from cuprum.capture_policy import BoundedCapture, CapturePolicy

__all__ = ["_reject_unsupported_bounded_encoding"]

# The codecs the trim can repair by name. UTF-8 is self-describing, and the
# fixed-width names are realigned by stream offset; every other safe codec is
# single-byte and needs no repair at all. The ``utf-16`` and ``utf-32`` family
# names are here because a truncated capture resolves them to a concrete
# fixed-width codec before it trims, using the mark or the platform's byte
# order — the same rule the family decoder would have applied itself.
_REPAIRABLE_CODECS: frozenset[str] = frozenset({
    "utf-8",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "utf-32",
    "utf-32-le",
    "utf-32-be",
})

# The one family name that resolves to plain UTF-8 once the bound has dropped
# the mark selecting it, and so needs no separate treatment here.
_MARK_ONLY_FAMILY = "utf-8-sig"


def _reject_unsupported_bounded_encoding(
    capture: CapturePolicy | None,
    *,
    encoding: str,
    capture_bytes: bool,
) -> None:
    """Reject a bounded text capture whose codec the trim cannot repair.

    Called from the two seams every run passes through before it spawns — the
    single-command bundle and the pipeline config — so the caller learns from
    a ``ValueError`` rather than from a result that quietly contains a
    character the child never wrote.

    Parameters
    ----------
    capture : CapturePolicy | None
        One stream's resolved capture policy.
    encoding : str
        The run's encoding, from its :class:`ExecutionContext`.
    capture_bytes : bool
        Whether the run returns raw bytes, in which case no trim happens and
        every encoding is admissible.

    Raises
    ------
    ValueError
        If the policy is a :class:`~cuprum.capture_policy.BoundedCapture`, the
        run is a text run, and the encoding is neither repairable by name nor
        single-byte.
    """
    if capture_bytes or not isinstance(capture, BoundedCapture):
        return
    if _is_trim_safe(encoding):
        return
    msg = (
        "BoundedCapture requires an encoding whose code-point boundaries can be "
        f"repaired after a cut; got {encoding!r}. Use FullCapture or "
        "FileBackedCapture for this stream, capture it as bytes, or choose a "
        "single-byte or UTF encoding."
    )
    raise ValueError(msg)


def _is_trim_safe(encoding: str) -> bool:
    """Whether a truncated tail under *encoding* can be trimmed correctly.

    Returns
    -------
    bool
        ``True`` for the repairable codecs, for the mark-selected ``utf-8-sig``
        family, and for any single-byte codec. ``False`` for a multi-byte codec
        the trim cannot realign, and for a name the registry does not know —
        which keeps its ``LookupError`` arriving from the decode, where it has
        always come from, rather than turning it into a construction error.
    """
    try:
        codec = codecs.lookup(encoding)
    except LookupError:
        return True
    canonical = codec.name
    if canonical in _REPAIRABLE_CODECS or canonical == _MARK_ONLY_FAMILY:
        return True
    return _is_single_byte(codec)


def _is_single_byte(codec: codecs.CodecInfo) -> bool:
    """Whether *codec* maps each byte it accepts to one character on its own.

    The probe is a definition rather than a sample. An incremental decoder fed
    one byte and asked what it is holding reports any byte it has not yet
    emitted: a single-byte codec never holds one, and a decoder that does is
    waiting for more bytes to finish the character it started. Asking all 256
    byte values covers every input such a decoder can be given.

    This is also what catches the escape-based codecs. ``hz`` and the ISO-2022
    families consume each lone byte cleanly as *text*, because their escape
    sequences are only recognized in longer runs, so comparing decoded lengths
    would admit them. The pending-byte question exposes them: the byte that
    begins an escape sequence is held until the rest of the sequence arrives.

    A codec that rejects a byte outright, or that fails before it can report
    its state, is not a text encoding at all and is refused by the same rule.

    Returns
    -------
    bool
        ``True`` only when the codec is a plain single-byte mapping.
    """
    try:
        decoder_factory = codecs.getincrementaldecoder(codec.name)
        for value in range(256):
            decoder = decoder_factory(errors="replace")
            decoder.decode(bytes((value,)), final=False)
            buffered: bytes
            buffered, _flags = decoder.getstate()
            if buffered:
                return False
    except (LookupError, TypeError, AssertionError, UnicodeError):
        # A transform codec (``base64``, ``hex``, ``bz2``) is not a text
        # encoding at all: ``bytes.decode`` refuses it outright, and a
        # transform codec's incremental decoder may assert instead of
        # reporting state. Treating both as unusable here reports the caller's
        # mistake at spawn, beside the policy that made it matter, rather than
        # from a failed decode later.
        return False
    return True
