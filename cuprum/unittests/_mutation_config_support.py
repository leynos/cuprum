"""Readers and the copied-input inventory for the mutation harness contract.

Three contract modules check different faces of the same declaration in
``pyproject.toml`` — the inputs the sandbox must carry, the test selection, and
the shared preflight — so the readers and the audited inventory live here
rather than being copied into each of them.
"""

from __future__ import annotations

import dataclasses as dc
import tomllib

import pytest

from tests.helpers.docs import repo_root


@dc.dataclass(frozen=True, slots=True)
class CopiedInput:
    """One repository-root input the mutation baseline requires."""

    path: str
    reader: str
    #: True for a directory entry, written with a trailing slash in TOML.
    is_directory: bool


#: The complete inventory of ``also_copy`` entries, in declaration order.
#: Each ``reader`` names the test or helper that fails without the input.
COPIED_INPUTS: tuple[CopiedInput, ...] = (
    CopiedInput(
        "benchmarks/",
        "the benchmark and profiling tests import this package",
        is_directory=True,
    ),
    CopiedInput(
        "docs/",
        "the documentation contract tests read these files",
        is_directory=True,
    ),
    CopiedInput(
        ".github/",
        "the workflow and action contract tests read these files",
        is_directory=True,
    ),
    CopiedInput(
        "scripts/",
        "tests/helpers and the release contracts import this package",
        is_directory=True,
    ),
    CopiedInput(
        "tools/dev-fast/",
        "test_dev_fast_contract and test_dev_fast_adapter read the fragment",
        is_directory=True,
    ),
    CopiedInput(
        "tools/mold/",
        "test_dev_fast_prerequisites and the Makefile read VERSION",
        is_directory=True,
    ),
    CopiedInput(
        "tools/rust-prover-tools/",
        "test_dev_fast_contract dry-runs the Makefile, which parses REF",
        is_directory=True,
    ),
    CopiedInput(
        "rust/.config/",
        "the timeout-lane helpers read nextest.toml",
        is_directory=True,
    ),
    CopiedInput(
        "rust/cuprum-rust/",
        "the wheel-build snapshot test builds from this crate",
        is_directory=True,
    ),
    CopiedInput(
        "rust/cuprum-streams/",
        "the wheel-build snapshot test builds from this crate",
        is_directory=True,
    ),
    CopiedInput(
        "rust/cuprum-native-io/",
        "the wheel-build snapshot test builds from this crate",
        is_directory=True,
    ),
    CopiedInput(
        "conftest.py",
        "the shared root fixture module is loaded by pytest",
        is_directory=False,
    ),
    CopiedInput(
        "README.md",
        "the documentation examples are read during collection",
        is_directory=False,
    ),
    CopiedInput(
        ".yamllint.yml",
        "test_workflow_lint_provisioning reads the lint policy",
        is_directory=False,
    ),
    CopiedInput(
        ".concordat",
        "the Rust build-footprint contracts read the build defaults",
        is_directory=False,
    ),
    CopiedInput(
        "CHANGELOG.md",
        "test_changelog_records_cleanup_telemetry_contract reads the log",
        is_directory=False,
    ),
    CopiedInput(
        "Makefile",
        "test_toolchain_pins reads the tool-version defaults",
        is_directory=False,
    ),
    CopiedInput(
        "pyproject.toml",
        "the harness configuration and dependency-pin tests read it",
        is_directory=False,
    ),
    CopiedInput(
        "rust/Cargo.toml",
        "the Rust build-footprint contracts read the workspace manifest",
        is_directory=False,
    ),
    CopiedInput(
        "rust/Cargo.lock",
        "the Rust build-footprint contracts read the lock file",
        is_directory=False,
    ),
    CopiedInput(
        "rust/clippy.toml",
        "the Rust lint contracts read the Clippy configuration",
        is_directory=False,
    ),
    CopiedInput(
        "rust/dylint.toml",
        "the Rust lint contracts read the Dylint configuration",
        is_directory=False,
    ),
    CopiedInput(
        "rust/rust-toolchain.toml",
        "the toolchain pins and build-footprint contracts read the toolchain",
        is_directory=False,
    ),
)


def _table(name: str) -> dict[str, object]:
    """Return the named table under ``[tool]``, failing when it is absent."""
    pyproject = tomllib.loads(
        (repo_root() / "pyproject.toml").read_text(encoding="utf-8")
    )
    match pyproject["tool"][name]:
        case dict() as table:
            return table
        case _:
            pytest.fail(f"pyproject.toml must declare [tool.{name}]")


def mutmut_table() -> dict[str, object]:
    """Return the ``[tool.mutmut]`` table declared in ``pyproject.toml``."""
    return _table("mutmut")


def preflight_table() -> dict[str, object]:
    """Return the ``[tool.mutation-preflight]`` table declared in ``pyproject.toml``."""
    return _table("mutation-preflight")


def string_list(table: dict[str, object], key: str) -> list[str]:
    """Return one string-list setting, failing when it is absent or mistyped."""
    match table.get(key):
        case list() as entries:
            return entries
        case _:
            pytest.fail(f"the declared table must carry a {key} list")
