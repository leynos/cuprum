"""Tests for the subprocess oracle used by the Rust parity property."""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import _streams_rs
from tests.helpers.consume_boundary_oracle import _capture_python, check_payload


def _patterned_large_payload(length: int, tail: bytes) -> bytes:
    """Build an exact-length repeated prefix with a decoding witness at EOF."""
    pattern = b"cuprum-boundary/"
    prefix_length = length - len(tail)
    prefix = (pattern * ((prefix_length + len(pattern) - 1) // len(pattern)))[
        :prefix_length
    ]
    return prefix + tail


def test_python_capture_remains_independent_of_rust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Python reference must not call the optional Rust consumer."""

    def reject_native_call(*_args: object, **_kwargs: object) -> typ.Never:
        """Fail if the Python reference crosses into the native consumer."""
        pytest.fail("Python oracle called the Rust consumer")

    monkeypatch.setattr(_streams_rs, "rust_consume_stream", reject_native_call)

    assert _capture_python(b"plain\xe2\x98\x83\xff") == "plain☃\ufffd", (
        "the independent Python consumer should replace malformed UTF-8"
    )


@pytest.mark.parametrize(
    ("payload", "buffer_size"),
    [
        pytest.param(b"", 1, id="empty"),
        pytest.param(b"ascii\x00bytes", 7, id="nul-and-ascii"),
        pytest.param(b"\xc2\xa2", 2, id="valid-two-byte-scalar"),
        pytest.param(b"\xe2\x98\x83", 3, id="valid-three-byte-scalar"),
        pytest.param(b"\xf0\x9f\x8c\xb1", 4, id="valid-four-byte-scalar"),
        pytest.param(b"\x80", 1, id="lone-continuation"),
        pytest.param(b"\xc0\xaf", 2, id="overlong-encoding"),
        pytest.param(b"\xed\xa0\x80", 3, id="surrogate-encoding"),
        pytest.param(b"\xf4\x90\x80\x80", 4, id="above-unicode-maximum"),
        pytest.param(b"truncated-\xe2\x98", 8, id="truncated-at-eof"),
    ],
)
def test_native_wrapper_and_direct_defaults_match_python(
    payload: bytes,
    buffer_size: int,
) -> None:
    """The extension oracle compares all independent native call forms."""
    metadata = check_payload(payload, buffer_size)

    assert metadata.python_executable, "the child should report its interpreter"
    assert metadata.native_module_path, "the child should report its extension"


@pytest.mark.parametrize(
    "buffer_size",
    [1, 2, 3, 4, 65_535, 65_536, 65_537],
    ids=[
        "split-at-buffer-1",
        "split-at-buffer-2",
        "split-at-buffer-3",
        "split-at-buffer-4",
        "split-at-buffer-65535",
        "split-at-buffer-65536",
        "split-at-buffer-65537",
    ],
)
def test_four_byte_scalar_straddles_each_named_read_boundary(
    buffer_size: int,
) -> None:
    """A deterministic prefix puts the first scalar byte at each read edge."""
    payload = b"x" * (buffer_size - 1) + "🌱".encode()
    metadata = check_payload(payload, buffer_size)

    assert metadata.native_module_path, "the child should report its extension"


@pytest.mark.parametrize(
    ("length", "buffer_size", "tail"),
    [
        pytest.param(1_048_572, 4096, b"\xe2\x98\x83", id="length-1048572-valid"),
        pytest.param(1_048_573, 65_535, b"\xed\xa0\x80", id="length-1048573-surrogate"),
        pytest.param(1_048_574, 65_536, b"\xe2\x98", id="length-1048574-truncated"),
        pytest.param(1_048_575, 65_537, b"\xf0\x9f\x8c\xb1", id="length-1048575-valid"),
        pytest.param(
            1_048_576, 4096, b"\xf4\x90\x80\x80", id="length-1048576-above-max"
        ),
        pytest.param(1_048_577, 65_535, b"\xe2\x98\x83", id="length-1048577-valid"),
        pytest.param(1_048_578, 65_536, b"\xed\xa0\x80", id="length-1048578-surrogate"),
        pytest.param(1_048_579, 65_537, b"\xe2\x98", id="length-1048579-truncated"),
        pytest.param(1_048_580, 4096, b"\xf0\x9f\x8c\xb1", id="length-1048580-valid"),
    ],
)
def test_named_large_payload_lengths_match_python(
    length: int,
    buffer_size: int,
    tail: bytes,
) -> None:
    """Every required one-mebibyte boundary keeps the independent oracle."""
    payload = _patterned_large_payload(length, tail)
    metadata = check_payload(payload, buffer_size)

    assert metadata.native_module_path, "the child should report its extension"
