"""Static-check contract tests for the ``sh.make`` builder signature.

The positive half of the contract is a checked-in fixture,
``cuprum/unittests/typing_fixtures/sh_make_valid.py``, which the
repository-wide ``make typecheck`` run checks as ordinary source. This module
covers the negative half: calls the checker must reject. They cannot live in
the repository, because a file that fails the checker fails the gate, so each
one is written to ``tmp_path`` and handed to ty in a subprocess.

The subprocess runs ty at the interpreter that is executing the tests
(``sys.prefix``), so it resolves ``cuprum`` from the same environment the rest
of the suite uses rather than from whatever the ambient ``PATH`` offers.
"""

from __future__ import annotations

import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - runs the pinned checker on generated files.
import sys
import typing as typ
from pathlib import Path

import pytest

#: The repository root, two levels above this module (``cuprum/unittests``).
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The positive fixture checked into the repository.
_VALID_FIXTURE = _REPO_ROOT / "cuprum" / "unittests" / "typing_fixtures"

#: Calls the published ``SafeCmdBuilder`` contract must reject.
_INVALID_CALLS: typ.Final[dict[str, str]] = {
    "object-positional": "b(object())",
    "object-keyword": "b(flag=object())",
    "none-positional": "b(None)",
    "none-keyword": "b(flag=None)",
    "bytes-positional": "b(b'raw')",
    "list-positional": "b(['a', 'b'])",
}

_SNIPPET_HEADER = "from cuprum import ECHO, sh\n\nb = sh.make(ECHO)\n"


def _ty_executable() -> str:
    """Return the ty executable bound to the running interpreter's venv."""
    candidate = Path(sys.prefix) / "bin" / "ty"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("ty")
    assert found is not None, "ty must be available on PATH or in sys.prefix"
    return found


def _run_ty(workdir: Path) -> subprocess.CompletedProcess[str]:
    """Type-check ``workdir`` with the pinned ty and return the process result."""
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv
        [
            _ty_executable(),
            "check",
            "--python",
            sys.prefix,
            "--output-format",
            "concise",
        ],
        shell=False,
        cwd=workdir,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


@pytest.fixture
def fixture_copy(tmp_path: Path) -> Path:
    """Copy the positive fixture into ``tmp_path`` for isolated checking."""
    target = tmp_path / "sh_make_valid.py"
    shutil.copy(_VALID_FIXTURE / "sh_make_valid.py", target)
    return target


def test_ty_is_available_to_the_suite() -> None:
    """The test harness can locate the ty executable it asserts against."""
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv
        [_ty_executable(), "--version"],
        shell=False,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"ty --version failed: {result.stderr}"
    assert "0.0.74" in result.stdout, (
        f"the suite must run against the pinned ty, got {result.stdout.strip()!r}"
    )


def test_positive_fixture_type_checks_clean(fixture_copy: Path) -> None:
    """Every valid call in the checked-in fixture is accepted."""
    result = _run_ty(fixture_copy.parent)

    assert result.returncode == 0, (
        f"the positive fixture must type-check cleanly:\n{result.stdout}"
    )
    assert "unresolved-import" not in result.stdout, (
        f"cuprum must resolve from sys.prefix, got:\n{result.stdout}"
    )


@pytest.mark.parametrize("name", sorted(_INVALID_CALLS), ids=sorted(_INVALID_CALLS))
def test_invalid_calls_are_rejected(name: str, tmp_path: Path) -> None:
    """An unsupported argument is rejected at the call site, not at runtime."""
    snippet = tmp_path / f"{name}.py"
    snippet.write_text(
        f"{_SNIPPET_HEADER}{_INVALID_CALLS[name]}\n",
        encoding="utf-8",
    )

    result = _run_ty(tmp_path)
    output = result.stdout + result.stderr

    assert result.returncode != 0, (
        f"ty must reject {_INVALID_CALLS[name]!r}, but it exited 0:\n{output}"
    )
    assert "invalid-argument-type" in output, (
        f"ty must report invalid-argument-type for {name}, got:\n{output}"
    )
    assert f"{name}.py" in output, (
        f"the diagnostic must name the offending file, got:\n{output}"
    )
    assert "unresolved-import" not in output, (
        f"cuprum must resolve from sys.prefix, got:\n{output}"
    )
