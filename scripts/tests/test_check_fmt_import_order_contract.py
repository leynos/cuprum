"""Keep ``make check-fmt`` able to detect import-order defects.

Ruff's formatter and its import sorter are separate tools with separate
opinions. ``ruff format`` rewrites a file's layout but never reorders an
import block, so a file can be perfectly formatted and still have imports in
an order ``ruff check --select I`` rejects. ``make fmt`` has always repaired
that (it runs ``check --select I --fix``); ``make check-fmt`` did not verify
it, so a contributor could pass the formatting gate with an import block the
lint gate would later reject.

This module holds the recipe to that contract, and it does so by *running*
the check against a file rather than by reading the recipe text: a recipe
assertion passes even when the command it names is a no-op, which is the
failure mode that matters here.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - this test runs the pinned formatter on a scratch fixture.
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"

#: An import block in the wrong order that ``ruff format`` leaves alone.
#: ``os`` sorts after ``io``, so the two lines are transposed.
_UNSORTED_MODULE = '''"""Scratch module with a deliberately unsorted import block."""

from __future__ import annotations

import os
import io


def _probe() -> tuple[object, object]:
    """Return both modules so neither import reads as unused."""
    return os, io
'''


def _recipe_lines(target: str) -> list[str]:
    """Return the tab-indented recipe lines of one Makefile target.

    A rule may name prerequisites and carry an inline ``##`` comment, so the
    target line is matched on its ``name:`` prefix rather than the bare name.

    Parameters
    ----------
    target : str
        The target name, without its trailing colon.

    Returns
    -------
    list[str]
        Each recipe line with its leading tab removed, in file order.
    """
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if line.startswith(f"{target}:") and not line.startswith("\t")
    )
    recipe: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line.lstrip("\t"))
    return recipe


def _ruff_argv() -> list[str]:
    """Build the pinned Ruff invocation the Makefile's ``RUFF`` expands to."""
    uv = shutil.which("uv")
    assert uv is not None, "the formatting contract requires uv on PATH"
    version = next(
        line.split("?=", 1)[1].strip()
        for line in MAKEFILE.read_text(encoding="utf-8").splitlines()
        if line.startswith("RUFF_VERSION ?=")
    )
    return [uv, "tool", "run", "--from", f"ruff=={version}", "ruff"]


def _run_ruff(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run Ruff with a clean environment and a bounded timeout."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_CONFIG_", "RUFF_"))
    }
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell.
        [*_ruff_argv(), *args],
        capture_output=True,
        check=False,
        cwd=cwd,
        env=env,
        text=True,
        timeout=300,
    )


def test_check_fmt_verifies_import_order() -> None:
    """``make check-fmt`` must verify what ``make fmt`` repairs.

    The pairing is the whole point: every ordering rule the formatter
    silently fixes has to be one the checking gate can report, or a
    contributor who only runs ``make check-fmt`` ships a file the lint gate
    rejects.
    """
    recipe = "\n".join(_recipe_lines("check-fmt"))
    assert "check --select I" in recipe, (
        "make check-fmt runs `ruff check --select I --fix`, so it must verify "
        f"the same rule; its recipe is:\n{recipe}"
    )


def test_the_import_order_check_rejects_a_formatted_but_unsorted_file(
    tmp_path: Path,
) -> None:
    """The check must fail on a file the formatter considers well formed.

    This is the non-vacuity half. Asserting the recipe *mentions* the check
    would pass against a command that detects nothing, so the check is run
    for real against a fixture that is formatted correctly and ordered
    incorrectly. ``ruff format --check`` must accept it and
    ``ruff check --select I`` must reject it; if either half stops being
    true the fixture has stopped testing anything and this test says so.
    """
    probe = tmp_path / "unsorted_probe.py"
    probe.write_text(_UNSORTED_MODULE, encoding="utf-8")

    formatted = _run_ruff(["format", "--check", probe.name], tmp_path)
    if formatted.returncode != 0:
        pytest.skip(
            "the fixture is only meaningful while `ruff format` tolerates an "
            f"unsorted import block:\n{formatted.stdout}{formatted.stderr}"
        )

    checked = _run_ruff(["check", "--select", "I", probe.name], tmp_path)
    assert checked.returncode != 0, (
        "`ruff check --select I` accepted an import block that is out of "
        "order, so the formatting gate cannot detect this defect class"
    )
    assert "I001" in checked.stdout, (
        "the import sorter must report I001 for the fixture; it said:\n"
        f"{checked.stdout}"
    )
