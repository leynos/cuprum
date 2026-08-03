"""Tests for the maturin version and container pins staying synchronized.

This module owns the pin-synchronization checks. The readers live here rather
than in `tests/helpers/maturin.py`: they read repository files, have this
module as their only consumer, and gain nothing from indirection. The two that
do have a second consumer — the pyproject version reader and the container
reference pattern — sit in `_maturin_pin_support.py`, which documents why.
Wheel building and inspection keep their helpers, because those wrap
`subprocess` and `zipfile` work that does not inline cleanly.
"""

from __future__ import annotations

import importlib
import importlib.metadata as im
import re
import typing as typ

import pytest

from cuprum.unittests._maturin_pin_support import (
    MANYLINUX_CONTAINER_SHA256_RE,
    read_expected_maturin_version,
    read_text,
    require_pin_match,
)
from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import pathlib as pth


_WORKFLOW_PIN_RE = re.compile(r'MATURIN_VERSION:\s*"(\d+\.\d+\.\d+)"')
_ACTION_PIN_RE = re.compile(r'default:\s*"(\d+\.\d+\.\d+)"')
_AARCH64_CONTAINER_PIN_RE = re.compile(
    r"^\s*MANYLINUX_AARCH64_CONTAINER:\s*([^\s#]+)\s+#\s*\S.*$",
    re.MULTILINE,
)
_AARCH64_CONTAINER_USAGE_RE = re.compile(
    r"^\s*container:\s*\$\{\{\s*env\.MANYLINUX_AARCH64_CONTAINER\s*\}\}\s*$",
    re.MULTILINE,
)


def _read_maturin_pins(root: pth.Path) -> dict[str, str]:
    """Read the maturin version pins from every synchronized location."""
    return {
        # Reuse the dev-dependency reader so "how to read the pyproject pin"
        # lives in exactly one place.
        "pyproject.toml": read_expected_maturin_version(root),
        "build-wheels.yml": require_pin_match(
            _WORKFLOW_PIN_RE.search(
                read_text(root, ".github/workflows/build-wheels.yml"),
            ),
            ".github/workflows/build-wheels.yml",
        ),
        "build-wheels/action.yml": require_pin_match(
            _ACTION_PIN_RE.search(
                read_text(root, ".github/actions/build-wheels/action.yml"),
            ),
            ".github/actions/build-wheels/action.yml",
        ),
    }


def _read_manylinux_aarch64_container_ref(root: pth.Path) -> str:
    """Read the pinned manylinux aarch64 container reference from the workflow."""
    return require_pin_match(
        _AARCH64_CONTAINER_PIN_RE.search(
            read_text(root, ".github/workflows/build-wheels.yml"),
        ),
        ".github/workflows/build-wheels.yml",
        subject="MANYLINUX_AARCH64_CONTAINER pin",
    )


def _workflow_uses_manylinux_aarch64_container_ref(root: pth.Path) -> bool:
    """Report whether the workflow references the pinned manylinux container."""
    workflow = read_text(root, ".github/workflows/build-wheels.yml")
    return _AARCH64_CONTAINER_USAGE_RE.search(workflow) is not None


def test_maturin_pins_are_synchronized() -> None:
    """Maturin version pins stay aligned across CI and dev dependencies."""
    pins = _read_maturin_pins(repo_root())
    assert len(set(pins.values())) == 1, f"Expected one maturin pin, found {pins!r}"


def test_installed_maturin_matches_expected_pin() -> None:
    """The active maturin matches the pinned development dependency.

    Skip on the *module* rather than on a launcher found via `PATH`.
    `build_native_wheel_artifact` runs `python -m maturin` in the current
    interpreter, so an unrelated `maturin` earlier on `PATH` would let this
    test compare a version the build never uses.
    """
    try:
        importlib.import_module("maturin")
    except ImportError:
        pytest.skip("maturin is not installed in this interpreter.")
    expected = read_expected_maturin_version(repo_root())
    installed = im.version("maturin")
    assert installed == expected, (
        f"Expected maturin {expected}, but {installed} is installed"
    )


def test_manylinux_aarch64_container_is_pinned_to_sha256() -> None:
    """Aarch64 manylinux container pin must be immutable."""
    container_ref = _read_manylinux_aarch64_container_ref(repo_root())
    assert MANYLINUX_CONTAINER_SHA256_RE.fullmatch(container_ref), (
        f"Expected SHA-256 pinned container ref, found {container_ref!r}"
    )


def test_manylinux_aarch64_container_is_referenced_by_build_step() -> None:
    """The build job should consume the pinned aarch64 container variable."""
    assert _workflow_uses_manylinux_aarch64_container_ref(repo_root()), (
        "Expected build-wheels.yml to reference env.MANYLINUX_AARCH64_CONTAINER"
    )


@pytest.mark.parametrize(
    "container_ref",
    [
        "ghcr.io/rust-cross/manylinux_2_28-cross:aarch64",
        "ghcr.io/rust-cross/manylinux_2_28-cross:latest",
        "ghcr.io/rust-cross/manylinux_2_28-cross@sha256:tooshort",
        "ghcr.io/rust-cross/manylinux_2_28-cross@sha256:" + "g" * 64,
        "",
    ],
)
def test_manylinux_aarch64_container_ref_rejects_mutable_tag(
    container_ref: str,
) -> None:
    """Aarch64 manylinux container refs reject mutable or invalid pins."""
    assert MANYLINUX_CONTAINER_SHA256_RE.fullmatch(container_ref) is None, (
        f"{container_ref!r} is not an immutable digest pin, so accepting it "
        "would let the aarch64 build pull a different image than the one "
        "reviewed"
    )


def test_manylinux_aarch64_container_pin_regex_rejects_missing_comment() -> None:
    """Aarch64 manylinux container pins require the source-tag comment."""
    yaml_line = (
        "MANYLINUX_AARCH64_CONTAINER: "
        "ghcr.io/rust-cross/manylinux_2_28-cross@sha256:"
        "4864c3e931d790def6dba05cbf133b236b242d0c732f77546c68663c7923116e"
    )

    assert _AARCH64_CONTAINER_PIN_RE.search(yaml_line) is None, (
        "the pin pattern must reject a digest carrying no trailing comment: "
        "the comment records which tag the digest came from, without which a "
        "reviewer cannot tell what a bump is bumping to"
    )


def test_manylinux_aarch64_container_usage_regex_rejects_literal_image() -> None:
    """Aarch64 manylinux container usage requires the pinned env variable."""
    yaml_line = (
        "        container: "
        "ghcr.io/rust-cross/manylinux_2_28-cross@sha256:"
        "4864c3e931d790def6dba05cbf133b236b242d0c732f77546c68663c7923116e"
    )

    assert _AARCH64_CONTAINER_USAGE_RE.search(yaml_line) is None, (
        "the usage pattern must reject a literal image reference: the build "
        "step has to read env.MANYLINUX_AARCH64_CONTAINER, or the pin the "
        "other tests check would not be what the job actually pulls"
    )
