"""Tests for the subprocess oracle used by the Rust parity property."""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import _streams_rs
from tests.helpers.consume_boundary_oracle import _capture_python, check_payload


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
        pytest.param(b"split-\xe2\x98\x83-tail", 2, id="split-multibyte"),
        pytest.param(b"malformed-\xed\xa0\x80-end", 3, id="malformed-surrogate"),
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
