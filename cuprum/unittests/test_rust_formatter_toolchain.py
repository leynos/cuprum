"""Contracts for the Rust formatter's maintenance toolchain."""

from __future__ import annotations

import tomllib

from tests.helpers.docs import repo_root
from tests.helpers.workflow import Workflow, step_named, steps

_SHARED_ACTION_REVISION = "c5a54701c8603a0fa756a6b34c49bc2af75a6c11"
_SETUP_RUST = (
    f"leynos/shared-actions/.github/actions/setup-rust@{_SHARED_ACTION_REVISION}"
)
_FORMATTER_TOOLCHAIN = "nightly-2026-05-28"
_PROJECT_TOOLCHAIN = "1.85.0"


def test_formatter_toolchain_precedes_the_project_toolchain(
    workflow_data: Workflow,
) -> None:
    """The cold CI path installs the formatter without changing project Rust."""
    formatter_setup = step_named(
        workflow_data, "lint-test", "Install formatter Rust toolchain"
    )
    project_setup = step_named(
        workflow_data, "lint-test", "Install project Rust toolchain"
    )

    assert formatter_setup.get("uses") == _SETUP_RUST, (
        "the formatter toolchain must use the pinned shared Rust setup action"
    )
    assert formatter_setup.get("with") == {
        "toolchain": _FORMATTER_TOOLCHAIN,
        "cache-provider": "external",
        "use-sccache": "false",
    }, "the formatter setup must provision the pinned nightly on every runner"
    assert project_setup.get("with") == {
        "toolchain": _PROJECT_TOOLCHAIN,
        "cache-provider": "external",
        "use-sccache": "false",
    }, "the project setup must restore the supported stable compiler"

    lint_steps = steps(workflow_data, "lint-test")
    assert lint_steps.index(formatter_setup) < lint_steps.index(project_setup), (
        "the stable project toolchain must replace setup-rust's formatter override"
    )


def test_project_toolchain_declares_maintenance_components() -> None:
    """The stable toolchain keeps editor and lint components available locally."""
    configuration = tomllib.loads(
        (repo_root() / "rust" / "rust-toolchain.toml").read_text(encoding="utf-8")
    )
    toolchain = configuration.get("toolchain")
    assert isinstance(toolchain, dict), "rust-toolchain.toml must declare a toolchain"
    assert toolchain == {
        "channel": _PROJECT_TOOLCHAIN,
        "profile": "minimal",
        "components": ["rustfmt", "clippy", "rust-analyzer"],
    }, "the stable pin must retain its compiler and declare each required component"
