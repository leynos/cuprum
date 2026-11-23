"""Behavioural tests for the opt-in pytest session-evidence plugin.

The plugin exists to measure the suite without changing it, so the tests assert
the two properties that make it safe to enable in CI: a run that does not ask
for evidence writes nothing and behaves identically, and a run that does ask
produces a record describing what actually happened. Each test drives a real
`pytest` process over a tiny generated suite, because the plugin's hooks only
run inside a session and a double would prove nothing about the real one.
"""

from __future__ import annotations

import json
import os
import pathlib as pth
import subprocess  # ruff: ignore[suspicious-subprocess-import] - drive real pytest sessions.
import sys
import typing as typ

from tests.helpers.pytest_session_evidence import (
    EVIDENCE_ENV,
    PLUGIN_NAME,
    SCHEMA_VERSION,
    SessionEvidence,
    evidence_directory,
    record_path,
    register,
    write_record,
)

if typ.TYPE_CHECKING:
    import pytest

_PASSING_SUITE = '''"""One passing test."""


def test_passes() -> None:
    """Pass, so the record has a call outcome."""
'''

_MIXED_SUITE = '''"""A suite with one of every outcome the record must distinguish."""

import pytest


def test_passes() -> None:
    """Pass, so a record cannot be built only from failures."""


def test_fails() -> None:
    """Fail, so the record carries a failure and a nonzero status."""
    assert False, "deliberate failure"


def test_skips() -> None:
    """Skip, so the record carries a reason."""
    pytest.skip("deliberate skip")


@pytest.mark.xfail(reason="deliberate xfail", strict=True)
def test_xfails() -> None:
    """Fail in a way the run expects, so the record carries an xfail reason."""
    assert False, "deliberate xfail"


def test_errors() -> None:
    """Error during setup, so the record carries an error outcome."""
    raise RuntimeError("deliberate setup error")
'''


