"""Documentation contracts for aggregate Python stream-operation telemetry."""

from __future__ import annotations

import pytest

from tests.helpers import extract_markdown_subsection, read_doc, read_users_guide
from tests.helpers.docs import assert_documents

_DEVELOPERS_GUIDE = "docs/developers-guide.md"
_DEVELOPERS_HEADING = "Aggregate Python stream-operation observation"
_USERS_HEADING = "Aggregate Python stream-operation events"


@pytest.fixture(scope="module")
def developers_section() -> str:
    """Load the developer aggregate-observation contract."""
    return extract_markdown_subsection(
        read_doc(_DEVELOPERS_GUIDE),
        heading=_DEVELOPERS_HEADING,
        level=3,
    )


@pytest.fixture(scope="module")
def users_section() -> str:
    """Load the user-facing aggregate-observation contract."""
    return extract_markdown_subsection(
        read_users_guide(),
        heading=_USERS_HEADING,
        level=4,
    )


@pytest.mark.parametrize(
    "term",
    [
        "observe_stream_operation",
        "StreamOperationEvent",
        "stream_drain",
        "pipeline_transfer",
        "bytes_consumed",
        "read_operations",
        "duration_s",
        "post_close_drain_timeout",
        "cuprum_stream_operation_bytes_total",
        "cuprum_stream_operation_read_operations_total",
        "cuprum_stream_operation_duration_seconds",
        "operation",
        "outcome",
    ],
)
def test_developers_guide_documents_aggregate_stream_contract(
    developers_section: str,
    term: str,
) -> None:
    """The internal contract keeps aggregate metrics and labels explicit."""
    assert_documents(developers_section, term)


def test_developers_guide_rejects_per_read_or_chunk_events(
    developers_section: str,
) -> None:
    """The internal contract makes aggregate-only emission explicit."""
    normalized = " ".join(developers_section.split())
    assert_documents(normalized, "No event is emitted per read or per chunk.")


@pytest.mark.parametrize(
    "term",
    [
        "observe_stream_operation",
        "stream_operation_metrics_hook",
        "bytes_consumed",
        "read_operations",
        "No event is emitted per read or per chunk",
        "cuprum_stream_operation_duration_seconds",
        "bounded `operation` and `outcome` labels",
    ],
)
def test_users_guide_documents_opt_in_aggregate_stream_contract(
    users_section: str,
    term: str,
) -> None:
    """The public contract documents opt-in aggregate stream telemetry."""
    assert_documents(users_section, term)
