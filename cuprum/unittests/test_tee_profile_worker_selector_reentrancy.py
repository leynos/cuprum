"""Tests that ``_EnvBackendSelector`` rejects and recovers from same-thread re-entrant activation."""  # noqa: E501

from __future__ import annotations

import contextlib
import logging
import re
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings

from benchmarks import tee_profile_worker
from cuprum.unittests._tee_profile_worker_test_helpers import _backends_strategy

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(outer_backend=_backends_strategy, inner_backend=_backends_strategy)
def test_nested_selector_rejects_generated_backend_pairs(
    outer_backend: tee_profile_worker.BackendName,
    inner_backend: tee_profile_worker.BackendName,
) -> None:
    """Same-thread nested selector entry always raises before mutation."""
    selector = tee_profile_worker._EnvBackendSelector()
    with (
        selector(outer_backend),
        pytest.raises(
            RuntimeError,
            match=tee_profile_worker._REENTRANT_SELECTOR_MESSAGE,
        ),
        selector(inner_backend),
    ):
        pass


def test_nested_selector_raises_runtime_error_and_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nested backend selector entry fails explicitly and cleans up state."""
    expected_message = (
        "_EnvBackendSelector is not re-entrant; nested calls are forbidden"
    )
    expected_warning = "Rejected re-entrant backend selector activation"
    selector = tee_profile_worker._EnvBackendSelector()
    with selector("python"):  # noqa: SIM117
        with caplog.at_level(logging.WARNING):
            with pytest.raises(RuntimeError, match=expected_message):
                with selector("auto"):
                    pass

    assert expected_warning in caplog.text

    with selector("auto"):
        pass


def test_nested_selector_logs_rejection_warning(
    caplog: pytest.LogCaptureFixture,
    snapshot: SnapshotAssertion,
) -> None:
    """Reentrant selector activation emits a structured warning log record.

    Verifies that the rejected-backend name, the active-selector flag, and
    a thread-id field are all present in the logged message. The thread_id
    value is redacted for snapshot determinism.
    """
    selector = tee_profile_worker._EnvBackendSelector()
    with (
        caplog.at_level(
            logging.WARNING,
            logger="benchmarks.tee_profile_worker",
        ),
        selector("python"),
        contextlib.suppress(RuntimeError),
        selector("auto"),
    ):
        pass

    warning_records = [
        record
        for record in caplog.records
        if "re-entrant" in record.getMessage().lower()
    ]
    assert warning_records, "expected a reentrant-rejection warning to be logged"
    msg = warning_records[0].getMessage()
    redacted = re.sub(r"thread_id=\d+", "thread_id=<redacted>", msg)
    assert "backend='auto'" in redacted, (
        f"expected rejected backend name in warning, got: {redacted!r}"
    )
    assert "thread_id=<redacted>" in redacted, (
        f"expected thread_id field in warning, got: {redacted!r}"
    )
    assert "selector_active=True" in redacted, (
        f"expected selector_active field in warning, got: {redacted!r}"
    )
    assert redacted == snapshot, (
        f"Snapshot mismatch: redacted={redacted!r} expected={snapshot!r}"
    )
