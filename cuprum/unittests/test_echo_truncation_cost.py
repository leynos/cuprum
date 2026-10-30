"""Copy-volume regressions for the text-sink bounded-echo prefix search.

``_encode_prefix`` must do work linear in the line it is given. The original
implementation retained the best prefix by copying ``line[:length]`` and
``bytes(encoded_prefix)`` on every iteration, so a line only slightly longer
than the byte bound paid a triangular, quadratic copy volume. Wall-clock timing
is too noisy for a regression, so these cases count the bytes the search copies:
a duck-typed sequence stands in for the source line and records every slice, and
a module-local shadow of ``bytes`` counts conversions from a ``bytearray`` or
``memoryview``. The per-byte ``bytes((byte,))`` decode probe is not a prefix
copy and is excluded.

``_echo_truncation`` contains no runtime ``isinstance(..., bytes)`` check, which
is why a callable shadow is safe to install. Every measurement is preceded by a
live-counter witness, because a recorder that had gone blind would otherwise
report a pleasing zero.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest

from cuprum import _echo_truncation
from cuprum._echo_truncation import (
    _EchoEncoding,
    _EchoLineLimiter,
    _encode_prefix,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_BUILTIN_BYTES = bytes
_REAL_ENCODE_PREFIX = _echo_truncation._encode_prefix
_TEXT_SETTINGS = _EchoEncoding("utf-8", "strict", is_text_sink=True)
_LINE_COUNT = 8
_SMALL_BOUND = 512
_LARGE_BOUND = 4 * _SMALL_BOUND
_PASSES_PER_LINE_CEILING = 16
_BOUND_SCALING_CEILING = 5


class _SliceCountingLine:
    """A source line stand-in that records the length of every slice taken.

    ``_encode_prefix`` is typed for ``bytes``, so callers pass this through
    ``typ.cast``. A ``bytes`` subclass would need a ``__getitem__`` override
    that satisfies both of the base overloads, which no single signature can
    do, and the checker rejects every attempt.
    """

    def __init__(self, value: bytes) -> None:
        """Wrap *value* with an empty slice log."""
        self._value = value
        self.sliced_lengths: list[int] = []

    def __iter__(self) -> cabc.Iterator[int]:
        """Iterate the wrapped bytes as integers, as ``bytes`` would."""
        return iter(self._value)

    def __len__(self) -> int:
        """Report the wrapped byte length."""
        return len(self._value)

    def __getitem__(self, key: slice) -> bytes:
        """Record a slice's length before returning it unchanged."""
        sliced = self._value[key]
        self.sliced_lengths.append(len(sliced))
        return sliced


@dc.dataclass(slots=True)
class _CopyVolume:
    """Bytes copied by one or more prefix searches, split by mechanism."""

    slices: list[int] = dc.field(default_factory=list)
    conversions: list[int] = dc.field(default_factory=list)
    passes: int = 0

    @property
    def total(self) -> int:
        """Every copied byte, whichever mechanism copied it."""
        return sum(self.slices) + sum(self.conversions)


class _CountingBytesFactory:
    """A ``bytes`` stand-in that counts conversions from mutable buffers."""

    def __init__(self, volume: _CopyVolume) -> None:
        """Record conversions into *volume*."""
        self._volume = volume

    def __call__(self, source: object = b"") -> bytes:
        """Convert *source*, counting a copied bytearray or memoryview."""
        result = _BUILTIN_BYTES(typ.cast("typ.Any", source))
        if isinstance(source, (bytearray, memoryview)):
            self._volume.conversions.append(len(result))
        return result


def _install_counters(monkeypatch: pytest.MonkeyPatch) -> _CopyVolume:
    """Install the slice and conversion recorders for one measurement."""
    volume = _CopyVolume()
    monkeypatch.setattr(
        _echo_truncation,
        "bytes",
        _CountingBytesFactory(volume),
        raising=False,
    )
    return volume


def _measure_encode_prefix(
    line: bytes,
    *,
    budget: int,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_CopyVolume, bytes, bytes]:
    """Run the real prefix search once and return its copy volume and output."""
    volume = _install_counters(monkeypatch)
    spy = _SliceCountingLine(line)
    retained, encoded = _encode_prefix(
        typ.cast("bytes", spy),
        budget=budget,
        settings=_TEXT_SETTINGS,
    )
    volume.slices.extend(spy.sliced_lengths)
    return volume, retained, encoded


