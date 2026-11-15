"""Tests for the subprocess oracle used by the Rust parity property."""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import _rust_backend, _streams_rs
from tests.helpers.consume_boundary_oracle import (
    _capture_python,
    _is_streamed_witness,
    check_payload,
)


@pytest.fixture(name="native_extension")
def fixture_native_extension() -> None:
    """Skip when the compiled extension the oracle compares against is absent.

    The comparison calls ``cuprum._rust_backend_native`` directly, so these
    cases cannot run without it. Gating them individually rather than with a
    module-level ``skipif`` keeps ``test_python_capture_remains_independent_of_rust``
    running everywhere: that case exercises the Python reference alone, so it
    is real coverage in an extension-free lane rather than a skip.
    """
    if not _rust_backend.is_available():
        pytest.skip("Rust extension is not installed")


def _patterned_large_payload(length: int, tail: bytes) -> bytes:
    """Build an exact-length repeated prefix with a decoding witness at EOF."""
    pattern = b"cuprum-boundary/"
    prefix_length = length - len(tail)
    prefix = (pattern * ((prefix_length + len(pattern) - 1) // len(pattern)))[
        :prefix_length
    ]
    return prefix + tail


@pytest.mark.parametrize(("available", "skips"), [(False, True), (True, False)])
def test_the_guard_skips_exactly_when_the_extension_is_unusable(
    *,
    available: bool,
    skips: bool,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Absence must produce a skip, and presence must not.

    A guard whose skip path is never exercised cannot be told apart from one
    that raises instead: the extension-free coverage lane reported
    ``ModuleNotFoundError`` for the native cases while every sibling module
    skipped, because nothing here asserted that absence means a skip.
    """
    monkeypatch.setattr(_rust_backend, "is_available", lambda: available)

    if not skips:
        assert request.getfixturevalue("native_extension") is None, (
            "a usable extension must not skip the native cases"
        )
        return
    with pytest.raises(pytest.skip.Exception):
        request.getfixturevalue("native_extension")


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


@pytest.mark.usefixtures("native_extension")
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


@pytest.mark.usefixtures("native_extension")
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


@pytest.mark.usefixtures("native_extension")
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


@pytest.mark.usefixtures("native_extension")
def test_incomplete_mebibyte_capture_drains_a_concurrent_producer() -> None:
    """Direct native consume drains a real producer pipe through EOF."""
    payload = _patterned_large_payload(1_048_577, b"\xe2\x98")
    metadata = check_payload(payload, buffer_size=65_535)
    expected = payload.decode("utf-8", errors="replace")

    assert len(metadata.native_captures) == 6, (
        "the wrapper and direct-native capture paths must all be exercised"
    )
    assert all(capture == expected for capture in metadata.native_captures), (
        "all native paths must match the Python replacement oracle"
    )
    assert metadata.reader_open_checks == len(metadata.native_captures), (
        "native consume must borrow every reader through its complete lifetime"
    )


@pytest.mark.parametrize(
    ("label", "length", "tail", "is_witness"),
    [
        ("named witness", 1_048_577, b"\xe2\x98", True),
        # Same length, other tail: every committed named case for this length
        # uses a three-byte tail, so this states the guard the oracle relies on.
        ("named length, other tail", 1_048_577, b"\xe2\x98\x83", False),
        # Same tail, other length: the payload cannot reach the producer intact.
        ("named tail, other length", 1_048_574, b"\xe2\x98", False),
        # Right length and tail, wrong prefix: the producer streams a repeated
        # pattern, so a payload built from anything else is not reconstructible.
        ("wrong prefix", 1_048_577, b"\xe2\x98", False),
    ],
)
def test_the_streamed_witness_requires_the_whole_named_shape(
    label: str,
    length: int,
    tail: bytes,
    *,
    is_witness: bool,
) -> None:
    """Only a fully reconstructible payload may select the subprocess path.

    A length-and-tail test admits payloads the producer cannot emit, which
    would fail the parity run with a "named boundary pattern" error instead of
    the guard simply declining to run. Checking the complete shape keeps the
    witness's own precondition where the witness is selected.
    """
    payload = (
        _patterned_large_payload(length, tail)
        if label != "wrong prefix"
        else b"x" * (length - len(tail)) + tail
    )

    assert _is_streamed_witness(payload) is is_witness, (
        f"{label} must {'select' if is_witness else 'not select'} the streamed "
        f"witness, got {_is_streamed_witness(payload)}"
    )
