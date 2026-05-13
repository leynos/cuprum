"""Tests for the tee hot-path profile worker."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - integration tests exercise fixed CLI commands.
import sys
import threading
import typing as typ

import pytest

from benchmarks import tee_profile_worker
from benchmarks.tee_profile_worker import TeeProfileWorkerConfig, run_tee_profile_worker
from cuprum.unittests.conftest import _VOLATILE_KEYS, redact

if typ.TYPE_CHECKING:
    import pathlib as pth

    from syrupy.assertion import SnapshotAssertion


@pytest.mark.parametrize("with_line_callbacks", [False, True])
def test_worker_exercises_parent_side_consume_path(
    tmp_path: pth.Path,
    with_line_callbacks: bool,  # noqa: FBT001 - pytest parametrises this value.
) -> None:
    """A small fixture can run through echo, capture, and tee modes."""
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJjZGVm\n")
    cb_label = "cb" if with_line_callbacks else "nocb"

    for mode in ("echo", "capture", "tee"):
        result = run_tee_profile_worker(
            TeeProfileWorkerConfig(
                fixture_path=fixture,
                stages=1,
                mode=mode,
                sink_kind="devnull",
                with_line_callbacks=with_line_callbacks,
                backend="python",
                repeat_count=1,
            ),
        )

        assert result["status"] == "ok", f"expected worker status ok, got {result}"
        assert result["exit_code"] == 0, (
            f"expected worker exit code 0 for mode {mode}, got {result}"
        )
        assert result["scenario"] == f"{mode}-devnull-{cb_label}-s1-python", (
            f"expected scenario label for mode {mode}, got {result}"
        )
        captured_output_length = result["captured_output_length"]
        if mode == "echo":
            assert captured_output_length == 0, (
                f"expected no captured output in echo mode, got {result}"
            )
        else:
            assert captured_output_length > 0, (
                f"expected captured output in {mode} mode, got {result}"
            )
        stdout_line_count = result["stdout_line_count"]
        if with_line_callbacks:
            assert stdout_line_count > 0, (
                f"expected stdout line callbacks to run, got {result}"
            )
        else:
            assert stdout_line_count == 0, (
                f"expected no stdout line callbacks without callbacks, got {result}"
            )


def test_run_tee_profile_worker_snapshot(
    tmp_path: pth.Path,
    snapshot: SnapshotAssertion,
) -> None:
    """run_tee_profile_worker output structure matches snapshot."""
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJjZGVm\n")

    result = run_tee_profile_worker(
        TeeProfileWorkerConfig(
            fixture_path=fixture,
            stages=1,
            mode="tee",
            sink_kind="devnull",
            with_line_callbacks=True,
            backend="python",
            repeat_count=1,
        ),
    )

    assert redact(result, _VOLATILE_KEYS) == snapshot


def test_worker_accumulates_repeat_counters(tmp_path: pth.Path) -> None:
    """Worker output counters accumulate over repeated measured runs."""
    fixture = tmp_path / "fixture_repeat.b64"
    fixture.write_text("YWJjZGVm\n")

    result = run_tee_profile_worker(
        TeeProfileWorkerConfig(
            fixture_path=fixture,
            stages=1,
            mode="tee",
            sink_kind="devnull",
            with_line_callbacks=True,
            backend="python",
            repeat_count=3,
        ),
    )

    assert result["status"] == "ok", f"expected worker status ok, got {result}"
    assert result["exit_code"] == 0, f"expected worker exit code 0, got {result}"
    assert result["captured_output_length"] == len(fixture.read_text()) * 3, (
        "expected captured output length to accumulate across repeats, "
        f"got {result['captured_output_length']}"
    )
    assert result["stdout_line_count"] == 3, (
        f"expected line callbacks to accumulate across repeats, got {result}"
    )


def test_concurrent_workers_do_not_race(tmp_path: pth.Path) -> None:
    """Concurrent workers with different backends complete successfully."""
    fixture = tmp_path / "fixture_concurrent.b64"
    fixture.write_text("YWJjZGVm\n")
    alternate_backend = (
        "rust" if tee_profile_worker._backend._check_rust_available() else "auto"
    )
    backends: tuple[tee_profile_worker.BackendName, tee_profile_worker.BackendName] = (
        "python",
        alternate_backend,
    )
    barrier = threading.Barrier(parties=len(backends) + 1)
    errors: list[BaseException] = []
    results: list[tee_profile_worker.TeeProfileWorkerResult] = []
    result_lock = threading.Lock()

    def run_worker(backend: tee_profile_worker.BackendName) -> None:
        try:
            barrier.wait(timeout=5)
            result = run_tee_profile_worker(
                TeeProfileWorkerConfig(
                    fixture_path=fixture,
                    stages=1,
                    mode="tee",
                    sink_kind="devnull",
                    with_line_callbacks=True,
                    backend=backend,
                    repeat_count=1,
                ),
            )
        except BaseException as exc:  # noqa: BLE001 - thread failures must surface.
            with result_lock:
                errors.append(exc)
            return

        with result_lock:
            results.append(result)

    threads = [
        threading.Thread(target=run_worker, args=(backend,), daemon=True)
        for backend in backends
    ]
    for thread in threads:
        thread.start()

    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)

    alive_threads = [thread.name for thread in threads if thread.is_alive()]
    assert not alive_threads, f"expected worker threads to finish, got {alive_threads}"
    assert not errors, f"expected no worker thread errors, got {errors!r}"
    assert len(results) == len(backends), (
        f"expected one result per worker, got {results}"
    )
    assert all(result["status"] == "ok" for result in results), (
        f"expected all worker statuses to be ok, got {results}"
    )
    assert all(result["exit_code"] == 0 for result in results), (
        f"expected all worker exit codes to be 0, got {results}"
    )


def test_nested_selector_raises_runtime_error() -> None:
    """Nested backend selector entry fails explicitly instead of deadlocking."""
    expected_message = (
        "_EnvBackendSelector is not re-entrant; nested calls are forbidden"
    )
    with tee_profile_worker._EnvBackendSelector._activate("python"):  # noqa: SIM117
        with pytest.raises(RuntimeError, match=expected_message):
            with tee_profile_worker._EnvBackendSelector._activate("auto"):
                pass


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
    )

    assert completed.returncode == 0, f"expected exit 0, got {completed.returncode}"
    assert output.exists(), "expected worker-result.json to be written"
    result = json.loads(output.read_text())
    assert result["status"] == "ok", f"expected worker status ok, got {result}"
    assert result["exit_code"] == 0, f"expected worker exit code 0, got {result}"
