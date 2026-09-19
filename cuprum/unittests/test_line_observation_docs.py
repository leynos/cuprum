"""Documentation contracts for maintainer-facing line observation guidance."""

from __future__ import annotations

import pytest

from tests.helpers import extract_markdown_subsection, read_doc
from tests.helpers.docs import assert_documents

_DEVELOPERS_GUIDE = "docs/developers-guide.md"
_LINE_OBSERVATION_HEADING = "Line observation"


@pytest.fixture(scope="module")
def line_observation_section() -> str:
    """Load the developers' guide line-observation contract."""
    return extract_markdown_subsection(
        read_doc(_DEVELOPERS_GUIDE),
        heading=_LINE_OBSERVATION_HEADING,
        level=2,
    )


@pytest.mark.parametrize(
    "term",
    [
        "`SafeCmd.lines()`",
        "`RunOutputOptions.on_line`",
        "`LineEvent`",
        "`ExecEvent`",
        "`sh.observe()`",
        "`LineStreamEvent`",
        "`observe_line_stream()`",
        "`exec_id`",
        "PID",
        "bounded queue",
        "backpressure",
        "capture and echo",
        "`cuprum/_line_stream.py`",
        "`cuprum/_line_iteration.py`",
        "`cuprum/_line_callbacks.py`",
        "`cuprum/_subprocess_streams.py`",
        "`cuprum/_execution_tracking.py`",
        "`cuprum/_subprocess_wait.py`",
        "`cuprum/_process_lifecycle.py`",
    ],
)
def test_developers_guide_documents_line_observation_contract(
    line_observation_section: str,
    term: str,
) -> None:
    """The maintainer guide keeps line observation boundaries discoverable."""
    assert_documents(line_observation_section, term)


def test_line_observation_docs_separate_payload_channels(
    line_observation_section: str,
) -> None:
    """The guide distinguishes line callbacks from structured observe events."""
    normalized = " ".join(line_observation_section.split())
    expected = (
        "The line callback path delivers `LineEvent` values. "
        "`sh.observe()` delivers structured `ExecEvent` lifecycle and output records"
    )
    assert_documents(
        normalized,
        expected,
    )


def test_line_observation_docs_define_telemetry_boundary(
    line_observation_section: str,
) -> None:
    """The guide documents the fail-open line-stream telemetry boundary."""
    normalized = " ".join(line_observation_section.split())
    expected = (
        "Line-stream lifecycle telemetry is a separate `LineStreamEvent` channel "
        "exposed through `observe_line_stream()`. Its hook is fail-open"
    )
    assert_documents(normalized, expected)