def _write(tmp_path: pth.Path, name: str, body: str) -> pth.Path:
    """Write one generated test module and return its path."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _write_config(directory: pth.Path) -> pth.Path:
    """Write an empty pytest configuration beside a generated suite."""
    config = directory / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    return config


def _run(
    suite: pth.Path,
    evidence_dir: pth.Path | None,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    """Run the real pytest over one generated suite.

    Parameters
    ----------
    suite : pathlib.Path
        The generated test module to run.
    evidence_dir : pathlib.Path or None
        Evidence directory to request, or ``None`` to leave the plugin off.
    *extra : str
        Further pytest arguments, such as ``-n 2``.

    Returns
    -------
    subprocess.CompletedProcess of str
        The finished process, with captured output.
    """
    environment = {
        key: value for key, value in os.environ.items() if key != EVIDENCE_ENV
    }
    if evidence_dir is not None:
        environment[EVIDENCE_ENV] = str(evidence_dir)
    repository_root = pth.Path(__file__).resolve().parents[1]
    environment["PYTHONPATH"] = str(repository_root)
    # The generated suite lives under the system temporary directory, and a
    # configuration file there (a stray `pyproject.toml` is a known local
    # hazard) would contribute its own `addopts` and plugins. Pin the config and
    # the root so the run exercises this plugin and nothing else.
    config = _write_config(suite.parent)
    arguments = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        "tests.helpers.pytest_session_evidence",
        "-c",
        str(config),
        f"--rootdir={suite.parent}",
        *extra,
        str(suite),
    ]
    # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv; no shell
    return subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=180,
    )


def _only_record(evidence_dir: pth.Path) -> dict[str, object]:
    """Return the single record written into ``evidence_dir``."""
    records = sorted(evidence_dir.glob("pytest-evidence-*.json"))
    assert len(records) == 1, f"expected exactly one record, got {records}"
    return json.loads(records[0].read_text(encoding="utf-8"))


def test_a_run_that_does_not_ask_for_evidence_writes_nothing(
    tmp_path: pth.Path,
) -> None:
    """Leave the suite untouched when the variable is absent.

    The whole experiment depends on this: an always-on plugin would perturb the
    control arm it is supposed to measure.
    """
    suite = _write(tmp_path, "test_one.py", _PASSING_SUITE)
    evidence_dir = tmp_path / "evidence"
    completed = _run(suite, None)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not evidence_dir.exists(), "an unrequested run created evidence"


def test_a_passing_run_records_its_collection_and_outcome(
    tmp_path: pth.Path,
) -> None:
    """Record what a green session collected and what it ran."""
    suite = _write(tmp_path, "test_one.py", _PASSING_SUITE)
    evidence_dir = tmp_path / "evidence"
    completed = _run(suite, evidence_dir)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    record = _only_record(evidence_dir)
    assert record["schema_version"] == SCHEMA_VERSION, (
        "Record what a green session collected and what it ran"
    )
    assert record["exit_status"] == 0, (
        "Record what a green session collected and what it ran"
    )
    assert record["worker_failures"] == 0, (
        "Record what a green session collected and what it ran"
    )
    assert record["collected"] == ["test_one.py::test_passes"], (
        f"collected node IDs must name the test that ran, got {record['collected']}"
    )
    assert record["outcomes"] == {"test_one.py::test_passes": "passed"}, (
        "Record what a green session collected and what it ran"
    )
    assert record["collection_seconds"] is not None, (
        "Record what a green session collected and what it ran"
    )
    wall_seconds = record["wall_seconds"]
    assert isinstance(wall_seconds, int | float), (
        f"wall_seconds must be a number, got {wall_seconds!r}"
    )
    assert wall_seconds > 0.0, "a session cannot take no time"


def test_every_outcome_kind_is_distinguished(tmp_path: pth.Path) -> None:
    """Record pass, fail, skip, xfail and error as five different things.

    A record that collapsed them would report a run of five tests while hiding
    that four of them never exercised their subject.
    """
    suite = _write(tmp_path, "test_mixed.py", _MIXED_SUITE)
    evidence_dir = tmp_path / "evidence"
    completed = _run(suite, evidence_dir)

    assert completed.returncode == 1, (
        f"a suite with failures must exit nonzero, got {completed.returncode}"
    )
    record = _only_record(evidence_dir)
    outcomes = typ.cast("dict[str, str]", record["outcomes"])
    assert outcomes["test_mixed.py::test_passes"] == "passed", (
        "Record pass, fail, skip, xfail and error as five different things"
    )
    assert outcomes["test_mixed.py::test_fails"] == "failed", (
        "Record pass, fail, skip, xfail and error as five different things"
    )
    assert outcomes["test_mixed.py::test_skips"] == "skipped", (
        "Record pass, fail, skip, xfail and error as five different things"
    )
    assert outcomes["test_mixed.py::test_xfails"] == "skipped", (
        "an xfail is reported as a skip by pytest"
    )
    assert outcomes["test_mixed.py::test_errors"] == "failed", (
        "Record pass, fail, skip, xfail and error as five different things"
    )
    skip_reasons = typ.cast("dict[str, str]", record["skip_reasons"])
    assert "deliberate skip" in skip_reasons["test_mixed.py::test_skips"], (
        "Record pass, fail, skip, xfail and error as five different things"
    )
    xfail_reasons = typ.cast("dict[str, str]", record["xfail_reasons"])
    assert "deliberate xfail" in xfail_reasons["test_mixed.py::test_xfails"], (
        "Record pass, fail, skip, xfail and error as five different things"
    )
    assert record["exit_status"] == 1, (
        "Record pass, fail, skip, xfail and error as five different things"
    )


def test_a_collection_error_is_recorded_without_hiding_the_status(
    tmp_path: pth.Path,
) -> None:
    """Record a module that cannot even be imported, and keep the failure.

    A collection error produces no test outcomes at all, so a record that
    counted only outcomes would describe this run as an empty success.
    """
    suite = _write(
        tmp_path,
        "test_broken.py",
        (
            '"""A module that cannot be imported."""\n\n'
            "import a_module_that_does_not_exist\n"
        ),
    )
    evidence_dir = tmp_path / "evidence"
    completed = _run(suite, evidence_dir)

    assert completed.returncode in {2, 4}, (
        f"a collection error must exit nonzero, got {completed.returncode}"
    )
    records = sorted(evidence_dir.glob("pytest-evidence-*.json"))
    assert len(records) == 1, f"a collection error is still a session: {records}"
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["collected"] == [], "nothing can be collected from a broken module"
    assert record["exit_status"] == completed.returncode, (
        "the recorded status must be the status pytest exited with"
    )


def test_an_unwritable_directory_leaves_the_status_unchanged(
    tmp_path: pth.Path,
) -> None:
    """Never let a failed evidence write change a passing run's status."""
    suite = _write(tmp_path, "test_one.py", _PASSING_SUITE)
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory\n", encoding="utf-8")
    completed = _run(suite, blocked / "evidence")

    assert completed.returncode == 0, (
        "a failed evidence write must not fail a green suite: "
        + completed.stdout
        + completed.stderr
    )
    assert "could not write" in completed.stdout, (
        "the refusal must be reported once rather than swallowed silently"
    )


