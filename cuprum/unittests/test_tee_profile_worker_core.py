"""Core worker-execution tests for ``benchmarks.tee_profile_worker``."""

from __future__ import annotations

import typing as typ

import pytest

from benchmarks.tee_profile_worker import (
    TeeProfileWorkerConfig,
    TeeProfileWorkerResult,
    run_tee_profile_worker,
)
from cuprum.stream_events import StreamOperation, StreamOperationOutcome
from cuprum.unittests._tee_profile_backend_support import assert_worker_result_ok
from cuprum.unittests.conftest import _VOLATILE_KEYS, redact

if typ.TYPE_CHECKING:
    import pathlib as pth

    from syrupy.assertion import SnapshotAssertion


def _assert_positive_stream_telemetry(
    result: TeeProfileWorkerResult,
    expected_operations: set[StreamOperation],
) -> None:
    """Assert observed telemetry uses only the closed vocabulary and totals."""
    telemetry = result["stream_telemetry"]
    groups = telemetry["groups"]
    assert set(groups) == {operation.value for operation in expected_operations}, (
        f"expected telemetry groups {expected_operations}, got {groups}"
    )
    for expected_operation in expected_operations:
        assert groups.get(expected_operation.value), (
            f"expected non-empty {expected_operation.value} telemetry, got {groups}"
        )
    group_payloads = []
    for outcomes in groups.values():
        assert outcomes, f"operation telemetry must contain an outcome, got {groups}"
        assert set(outcomes) <= {outcome.value for outcome in StreamOperationOutcome}, (
            f"telemetry must use only closed outcome labels, got {outcomes}"
        )
        group_payloads.extend(outcomes.values())
        for group in outcomes.values():
            assert group["bytes_consumed"] > 0, (
                f"expected positive consumed bytes, got {group}"
            )
            assert group["read_operations"] > 0, (
                f"expected positive read-operation count, got {group}"
            )
            assert group["operation_count"] > 0, (
                f"expected positive operation count, got {group}"
            )
    totals = telemetry["totals"]
    assert totals["bytes_consumed"] == sum(
        group["bytes_consumed"] for group in group_payloads
    ), f"bytes total must match groups, got {telemetry}"
    assert totals["read_operations"] == sum(
        group["read_operations"] for group in group_payloads
    ), f"read-operation total must match groups, got {telemetry}"
    assert totals["operation_count"] == sum(
        group["operation_count"] for group in group_payloads
    ), f"operation total must match groups, got {telemetry}"
    assert totals["duration_seconds"] == pytest.approx(
        sum(group["duration_seconds"] for group in group_payloads)
    ), f"duration total must match groups, got {telemetry}"


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


class TestWorkerStreamTelemetry:
    """Worker-result aggregate stream telemetry tests."""

    @pytest.mark.parametrize(
        ("stages", "expected_operations"),
        [
            pytest.param(
                1,
                {StreamOperation.DRAIN},
                id="single-stage-drain",
            ),
            pytest.param(
                2,
                {StreamOperation.DRAIN, StreamOperation.PIPELINE_TRANSFER},
                id="multistage-pipeline-transfer",
            ),
        ],
    )
    def test_python_worker_reports_expected_stream_telemetry(
        self,
        tmp_path: pth.Path,
        stages: int,
        expected_operations: set[StreamOperation],
    ) -> None:
        """A Python worker reports the pure-Python operation kinds it executes."""
        fixture = tmp_path / "fixture_stream_telemetry.b64"
        fixture.write_text("YWJjZGVm\n")

        # The Rust backend may not emit ``pipeline_transfer`` because this
        # observer sees only the pure-Python pipeline path.
        result = run_tee_profile_worker(
            TeeProfileWorkerConfig(
                fixture_path=fixture,
                stages=stages,
                mode="tee",
                sink_kind="devnull",
                with_line_callbacks=False,
                backend="python",
                repeat_count=1,
            ),
        )

        _assert_positive_stream_telemetry(result, expected_operations)


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