@pytest.mark.parametrize("line_size", [1024, 16 * 1024])
@pytest.mark.parametrize("shortfall", [0, 20])
def test_encode_prefix_copies_a_line_prefix_once(
    line_size: int,
    shortfall: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A near-line-length budget copies the line once, not once per byte."""
    line = b"x" * line_size
    budget = line_size - shortfall

    volume, retained, encoded = _measure_encode_prefix(
        line,
        budget=budget,
        monkeypatch=monkeypatch,
    )

    assert volume.slices, "the slice recorder must observe the source prefix"
    assert volume.conversions, "the conversion recorder must observe the encoder"
    assert retained == b"x" * len(retained), "an ASCII prefix must be unchanged"
    assert encoded == retained, "an ASCII prefix encodes to itself"
    assert len(encoded) <= budget, "the prefix must fit its budget"
    assert sum(volume.slices) <= line_size, (
        f"{sum(volume.slices)} source bytes were copied from a {line_size}-byte "
        "line; the search must copy the retained prefix once"
    )
    assert sum(volume.conversions) <= 8 * budget, (
        f"{sum(volume.conversions)} bytes were converted for budget {budget}; "
        "the search must convert the retained prefix once"
    )


def _measure_bounded_lines(
    *,
    bound: int,
    line_count: int,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_CopyVolume, list[bytes]]:
    """Echo *line_count* over-limit lines and return its copy volume."""
    volume = _install_counters(monkeypatch)

    def counting_encode_prefix(
        line: bytes,
        *,
        budget: int,
        settings: _EchoEncoding,
    ) -> tuple[bytes, bytes]:
        """Count one prefix search, recording the slices of its source line."""
        spy = _SliceCountingLine(line)
        volume.passes += 1
        result = _REAL_ENCODE_PREFIX(
            typ.cast("bytes", spy),
            budget=budget,
            settings=settings,
        )
        volume.slices.extend(spy.sliced_lengths)
        return result

    monkeypatch.setattr(_echo_truncation, "_encode_prefix", counting_encode_prefix)
    limiter = _EchoLineLimiter(bound)
    source = b"y" * (bound * 2)
    payloads = []
    for _ in range(line_count):
        limiter.bound_line(source)
        finished = limiter.finish_line(
            ending=b"\n",
            encoding="utf-8",
            errors="strict",
            is_text_sink=True,
        )
        payloads.append(finished.payload)
    return volume, payloads


def test_repeated_lines_scale_linearly_in_line_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eight over-limit lines cost exactly eight times one line, byte for byte."""
    one_volume, one_payloads = _measure_bounded_lines(
        bound=_SMALL_BOUND,
        line_count=1,
        monkeypatch=monkeypatch,
    )
    many_volume, many_payloads = _measure_bounded_lines(
        bound=_SMALL_BOUND,
        line_count=_LINE_COUNT,
        monkeypatch=monkeypatch,
    )

    assert one_volume.passes > 0, "the pass counter must witness the search"
    assert one_volume.total > 0, "the copy counters must witness the search"
    assert one_payloads == [b"y" * 486 + "… [truncated 538 bytes]".encode() + b"\n"], (
        f"the exact bounded line changed, got={one_payloads!r}"
    )
    assert many_payloads == one_payloads * _LINE_COUNT, (
        "every repeated line must produce the same bounded payload"
    )
    assert many_volume.total == _LINE_COUNT * one_volume.total, (
        f"{many_volume.total} bytes were copied for {_LINE_COUNT} lines, but one "
        f"line copies {one_volume.total}; the per-line work must not grow"
    )
    assert one_volume.passes <= _PASSES_PER_LINE_CEILING, (
        f"{one_volume.passes} prefix passes for one line exceeded the ceiling"
    )


def test_prefix_work_scales_linearly_with_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quadrupling the bound may not multiply the copied volume by sixteen."""
    small_volume, _ = _measure_bounded_lines(
        bound=_SMALL_BOUND,
        line_count=1,
        monkeypatch=monkeypatch,
    )
    large_volume, _ = _measure_bounded_lines(
        bound=_LARGE_BOUND,
        line_count=1,
        monkeypatch=monkeypatch,
    )

    assert small_volume.total > 0, "the small measurement must witness copy work"
    assert large_volume.total > 0, "the large measurement must witness copy work"
    assert large_volume.total <= _BOUND_SCALING_CEILING * small_volume.total, (
        f"bound {_LARGE_BOUND} copied {large_volume.total} bytes against "
        f"{small_volume.total} at bound {_SMALL_BOUND}; the prefix search must "
        "be linear in the bound"
    )
    assert large_volume.passes <= _PASSES_PER_LINE_CEILING, (
        f"{large_volume.passes} prefix passes for one line exceeded the ceiling"
    )
