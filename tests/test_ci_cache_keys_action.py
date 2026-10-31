"""Execute the cache-key action so its own rendering is under test.

`tests/helpers/ci_cache_families.py` re-implements the renderer to discover
which family a save step publishes, and its tests pin that mirror. A mirror can
drift: if the action's shell changed the version scope and the Python reader did
not, every ownership test would stay green while the workflow restored stale
compiler output. These tests run the action's own step under `bash` and read
what it appends to `GITHUB_ENV`, so the shell itself is the subject.

The modern-stable family is the one that gained new scoping in this change, so
it is exercised here, together with both error paths that keep a version scope
honest.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.composite_actions import StepResult, run_step, step_script

if typ.TYPE_CHECKING:
    from pathlib import Path

CACHE_KEYS_ACTION = ".github/actions/cache-keys"
RENDER_STEP = "Render the cache keys"
RUNNER_SCOPE = "v1-Linux-X64-github-hosted"
IMAGE_SCOPE = f"{RUNNER_SCOPE}-ubuntu2404"
BASE_ENVIRONMENT = {
    "RUNNER_OS": "Linux",
    "RUNNER_ARCH": "X64",
    "RUNNER_ENVIRONMENT": "github-hosted",
    "GENERATION": "v1",
    "UBUNTU_RELEASE": "ubuntu2404",
    "PYTHON_VERSION": "3.13",
    "CARGO_HASH": "cargo-hash",
    "TOOL_HASH": "tool-hash",
    "GITHUB_RUN_ID": "42",
}


def _render(workdir: Path, **overrides: str) -> StepResult:
    """Run the renderer with the base environment plus any case overrides."""
    return run_step(
        step_script(CACHE_KEYS_ACTION, RENDER_STEP),
        workdir=workdir,
        environment={**BASE_ENVIRONMENT, **overrides},
    )


def test_modern_stable_renders_a_version_scoped_compiler_family(
    tmp_path: Path,
) -> None:
    """Append the exact Rust version so a toolchain bump cannot reuse output."""
    completed = _render(
        tmp_path, COMPILER_SHAPE="modern-stable", COMPILER_VERSION="1.99.1"
    )
    assert completed.returncode == 0, completed.stderr
    prefix = f"sccache-{IMAGE_SCOPE}-py3.13-modern-stable-rust1.99.1-"
    assert completed.exported["SCCACHE_CACHE_PREFIX"] == prefix, (
        "the modern-stable family must carry its explicit compiler version"
    )
    assert completed.exported["SCCACHE_CACHE_KEY"] == f"{prefix}42", (
        "the save key must extend the scoped prefix with the run identifier"
    )
    assert completed.exported["CARGO_CACHE_KEY"] == (
        f"cargo-{RUNNER_SCOPE}-cargo-hash"
    ), "the Cargo registry family must stay independent of the build shape"
    assert completed.exported["TOOL_CACHE_KEY"] == (
        f"tool-{IMAGE_SCOPE}-py3.13-tool-hash"
    ), "the tool family must follow the interpreter and runner image"


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    [
        (
            {"COMPILER_SHAPE": "modern-stable", "COMPILER_VERSION": ""},
            "modern-stable compiler shape needs an exact compiler-version",
        ),
        (
            {"COMPILER_SHAPE": "debug", "COMPILER_VERSION": "1.99.1"},
            "compiler-version is only valid with the modern-stable shape",
        ),
    ],
    ids=["modern-stable without a version", "a version outside modern stable"],
)
def test_a_version_scope_outside_the_rules_fails_the_action(
    tmp_path: Path, overrides: dict[str, str], expected_error: str
) -> None:
    """Refuse a key that cannot describe its own contents."""
    completed = _render(tmp_path, **overrides)
    assert completed.returncode != 0, f"the action must fail: {expected_error}"
    assert expected_error in completed.stderr, (
        "the action must explain why the version scope is invalid"
    )
    assert "SCCACHE_CACHE_KEY" not in completed.exported, (
        "a rejected render must not publish a key it could not scope"
    )
