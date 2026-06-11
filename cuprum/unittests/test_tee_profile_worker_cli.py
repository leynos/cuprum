"""CLI and configuration-validation tests for ``benchmarks.tee_profile_worker``."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - integration tests exercise fixed CLI commands.
import sys
import typing as typ

import pytest

from benchmarks import tee_profile_worker
from benchmarks.tee_profile_worker import TeeProfileWorkerConfig

if typ.TYPE_CHECKING:
    import pathlib as pth


def test_worker_cli_reports_config_errors(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Worker CLI returns a process-style error for invalid configuration."""
    missing_fixture = tmp_path / "missing.b64"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tee_profile_worker.py",
            "--fixture",
            str(missing_fixture),
            "--stages",
            "1",
            "--mode",
            "echo",
            "--sink-kind",
            "devnull",
        ],
    )

    assert tee_profile_worker.main() == 2, "expected invalid worker config exit code 2"
    captured = capsys.readouterr()
    expected_error = f"fixture_path must exist and be a file: {missing_fixture}"
    assert expected_error in captured.err, (
        f"expected missing fixture error on stderr, got {captured.err!r}"
    )


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        pytest.param(
            {"stages": 0},
            "stages must be >= 1",
            id="stages-zero",
        ),
        pytest.param(
            {"repeat_count": 0},
            "repeat-count must be >= 1",
            id="repeat-count-zero",
        ),
        pytest.param(
            {"mode": "invalid"},
            "mode must be one of",
            id="invalid-mode",
        ),
        pytest.param(
            {"sink_kind": "invalid"},
            "sink-kind must be one of",
            id="invalid-sink",
        ),
        pytest.param(
            {"backend": "invalid"},
            "backend must be one of",
            id="invalid-backend",
        ),
    ],
)
def test_worker_config_rejects_invalid_fields(
    tmp_path: pth.Path,
    kwargs: dict[str, object],
    fragment: str,
) -> None:
    """TeeProfileWorkerConfig raises ValueError for invalid field values."""
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJj\n")
    base: dict[str, object] = {
        "fixture_path": fixture,
        "stages": 1,
        "mode": "echo",
        "sink_kind": "devnull",
        "with_line_callbacks": False,
        "backend": "python",
        "repeat_count": 1,
    }
    base.update(kwargs)
    config_type = typ.cast("typ.Any", TeeProfileWorkerConfig)
    with pytest.raises(ValueError, match=fragment):
        config_type(**base)


def test_worker_cli_runs_successfully_via_subprocess(tmp_path: pth.Path) -> None:
    """Worker CLI produces a valid JSON result when invoked as a subprocess."""
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJjZGVm\n")
    output = tmp_path / "result.json"
    try:
        completed = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "benchmarks.tee_profile_worker",
                "--fixture",
                str(fixture),
                "--stages",
                "1",
                "--mode",
                "echo",
                "--sink-kind",
                "devnull",
                "--backend",
                "python",
                "--repeat-count",
                "1",
                "--output",
                str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"tee_profile_worker CLI timed out: {exc}")

    assert completed.returncode == 0, (
        f"expected exit 0, got {completed.returncode}: {completed.stderr!r}"
    )
    assert output.exists(), "expected worker-result.json to be written"
    result = json.loads(output.read_text())
    assert result["status"] == "ok", f"expected worker status ok, got {result}"
    assert result["exit_code"] == 0, f"expected worker exit code 0, got {result}"
    assert result["reentrant_rejection_count"] == 0, (
        f"expected no reentrant rejections in a single-selector run, got {result}"
    )
    assert result["lock_wait_seconds"] >= 0.0, (
        f"expected non-negative selector lock-wait seconds, got {result}"
    )
