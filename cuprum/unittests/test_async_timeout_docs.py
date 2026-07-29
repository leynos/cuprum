"""Documentation contract tests for the ASYNC lint policy and timeout telemetry.

Covers the Ruff ``ASYNC`` (flake8-async) policy, the subprocess timeout
observability contract, and the ADR-007 wait-helper addendum. These assert the
required wording so the lint and telemetry contracts are verified mechanically
rather than by prose review alone.
"""

from __future__ import annotations

import pytest

from tests.helpers import extract_markdown_subsection, read_doc, read_users_guide

_DEV_GUIDE = "docs/developers-guide.md"
_ADR_007 = "docs/adr-007-subprocess-execution-module-boundaries.md"

_ASYNC_POLICY_HEADING = "Ruff `ASYNC` (flake8-async) policy"
_TIMEOUT_OBSERVABILITY_HEADING = "Subprocess timeout observability"
_USERS_TIMEOUTS_HEADING = "Timeouts"
_USERS_EVENTS_HEADING = "Structured execution events"


# -- Fixtures -----------------------------------------------------------------


@pytest.fixture(scope="module")
def developers_guide() -> str:
    """Load the developers' guide once per module."""
    return read_doc(_DEV_GUIDE)


@pytest.fixture(scope="module")
def async_policy_section(developers_guide: str) -> str:
    """Extract the Ruff ASYNC policy subsection from the developers' guide."""
    return extract_markdown_subsection(
        developers_guide, heading=_ASYNC_POLICY_HEADING, level=3
    )


@pytest.fixture(scope="module")
def timeout_observability_section(developers_guide: str) -> str:
    """Extract the timeout observability section from the developers' guide."""
    return extract_markdown_subsection(
        developers_guide, heading=_TIMEOUT_OBSERVABILITY_HEADING, level=2
    )


@pytest.fixture(scope="module")
def adr_007() -> str:
    """Load ADR-007 (subprocess execution module boundaries) once per module."""
    return read_doc(_ADR_007)


@pytest.fixture(scope="module")
def users_guide() -> str:
    """Load the users' guide once per module."""
    return read_users_guide()


@pytest.fixture(scope="module")
def users_timeouts_section(users_guide: str) -> str:
    """Extract the users' guide Timeouts section."""
    return extract_markdown_subsection(
        users_guide, heading=_USERS_TIMEOUTS_HEADING, level=3
    )


@pytest.fixture(scope="module")
def users_events_section(users_guide: str) -> str:
    """Extract the users' guide structured execution events section."""
    return extract_markdown_subsection(
        users_guide, heading=_USERS_EVENTS_HEADING, level=3
    )


# -- Ruff ASYNC lint policy ---------------------------------------------------


@pytest.mark.parametrize(
    "term",
    [
        "flake8-async",
        "async-correctness",
        "ASYNC109",
        "ASYNC240",
        "SafeCmd.run",
        "Pipeline.run",
        "# noqa: ASYNC109",
        "subprocess.run(timeout=...)",
        "per-file-ignore",
        "pyproject.toml",
        "PID file",
        "trio.Path",
        "anyio.Path",
    ],
)
def test_developers_guide_documents_async_policy(
    async_policy_section: str, term: str
) -> None:
    """The developers' guide must document the Ruff ASYNC policy and suppressions.

    Covers why the family is selected, the narrowly scoped public-API
    ``# noqa: ASYNC109`` suppressions on ``SafeCmd.run`` / ``Pipeline.run``, and
    the test-scaffolding per-file-ignore for ``ASYNC109`` / ``ASYNC240``.
    """
    assert term in async_policy_section, f"Missing documentation clause: '{term}'"


# -- Timeout observability contract -------------------------------------------


@pytest.mark.parametrize(
    "term",
    [
        "ExecPhase",
        "sh.observe",
        "timeout",
        "teardown_error",
        "timeout_s",
        "timeout_mode",
        "cuprum.timeout",
        "subprocess_timeout_expired",
        "subprocess_teardown_drain_failed",
        "cuprum_operation",
        "cuprum_pid",
        "cuprum_timeout_s",
        "cuprum_timeout_mode",
        "cuprum_error_type",
        "cuprum_teardown_outcome",
        "cuprum_timeouts_total",
        "cuprum_teardown_errors_total",
        '"elapsed_deadline"',
        '"non_positive_immediate"',
        '"drain_error"',
        "non-positive",
        "TimeoutExpired",
        "CancelledError",
    ],
)
def test_developers_guide_documents_timeout_observability(
    timeout_observability_section: str, term: str
) -> None:
    """The developers' guide must document the timeout observability contract.

    Covers the distinguishable ``timeout`` / ``teardown_error`` observe events
    and their fields, the parallel ``cuprum.timeout`` log records, the
    elapsed-versus-immediate timeout mode, and the guarantee that telemetry
    never masks ``TimeoutExpired`` / ``CancelledError``.
    """
    assert term in timeout_observability_section, (
        f"Missing documentation clause: '{term}'"
    )


# -- Users' guide timeout contract --------------------------------------------


@pytest.mark.parametrize(
    "term",
    [
        "run()",
        "run_sync",
        "TimeoutExpired",
        "expires immediately",
        "negative",
    ],
)
def test_users_guide_documents_timeout_contract(
    users_timeouts_section: str, term: str
) -> None:
    """The users' guide must document the public run()/run_sync() timeout contract.

    Covers the ``timeout`` parameter, immediate non-positive expiry, and the
    public ``TimeoutExpired`` mapping.
    """
    assert term in users_timeouts_section, f"Missing documentation clause: '{term}'"


@pytest.mark.parametrize(
    "term",
    [
        "timeout",
        "teardown_error",
        "timeout_mode",
        "elapsed_deadline",
        "non_positive_immediate",
        "TimeoutExpired",
    ],
)
def test_users_guide_documents_timeout_events(
    users_events_section: str, term: str
) -> None:
    """The users' guide structured-events section must document timeout events."""
    assert term in users_events_section, f"Missing documentation clause: '{term}'"


# -- ADR-007 wait-helper addendum ---------------------------------------------


@pytest.mark.parametrize(
    "term",
    [
        "_wait_for_exit_code",
        "_wait_for_exit_code_within_timeout",
        "asyncio.timeout",
        "non-positive",
        "_drain_stream_consumers",
        "exactly once",
        "cuprum.timeout",
        "stream-consumer task",
    ],
)
def test_adr_007_documents_wait_helper_split(adr_007: str, term: str) -> None:
    """ADR-007 must record the wait-helper split, fast path, and no-orphan invariant.

    Covers the ``_wait_for_exit_code`` / ``_wait_for_exit_code_within_timeout``
    split, caller-owned ``asyncio.timeout`` deadlines, the non-positive fast
    path, the drain-exactly-once division of labour with
    ``_drain_stream_consumers``, and the invariant that no pending
    stream-consumer task is left behind.
    """
    assert term in adr_007, f"Missing documentation clause: '{term}'"
