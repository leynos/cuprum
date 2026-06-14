"""Property tests for manylinux container reference pinning."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum.unittests.test_maturin_build import _MANYLINUX_CONTAINER_SHA256_RE

_MANYLINUX_IMAGE = "ghcr.io/rust-cross/manylinux_2_28-cross"

settings.register_profile("manylinux-container-ref", max_examples=200)
settings.load_profile("manylinux-container-ref")


@given(digest=st.binary(min_size=32, max_size=32))
def test_valid_sha256_ref_always_matches(digest: bytes) -> None:
    """Every 32-byte digest hex encoding produces a valid pinned ref."""
    container_ref = f"{_MANYLINUX_IMAGE}@sha256:{digest.hex()}"

    assert _MANYLINUX_CONTAINER_SHA256_RE.fullmatch(container_ref)


@given(
    tag=st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        min_size=1,
        max_size=50,
    )
)
def test_mutable_tag_never_matches(tag: str) -> None:
    """Mutable tag references never satisfy the pinned ref regex."""
    container_ref = f"{_MANYLINUX_IMAGE}:{tag}"

    assert _MANYLINUX_CONTAINER_SHA256_RE.fullmatch(container_ref) is None


@given(digest_length=st.integers(min_value=0, max_value=63))
def test_truncated_digest_never_matches(digest_length: int) -> None:
    """Truncated SHA-256 digest references never satisfy the pinned ref regex."""
    container_ref = f"{_MANYLINUX_IMAGE}@sha256:{'a' * digest_length}"

    assert _MANYLINUX_CONTAINER_SHA256_RE.fullmatch(container_ref) is None
