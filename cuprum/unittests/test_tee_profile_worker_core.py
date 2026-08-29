"""Core worker-execution tests for ``benchmarks.tee_profile_worker``."""

from __future__ import annotations

import typing as typ

import pytest

from benchmarks.tee_profile_worker import TeeProfileWorkerConfig, run_tee_profile_worker
from cuprum.unittests._tee_profile_backend_support import assert_worker_result_ok
from cuprum.unittests.conftest import _VOLATILE_KEYS, redact

if typ.TYPE_CHECKING:
    import pathlib as pth

    from syrupy.assertion import SnapshotAssertion


@pytest.mark.parametrize("with_line_callbacks", [False, True])
def test_worker_exercises_parent_side_consume_path(
    tmp_path: pth.Path,
    with_line_callbacks: bool,
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

        assert_worker_result_ok(result)
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

    assert_worker_result_ok(
        result,
        expected_length=len(fixture.read_text()) * 3,
        expected_lines=3,
    )

def test_worker_uses_configured_read_size(tmp_path: pth.Path) -> None:
    """Worker results report the configured private stream read size."""
    fixture = tmp_path / "fixture_read_size.b64"
    fixture.write_text("YWJjZGVm\n")

    result = run_tee_profile_worker(
        TeeProfileWorkerConfig(
            fixture_path=fixture,
            stages=1,
            mode="tee",
            sink_kind="devnull",
            with_line_callbacks=False,
            backend="python",
            repeat_count=1,
            read_size=17,
        ),
    )

    assert result["read_size"] == 17, (
        f"expected active 17-byte worker read size, got {result}"
    )