def test_evidence_directory_treats_a_blank_value_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse to write into the working directory for an interpolated empty value."""
    monkeypatch.setenv(EVIDENCE_ENV, "   ")
    assert evidence_directory() is None, (
        "Refuse to write into the working directory for an interpolated empty value"
    )


def test_record_path_names_the_invocation(tmp_path: pth.Path) -> None:
    """Name the artefact after the invocation it describes."""
    path = record_path(tmp_path, "abc123")
    assert path.name == "pytest-evidence-abc123.json", (
        "Name the artefact after the invocation it describes"
    )
    assert path.parent == tmp_path, (
        "Name the artefact after the invocation it describes"
    )


def test_write_record_reports_a_failure_without_raising(
    tmp_path: pth.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Return ``None`` and one message when the destination cannot be written."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory\n", encoding="utf-8")
    written = write_record(blocked / "evidence", {"invocation_id": "abc"})

    assert written is None, "a failed write must not report a path"
    assert "could not write" in capsys.readouterr().out, (
        "Return ``None`` and one message when the destination cannot be written"
    )


def test_write_record_replaces_atomically(tmp_path: pth.Path) -> None:
    """Leave no temporary file behind once the record is written."""
    destination = write_record(tmp_path, {"invocation_id": "abc", "value": 1})

    assert destination is not None, (
        "Leave no temporary file behind once the record is written"
    )
    assert destination.exists(), (
        "Leave no temporary file behind once the record is written"
    )
    assert not list(tmp_path.glob(".*.tmp")), (
        "the temporary file must be renamed away, not left beside the record"
    )
    payload = typ.cast(
        "dict[str, object]", json.loads(destination.read_text(encoding="utf-8"))
    )
    assert payload["value"] == 1, (
        "Leave no temporary file behind once the record is written"
    )


def test_register_is_idempotent_and_only_on_request(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Register once, and only when the environment asks.

    A second registration would double every recorded duration, so the guard is
    asserted rather than assumed.
    """
    from _pytest.config import get_config

    config = get_config()
    monkeypatch.delenv(EVIDENCE_ENV, raising=False)
    assert register(config) is None, "no variable must mean no plugin"

    monkeypatch.setenv(EVIDENCE_ENV, str(tmp_path))
    first = register(config)
    second = register(config)
    assert isinstance(first, SessionEvidence), (
        "Register once, and only when the environment asks"
    )
    assert second is first, "a second call must not create a second plugin"
    config.pluginmanager.unregister(plugin=first, name=PLUGIN_NAME)
