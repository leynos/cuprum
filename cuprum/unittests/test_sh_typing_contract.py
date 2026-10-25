"""Static-check contract tests for the ``sh.make`` builder signature.

The positive half of the contract lives in
``cuprum/unittests/typing_fixtures/`` and is checked as ordinary source by the
repository-wide ``make typecheck`` run. This module covers the negative half:
calls and stream uses the checker must reject. They cannot live in the
repository, because a file that fails the checker fails the gate, so each one
is written to ``tmp_path`` and handed to ty in a subprocess.

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

#: The positive fixtures checked into the repository.
_VALID_FIXTURE = _REPO_ROOT / "cuprum" / "unittests" / "typing_fixtures"

#: Invalid snippets and the ty diagnostic codes each one must produce. Most
#: entries reject builder arguments; the captured-output cases pin the stream
#: types exposed by ``SafeCmd.run`` and ``run_sync``.
_INVALID_CALLS: typ.Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "object-positional": ("b(object())", ("invalid-argument-type",)),
    "object-keyword": ("b(flag=object())", ("invalid-argument-type",)),
    "none-positional": ("b(None)", ("invalid-argument-type",)),
    "none-keyword": ("b(flag=None)", ("invalid-argument-type",)),
    "bytes-positional": ("b(b'raw')", ("invalid-argument-type",)),
    "list-positional": ("b(['a', 'b'])", ("invalid-argument-type",)),
    "bytes-path-like": (
        (
            "class BytesPath:\n"
            "    def __fspath__(self) -> bytes:\n"
            "        return b'raw'\n"
            "\n"
            "\n"
            "b(BytesPath())"
        ),
        ("invalid-argument-type",),
    ),
    "optional-output-assigned-to-str": (
        (
            "from cuprum.sh import RunOutputOptions\n"
            "result: str = b().run_sync(output=RunOutputOptions()).stdout"
        ),
        ("invalid-assignment",),
    ),
    "optional-output-string-method": (
        (
            "from cuprum.sh import RunOutputOptions\n"
            "b().run_sync(output=RunOutputOptions()).stdout.strip()"
        ),
        ("unresolved-attribute",),
    ),
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
            str(workdir),
            "--project",
            str(workdir),
            "--config-file",
            str(_VALID_FIXTURE / "ty.toml"),
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
    """Copy every positive typing fixture into an isolated directory."""
    target = tmp_path / "typing_fixtures"
    shutil.copytree(_VALID_FIXTURE, target)
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


def test_positive_fixtures_type_check_cleanly(fixture_copy: Path) -> None:
    """Every valid call and return type in the fixtures is accepted."""
    result = _run_ty(fixture_copy)

    assert result.returncode == 0, (
        f"the positive fixture must type-check cleanly:\n{result.stdout}"
    )
    assert "unresolved-import" not in result.stdout, (
        f"cuprum must resolve from sys.prefix, got:\n{result.stdout}"
    )


@pytest.mark.parametrize("name", sorted(_INVALID_CALLS), ids=sorted(_INVALID_CALLS))
def test_invalid_calls_are_rejected(name: str, tmp_path: Path) -> None:
    """Each invalid call or stream use reports its declared diagnostic."""
    snippet_body, expected_codes = _INVALID_CALLS[name]
    snippet = tmp_path / f"{name}.py"
    snippet.write_text(
        f"{_SNIPPET_HEADER}{snippet_body}\n",
        encoding="utf-8",
    )

    result = _run_ty(tmp_path)
    output = result.stdout + result.stderr

    assert result.returncode != 0, (
        f"ty must reject the {name} call, but it exited 0:\n{output}"
    )
    for code in expected_codes:
        assert code in output, f"ty must report {code} for {name}, got:\n{output}"
    assert f"{name}.py" in output, (
        f"the diagnostic must name the offending file, got:\n{output}"
    )
    assert "unresolved-import" not in output, (
        f"cuprum must resolve from sys.prefix, got:\n{output}"
    )


@pytest.mark.parametrize(
    ("call", "expected_code"),
    [("b('valid')", 0), ("b(object())", 1)],
    ids=["valid-call", "invalid-call"],
)
def test_checker_ignores_ancestor_project(
    tmp_path: Path, call: str, expected_code: int
) -> None:
    """Ancestor projects cannot add files or suppress fixture diagnostics."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "unrelated"\nversion = "0.0.0"\n'
        '[tool.ty.rules]\ninvalid-argument-type = "ignore"\n',
        encoding="utf-8",
    )
    (tmp_path / "unrelated.py").write_text("missing_ancestor_name\n", encoding="utf-8")
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    (fixture_dir / "call.py").write_text(f"{_SNIPPET_HEADER}{call}\n", encoding="utf-8")

    result = _run_ty(fixture_dir)
    output = result.stdout + result.stderr

    assert result.returncode == expected_code, output
    assert "unrelated.py" not in output, output
    assert "unresolved-import" not in output, output
    if expected_code:
        assert "invalid-argument-type" in output, output
        assert "call.py" in output, output
