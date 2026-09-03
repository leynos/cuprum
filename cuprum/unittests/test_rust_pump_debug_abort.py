"""Regression tests for Rust-pump descriptor ownership in debug builds."""

from __future__ import annotations

import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - invokes a fixed Python/pytest command in a temp dir.
import sys
import typing as typ
import zipfile

import pytest

from tests.helpers.docs import repo_root
from tests.helpers.extension_requirement import (
    REQUIRE_EXTENSION_ENV,
    missing_extension_message,
)
from tests.helpers.maturin import (
    build_debug_native_wheel_artefact,
    maturin_script_locatable,
    toolchain_available,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def _debug_extension_build_is_available() -> bool:
    """Return whether this interpreter can build the debug extension."""
    return toolchain_available() and maturin_script_locatable()


def _extract_debug_wheel(wheel_path: Path, destination: Path) -> None:
    """Extract the wheel so a child imports its extension ahead of this checkout."""
    with zipfile.ZipFile(wheel_path) as wheel:
        wheel.extractall(destination)


@pytest.mark.timeout(0)
def test_debug_rust_pump_does_not_abort_on_early_exit_or_timeout(
    tmp_path: Path,
) -> None:
    """A debug extension survives the two writer-ownership failure paths."""
    build_available = _debug_extension_build_is_available()
    required_message = missing_extension_message(
        required=bool(os.environ.get(REQUIRE_EXTENSION_ENV)),
        available=build_available,
    )
    if required_message is not None:
        pytest.fail(required_message)
    if not build_available:
        pytest.skip("Rust toolchain or maturin script unavailable.")

    root = repo_root()
    wheel_path = build_debug_native_wheel_artefact(root, tmp_path / "wheelhouse")
    extension_root = tmp_path / "debug-extension"
    _extract_debug_wheel(wheel_path, extension_root)
    environment = os.environ | {
        "CUPRUM_STREAM_BACKEND": "rust",
        "PYTHONPATH": os.pathsep.join((str(extension_root), str(root))),
    }
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed test-node argument vector.
        [
            sys.executable,
            "-m",
            "pytest",
            str(root / "cuprum/unittests/test_stream_parity.py") + "::"
            "TestStreamParity::test_broken_pipe_downstream_early_exit[rust-backend]",
            str(root / "cuprum/unittests/test_pipeline.py") + "::"
            "test_pipeline_timeout_raises_timeout_expired[rust-backend]",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )

    assert completed.returncode not in {-6, -11}, (
        "the debug extension must not abort or segfault on early exit; "
        f"stderr:\n{completed.stderr}"
    )
    assert completed.returncode == 0, (
        "the debug extension child must pass the broken-pipe and timeout "
        f"scenarios; stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
