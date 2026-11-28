"""The required-outcomes guard fails on every outcome the macOS leg forbids.

``tests/helpers/required_outcomes.py`` is the plugin that lets the advisory
macOS leg treat a skip as a failure. Its value is discrimination: an ordinary
pytest run treats a skip, an xfail, and an empty selection as success, so a
guard that inherited any of those semantics would report a green leg that
proved nothing. These controls run synthetic suites in a subprocess — one
outcome per case — and check both the exit status and the diagnostic that names
the case.

The last two controls pin the difference. The same synthetic suite passes
without the plugin, so the outcomes are ordinary pytest behaviour; and a
distributed run is refused, because the guard cannot observe a skip in another
worker.
"""

from __future__ import annotations

import os
import pathlib as pth
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed pytest argv.
import sys
import textwrap

import pytest

ROOT = pth.Path(__file__).resolve().parents[1]
_PLUGIN = "tests.helpers.required_outcomes"
_MODULE = "test_case.py"
_PASSES = "def test_passes() -> None:\n    assert True\n"


def _run(
    tmp_path: pth.Path,
    source: str,
    *arguments: str,
    plugin: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run pytest over one synthetic suite and return the completed process."""
    (tmp_path / _MODULE).write_text(textwrap.dedent(source), encoding="utf-8")
    # An explicit config keeps rootdir here: a stray config above the temporary
    # directory (a machine-wide /tmp/pyproject.toml, for instance) would
    # otherwise supply addopts and plugins that have nothing to do with this
    # suite.
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-c",
        "pytest.ini",
        "--color=no",
        "-p",
        "no:cacheprovider",
    ]
    if plugin:
        command.extend(("-p", _PLUGIN))
    command.extend(arguments)
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        command,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _output(completed: subprocess.CompletedProcess[str]) -> str:
    """Return both streams so a failed expectation shows pytest's own report."""
    return f"{completed.stdout}\n{completed.stderr}"


def test_a_passing_required_outcome_exits_zero(tmp_path: pth.Path) -> None:
    """The guard accepts the outcome it exists to require."""
    completed = _run(tmp_path, _PASSES, "--required-outcome", f"{_MODULE}::test_passes")
    assert completed.returncode == 0, _output(completed)


_FORBIDDEN = (
    pytest.param(
        "import pytest\n\n"
        "@pytest.mark.skip(reason='deliberate')\n"
        "def test_skipped() -> None:\n"
        "    pass\n",
        "skipped",
        "test_skipped",
        id="skip",
    ),
    pytest.param(
        "def test_failing() -> None:\n    assert False\n",
        "failed",
        "test_failing",
        id="failure",
    ),
    pytest.param(
        "import pytest\n\n"
        "@pytest.mark.xfail(reason='deliberate')\n"
        "def test_expected_failure() -> None:\n"
        "    assert False\n",
        "xfail",
        "test_expected_failure",
        id="xfail",
    ),
    pytest.param(
        "import pytest\n\n"
        "@pytest.mark.xfail(reason='deliberate')\n"
        "def test_unexpected_pass() -> None:\n"
        "    assert True\n",
        "xpass",
        "test_unexpected_pass",
        id="xpass",
    ),
)


@pytest.mark.parametrize(("source", "category", "name"), _FORBIDDEN)
def test_an_outcome_the_guard_forbids_fails(
    tmp_path: pth.Path,
    source: str,
    category: str,
    name: str,
) -> None:
    """Each forbidden outcome fails the run and is named by its category."""
    completed = _run(
        tmp_path,
        source + "\n" + _PASSES,
        "--required-outcome",
        f"{_MODULE}::test_passes",
    )
    output = _output(completed)
    assert completed.returncode != 0, output
    assert f"  {category} (1)" in output, output
    assert f"{_MODULE}::{name}" in output, output


def test_a_missing_required_outcome_is_named(tmp_path: pth.Path) -> None:
    """A promised case that was never collected fails rather than passes."""
    completed = _run(tmp_path, _PASSES, "--required-outcome", f"{_MODULE}::test_absent")
    output = _output(completed)
    assert completed.returncode != 0, output
    assert "  missing-required-outcome (1)" in output, output
    assert f"{_MODULE}::test_absent" in output, output


def test_a_skipped_required_outcome_is_named_twice(tmp_path: pth.Path) -> None:
    """A skip breaks both the no-skip promise and the promised-pass promise."""
    skipped = (
        "@pytest.mark.skip(reason='deliberate')\n"
        "def test_skipped() -> None:\n"
        "    pass\n"
    )
    completed = _run(
        tmp_path,
        f"import pytest\n\n{skipped}\n{_PASSES}",
        "--required-outcome",
        f"{_MODULE}::test_skipped",
    )
    output = _output(completed)
    assert completed.returncode != 0, output
    assert "  skipped (1)" in output, output
    assert "  required-outcome-not-passed (1)" in output, output


def test_an_empty_selection_fails(tmp_path: pth.Path) -> None:
    """A suite that collected nothing proves nothing, whatever its exit code."""
    completed = _run(tmp_path, "def helper() -> None:\n    pass\n")
    output = _output(completed)
    assert completed.returncode != 0, output
    assert "  empty-selection" in output, output


def test_a_collection_error_is_named(tmp_path: pth.Path) -> None:
    """An unreadable module is reported as a collection error, not a pass."""
    completed = _run(
        tmp_path,
        "import a_module_that_does_not_exist\n\ndef test_never() -> None:\n    pass\n",
    )
    output = _output(completed)
    assert completed.returncode != 0, output
    assert "  collection-error (1)" in output, output


def test_a_module_level_skip_is_named(tmp_path: pth.Path) -> None:
    """A skipped module removes cases without reporting a skipped test."""
    (tmp_path / "test_skipped_module.py").write_text(
        "import pytest\n\n"
        "pytest.skip('this host cannot observe process state', "
        "allow_module_level=True)\n",
        encoding="utf-8",
    )
    completed = _run(tmp_path, _PASSES, "--required-outcome", f"{_MODULE}::test_passes")
    output = _output(completed)
    assert completed.returncode != 0, output
    assert "  collection-skip (1)" in output, output
    assert "test_skipped_module.py" in output, output


_DESELECTION_CASES = (
    (
        "unexpected-deselection",
        f"{_PASSES}\ndef test_other() -> None:\n    assert True\n",
        ("--deselect", f"{_MODULE}::test_other"),
        f"{_MODULE}::test_other",
    ),
    (
        "stale-deselection",
        _PASSES,
        (
            "--deselect",
            f"{_MODULE}::test_absent",
            "--expected-deselect",
            f"{_MODULE}::test_absent",
        ),
        f"{_MODULE}::test_absent",
    ),
)


@pytest.mark.parametrize(
    "case",
    _DESELECTION_CASES,
    ids=["unexpected", "stale"],
)
def test_a_deselection_the_manifest_did_not_predict_fails(
    tmp_path: pth.Path,
    case: tuple[str, str, tuple[str, ...], str],
) -> None:
    """An unpredicted removal, or a predicted one that matched nothing, fails."""
    category, source, deselection, named = case
    completed = _run(
        tmp_path,
        source,
        *deselection,
        "--required-outcome",
        f"{_MODULE}::test_passes",
    )
    output = _output(completed)
    assert completed.returncode != 0, output
    assert f"  {category} (1)" in output, output
    assert named in output, output


def test_an_expected_deselection_is_accepted(tmp_path: pth.Path) -> None:
    """The manifest's predictable removal is the one skip the leg tolerates."""
    completed = _run(
        tmp_path,
        "import pytest\n\n"
        "@pytest.mark.skip(reason='deliberate')\n"
        "def test_skipped() -> None:\n"
        "    pass\n"
        "\n"
        f"{_PASSES}",
        "--deselect",
        f"{_MODULE}::test_skipped",
        "--expected-deselect",
        f"{_MODULE}::test_skipped",
        "--required-outcome",
        f"{_MODULE}::test_passes",
    )
    output = _output(completed)
    assert completed.returncode == 0, output
    assert "1 expected deselection(s) matched" in output, output


def test_a_caught_skip_exception_is_an_ordinary_pass(tmp_path: pth.Path) -> None:
    """A case that catches ``pytest.skip`` internally passed on its merits."""
    completed = _run(
        tmp_path,
        "import pytest\n\n"
        "def test_caught_skip() -> None:\n"
        "    with pytest.raises(pytest.skip.Exception):\n"
        "        pytest.skip('caught inside the case')\n",
        "--required-outcome",
        f"{_MODULE}::test_caught_skip",
    )
    assert completed.returncode == 0, _output(completed)


def test_without_the_plugin_pytest_accepts_skips_and_xfails(tmp_path: pth.Path) -> None:
    """The outcomes are normal pytest behaviour; the plugin is the difference."""
    completed = _run(
        tmp_path,
        "import pytest\n\n"
        "@pytest.mark.skip(reason='deliberate')\n"
        "def test_skipped() -> None:\n"
        "    pass\n"
        "\n"
        "@pytest.mark.xfail(reason='deliberate')\n"
        "def test_expected_failure() -> None:\n"
        "    assert False\n",
        plugin=False,
    )
    assert completed.returncode == 0, _output(completed)


def test_a_distributed_run_is_refused(tmp_path: pth.Path) -> None:
    """A worker's skip is invisible here, so the guard refuses to distribute."""
    pytest.importorskip("xdist")
    completed = _run(
        tmp_path,
        _PASSES,
        "-n",
        "2",
        "--required-outcome",
        f"{_MODULE}::test_passes",
    )
    output = _output(completed)
    assert completed.returncode == 4, output
    assert "refuses a distributed run" in output, output
