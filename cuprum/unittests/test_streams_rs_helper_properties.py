"""Property tests for the shared stream argument helpers.

``_validate_buffer_size`` and ``_validate_raw_reader`` in
``cuprum/_stream_argument_policy.py`` are the single policy behind both
adapters. These properties exercise them directly over unbounded
integers and explicit boundaries, so a disagreement is reported by the smallest
value that produces it rather than through a native call. The wrapper-level
tables in ``test_streams_rs_adapter_contract.py`` pin which failure wins when
both arguments are bad; this module pins the classification itself.

Example
-------
pytest cuprum/unittests/test_streams_rs_helper_properties.py
"""

from __future__ import annotations

import operator
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum import _streams_rs

_I32_MAX = (1 << 31) - 1
_I64_MIN = -(1 << 63)
_I64_MAX = (1 << 63) - 1
_MAX_BUFFER_SIZE = 1 << 30


class _IndexOnly:
    """Expose an integer through ``__index__`` and nothing else."""

    def __init__(self, value: int) -> None:
        """Wrap ``value`` for the index protocol."""
        self.value = value

    def __index__(self) -> int:
        """Return the wrapped integer."""
        return self.value


_INDEX_LIKE = st.integers().map(_IndexOnly)
_NON_INDEX = st.one_of(
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(),
    st.binary(),
    st.none(),
    st.lists(st.integers()),
)


def _buffer_outcome(value: int) -> type[Exception] | None:
    """Return the exception the documented buffer policy requires."""
    if not _I64_MIN <= value <= _I64_MAX:
        return OverflowError
    if value <= 0 or value > _MAX_BUFFER_SIZE:
        return ValueError
    return None


def _reader_outcome(value: int) -> type[Exception] | None:
    """Return the exception the documented raw-reader policy requires."""
    if not _I64_MIN <= value <= _I64_MAX:
        return OverflowError
    if value < 0 or value > _I32_MAX:
        return ValueError
    return None


@settings(max_examples=200)
@given(value=st.integers())
def test_buffer_size_helper_matches_its_documented_policy(value: int) -> None:
    """Every integer is accepted or classified exactly as documented.

    ``st.integers()`` is unbounded, so the generator also covers values far
    outside PyO3's extraction range; those must stay ``OverflowError`` rather
    than becoming stream-domain ``ValueError``.
    """
    expected = _buffer_outcome(value)
    if expected is None:
        assert _streams_rs._validate_buffer_size(value) == value, (
            f"a permitted size must be returned unchanged, got {value}"
        )
        return

    with pytest.raises(expected):
        _streams_rs._validate_buffer_size(value)


@settings(max_examples=200)
@given(value=st.integers())
def test_raw_reader_helper_matches_its_documented_policy(value: int) -> None:
    """Every integer is classified by the shared descriptor policy.

    The signed 32-bit rejection is the Windows safety property: a value in
    ``(2**31 - 1, 2**63 - 1]`` fits PyO3's ``i64`` but must never reach
    ``msvcrt.get_osfhandle``.
    """
    expected = _reader_outcome(value)
    if expected is None:
        assert _streams_rs._validate_raw_reader(value) == value, (
            f"a permitted descriptor must be returned unchanged, got {value}"
        )
        return

    with pytest.raises(expected):
        _streams_rs._validate_raw_reader(value)


@settings(max_examples=100)
@given(value=st.one_of(st.integers(), _INDEX_LIKE))
def test_index_like_values_are_classified_by_their_integer(value: object) -> None:
    """Objects implementing ``__index__`` follow the same policy as ints."""
    candidate = typ.cast("int", value)
    raw = operator.index(candidate)
    expected = _buffer_outcome(raw)
    if expected is None:
        assert _streams_rs._validate_buffer_size(candidate) == raw, (
            f"index-like {raw} must normalize to a plain int"
        )
        return

    with pytest.raises(expected):
        _streams_rs._validate_buffer_size(candidate)


@given(_NON_INDEX)
def test_buffer_size_rejects_objects_without_index(value: object) -> None:
    """No implicit ``int()`` fallback is accepted for the buffer size."""
    with pytest.raises(TypeError):
        _streams_rs._validate_buffer_size(typ.cast("int", value))


@given(_NON_INDEX)
def test_raw_reader_rejects_objects_without_index(value: object) -> None:
    """No implicit ``int()`` fallback is accepted for the descriptor."""
    with pytest.raises(TypeError):
        _streams_rs._validate_raw_reader(typ.cast("int", value))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(1, None, id="smallest"),
        pytest.param(_MAX_BUFFER_SIZE, None, id="cap"),
        pytest.param(0, ValueError, id="zero"),
        pytest.param(-1, ValueError, id="negative"),
        pytest.param(_MAX_BUFFER_SIZE + 1, ValueError, id="above-cap"),
        pytest.param(_I64_MAX, ValueError, id="i64-max"),
        pytest.param(_I64_MAX + 1, OverflowError, id="above-i64"),
        pytest.param(_I64_MIN, ValueError, id="i64-min"),
        pytest.param(_I64_MIN - 1, OverflowError, id="below-i64"),
    ],
)
def test_buffer_boundaries(value: int, expected: type[Exception] | None) -> None:
    """The exact boundary values are classified by the documented policy."""
    if expected is None:
        assert _streams_rs._validate_buffer_size(value) == value, (
            f"boundary {value} must be accepted unchanged"
        )
        return

    with pytest.raises(expected):
        _streams_rs._validate_buffer_size(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(0, None, id="zero"),
        pytest.param(1, None, id="one"),
        pytest.param(_I32_MAX, None, id="i32-max"),
        pytest.param(-1, ValueError, id="negative"),
        pytest.param(_I32_MAX + 1, ValueError, id="above-i32"),
        pytest.param(_I64_MAX, ValueError, id="i64-max"),
        pytest.param(_I64_MAX + 1, OverflowError, id="above-i64"),
        pytest.param(_I64_MIN, ValueError, id="i64-min"),
        pytest.param(_I64_MIN - 1, OverflowError, id="below-i64"),
    ],
)
def test_reader_boundaries(value: int, expected: type[Exception] | None) -> None:
    """The exact descriptor boundary values are classified as documented."""
    if expected is None:
        assert _streams_rs._validate_raw_reader(value) == value, (
            f"boundary {value} must be accepted unchanged"
        )
        return

    with pytest.raises(expected):
        _streams_rs._validate_raw_reader(value)


def test_prepare_native_reader_rechecks_the_converted_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform conversion may widen beyond ``i64`` and is rechecked.

    Windows narrows a sign-extended handle to pointer width, but the value
    still has to cross PyO3 as ``i64``. The second check belongs after
    conversion, while the raw range check stays before it.
    """
    monkeypatch.setattr(_streams_rs, "_convert_fd_for_platform", lambda _fd: 1 << 63)

    with pytest.raises(OverflowError, match="too large"):
        _streams_rs._prepare_native_reader(5)
