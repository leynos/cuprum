"""Shared fixtures for the benchmark baseline fetch CLI tests."""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import pathlib as pth

ARTEFACT_NAME = "benchmark-ratchet-main-baseline"


def main_cli_args(output_dir: pth.Path) -> list[str]:
    """Return CLI arguments for invoking the baseline fetch command."""
    return [
        "--repository",
        "leynos/cuprum",
        "--workflow",
        "ci.yml",
        "--artifact-name",
        ARTEFACT_NAME,
        "--output-dir",
        str(output_dir),
    ]
