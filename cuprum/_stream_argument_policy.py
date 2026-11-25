"""Shared argument policy for the native stream adapters.

``rust_pump_stream`` and ``rust_consume_stream`` must classify a bad
``buffer_size`` or reader descriptor identically, in the same order, before any
platform work or native call. Keeping that policy here, rather than in either
adapter, is what stops the two wrappers drifting apart again.

The order mirrors the native boundary: ``operator.index`` first, then PyO3's
``i64`` extraction range, then the stream-domain limits. The adapters call the
buffer check first so an invalid size is always the failure a caller sees.

Example
-------
from cuprum._stream_argument_policy import _validate_buffer_size

size = _validate_buffer_size(65_536)
"""

from __future__ import annotations

import operator

_I64_MIN = -(1 << 63)
_I64_MAX = (1 << 63) - 1
_I32_MAX = (1 << 31) - 1
_MAX_BUFFER_SIZE = 1 << 30


def _index_within_i64(value: int) -> int:
    """Normalize an index-like value and require PyO3's ``i64`` range."""
    index = operator.index(value)
    if not _I64_MIN <= index <= _I64_MAX:
        msg = "Python int too large to convert to C long"
        raise OverflowError(msg)
    return index


def _validate_buffer_size(buffer_size: int) -> int:
    """Normalize a buffer size to ``int`` and apply the native size policy.

    The order mirrors the native validator: ``operator.index`` first, then the
    ``i64`` extraction range, then the positive and 1 GiB limits. Both wrappers
    call this before touching a descriptor so an invalid size is always the
    first reported failure.

    Parameters
    ----------
    buffer_size : int
        Candidate size; any object implementing ``__index__`` is accepted.

    Returns
    -------
    int
        The size as a plain ``int``, already known to be within policy.

    Raises
    ------
    TypeError
        If ``buffer_size`` does not implement ``__index__``.
    OverflowError
        If the value does not fit PyO3's ``i64`` extraction range.
    ValueError
        If the value is not positive or exceeds the 1 GiB cap.
    """  # ruff: ignore[docstring-extraneous-exception] - TypeError and OverflowError propagate from _index_within_i64.
    size = _index_within_i64(buffer_size)
    if size <= 0:
        msg = "buffer_size must be greater than zero"
        raise ValueError(msg)
    if size > _MAX_BUFFER_SIZE:
        msg = "buffer_size exceeds the maximum permitted size"
        raise ValueError(msg)
    return size


def _validate_raw_reader(reader_fd: int) -> int:
    """Normalize a raw descriptor and apply the shared range policy.

    The order matches the native boundary: ``i64`` extraction range, then the
    sign, then the signed 32-bit descriptor range. The check runs before any
    platform conversion so an unusable value never reaches ``msvcrt`` and never
    causes a Windows ``OSError`` where the documented class is ``ValueError``.

    Parameters
    ----------
    reader_fd : int
        Candidate descriptor; any object implementing ``__index__`` is
        accepted.

    Returns
    -------
    int
        The descriptor as a plain ``int``, still in its raw platform form.

    Raises
    ------
    TypeError
        If ``reader_fd`` does not implement ``__index__``.
    OverflowError
        If the value does not fit PyO3's ``i64`` extraction range.
    ValueError
        If the descriptor is negative or outside the signed 32-bit range.
    """  # ruff: ignore[docstring-extraneous-exception] - TypeError and OverflowError propagate from _index_within_i64.
    reader = _index_within_i64(reader_fd)
    if reader < 0:
        msg = "file descriptor must be non-negative"
        raise ValueError(msg)
    if reader > _I32_MAX:
        msg = "file descriptor out of range"
        raise ValueError(msg)
    return reader
