"""Integration contracts for the isolated classic and DF12 Pylint passes."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - invokes fixed lint tool contracts.
import sys

import pytest

from tests.helpers.docs import repo_root

pytestmark = pytest.mark.skipif(
    "PYLINT_PYTHON" not in os.environ,
    reason="make pylint-integration supplies the isolated lint environments",
)

_PYLINT = "pylint"
_ASTROID = "astroid"
_DF12_PLUGIN = "df12_python_lints"


def _required_environment(name: str) -> str:
    """Read an integration-contract setting supplied by the Makefile."""
    value = os.environ.get(name)
    assert value, f"make pylint-integration must set {name}"
    return value


def _tool_command(interpreter: str, *arguments: str) -> list[str]:
    """Build the exact pinned classic Pylint tool-environment command."""
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for Pylint integration contracts"
    return [
        uv,
        "tool",
        "run",
        "--python",
        interpreter,
        "--from",
        f"{_PYLINT}=={_required_environment('PYLINT_VERSION')}",
        "--with",
        f"{_ASTROID}=={_required_environment('ASTROID_VERSION')}",
        *arguments,
    ]


def _run_classic(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the baseline Pylint environment and retain its diagnostic output."""
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed tool command.
        _tool_command(_required_environment("PYLINT_PYTHON"), *arguments),
        check=False,
        cwd=repo_root(),
        capture_output=True,
        encoding="utf-8",
    )


def _run_df12(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the separate CPython 3.14 environment with its DF12 plugin."""
    command = _tool_command(_required_environment("DF12_PYTHON"), *arguments)
    command[command.index("python") : command.index("python")] = [
        "--with",
        _required_environment("DF12_PYTHON_LINTS"),
    ]
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed tool command.
        command,
        check=False,
        cwd=repo_root(),
        capture_output=True,
        encoding="utf-8",
    )


def _pylint_arguments(path: pathlib.Path) -> tuple[str, ...]:
    """Select a temporary fixture through the repository's Pylint policy."""
    return ("python", "-m", "pylint", "--jobs=1", str(path))


def test_classic_environment_is_pypy_312_with_pinned_tools() -> None:
    """The classic command uses the required PyPy and package identities."""
    completed = _run_classic(
        "python",
        "-c",
        (
            "import astroid, pylint, sys; "
            "print(sys.implementation.name); "
            "print(sys.version_info[:2]); "
            "print(sys.pypy_version_info[:3]); "
            "print(pylint.__version__); "
            "print(astroid.__version__)"
        ),
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "pypy",
        "(3, 12)",
        "(8, 0, 0)",
        "4.0.8",
        "4.0.4",
    ]


def test_classic_astroid_parses_python_312_generic_syntax(
    tmp_path: pathlib.Path,
) -> None:
    """Astroid builds the expected PEP 695 nodes instead of skipping a module."""
    fixture = tmp_path / "generic_syntax.py"
    fixture.write_text(
        "type Pair[T] = tuple[T, T]\n\n"
        "def identity[T](item: T) -> T:\n"
        "    return item\n",
        encoding="utf-8",
    )
    completed = _run_classic(
        "python",
        "-c",
        (
            "import astroid, pathlib; "
            f"module = astroid.parse(pathlib.Path({str(fixture)!r}).read_text()); "
            "print(type(module.body[0]).__name__); "
            "print(type(module.body[1]).__name__)"
        ),
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["TypeAlias", "FunctionDef"]


def test_classic_pylint_reports_enabled_violation_in_python_312_source(
    tmp_path: pathlib.Path,
) -> None:
    """An enabled classic diagnostic fails a PEP 695 module under PyPy."""
    fixture = tmp_path / "too_long.py"
    fixture.write_text(
        "type Pair[T] = tuple[T, T]\n\n"
        "def identity[T](item: T) -> T:\n"
        "    return item\n" + "# filler\n" * 401,
        encoding="utf-8",
    )
    completed = _run_classic(*_pylint_arguments(fixture))

    assert completed.returncode != 0, completed.stdout
    assert "C0302" in completed.stdout


def test_classic_pylint_fails_loudly_for_invalid_syntax(tmp_path: pathlib.Path) -> None:
    """The effective classic Pylint configuration enables syntax diagnostics."""
    fixture = tmp_path / "invalid_syntax.py"
    fixture.write_text("def broken(:\n    pass\n", encoding="utf-8")
    completed = _run_classic(*_pylint_arguments(fixture))

    assert completed.returncode != 0, completed.stdout
    assert "E0001" in completed.stdout


def test_classic_astroid_inspects_pypy_builtin_descriptors() -> None:
    """PyPy's descriptor paths bootstrap without the retired monkey-patch."""
    completed = _run_classic(
        "python",
        "-c",
        (
            "import astroid, builtins; "
            "module = astroid.MANAGER.ast_from_module_name('builtins'); "
            "assert getattr(builtins.anext, '__text_signature__', None) is None; "
            "assert getattr(list, '__class_getitem__'); "
            "assert module.locals['anext'][0].name == 'anext'; "
            "assert module.locals['list'][0].lookup('__class_getitem__')[0].name "
            "== 'list'; "
            "print('descriptor inspection succeeded')"
        ),
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "descriptor inspection succeeded"


def test_df12_environment_is_cpython_314_and_not_loaded_by_classic() -> None:
    """DF12 stays in its dedicated CPython environment and plugin boundary."""
    classic = _run_classic(
        "python",
        "-c",
        "import importlib.util; "
        f"assert importlib.util.find_spec('{_DF12_PLUGIN}') is None",
    )
    df12 = _run_df12(
        "python",
        "-c",
        (
            "import astroid, pylint, sys; "
            "print(sys.implementation.name); "
            "print(sys.version_info[:2]); "
            "print(pylint.__version__); "
            "print(astroid.__version__)"
        ),
    )

    assert classic.returncode == 0, classic.stderr
    assert df12.returncode == 0, df12.stderr
    assert df12.stdout.splitlines() == ["cpython", "(3, 14)", "4.0.8", "4.0.4"]


def test_df12_diagnostic_and_make_failure_propagate(tmp_path: pathlib.Path) -> None:
    """Both the DF12 plugin and the Makefile propagate a failing Pylint pass."""
    fixture = tmp_path / "bare_assert.py"
    fixture.write_text("assert True\n", encoding="utf-8")
    before = pathlib.Path(sys.executable).resolve()
    df12 = _run_df12(
        *_pylint_arguments(fixture),
        "--disable=all",
        "--load-plugins=df12_python_lints",
        "--enable=C9102",
    )
    make_executable = shutil.which("make")
    assert make_executable is not None, "make is required to verify failure propagation"
    make = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed local Makefile target.
        [make_executable, "--no-print-directory", "PYLINT=false", "pylint-classic"],
        check=False,
        cwd=repo_root(),
        capture_output=True,
        encoding="utf-8",
    )

    assert df12.returncode != 0, df12.stdout
    assert "C9102" in df12.stdout
    assert make.returncode != 0, make.stdout
    assert pathlib.Path(sys.executable).resolve() == before
