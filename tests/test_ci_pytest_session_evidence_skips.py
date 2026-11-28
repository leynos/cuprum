"""Tests for the skip outcomes the session-evidence plugin records.

A skip raised in a fixture settles a node before its call phase. If the plugin
recorded only call-phase outcomes, a valid run containing a fixture skip would
look like it had no result for that node, and the completeness check that
guards adoption would reject a clean run.
"""

from __future__ import annotations

import typing as typ

from tests.test_ci_pytest_session_evidence import (
    _FIXTURE_SKIP_SUITE,
    _only_record,
    _run,
    _write,
)

if typ.TYPE_CHECKING:
    import pathlib as pth


def test_a_setup_skip_is_recorded_as_an_outcome(tmp_path: pth.Path) -> None:
    """Record a fixture skip as the node's outcome, not as missing evidence.

    A skip raised in a fixture settles the node before its call phase. If the
    plugin recorded only call-phase outcomes, a valid run containing one would
    look like it had no result for that node.
    """
    suite = _write(tmp_path, "test_fixture_skip.py", _FIXTURE_SKIP_SUITE)
    evidence_dir = tmp_path / "evidence"
    completed = _run(suite, evidence_dir)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    record = _only_record(evidence_dir)
    outcomes = typ.cast("dict[str, str]", record["outcomes"])
    assert outcomes.get("test_fixture_skip.py::test_skipped_by_fixture") == "skipped", (
        f"a fixture skip must be recorded as an outcome, got {outcomes}"
    )
