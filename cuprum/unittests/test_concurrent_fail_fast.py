"""Unit tests for fail-fast cancellation and collect-all failure mapping.

These cover both execution modes end to end: collect-all running every
command despite failures, fail-fast cancelling pending commands, and the
submission-index mapping that keeps failures traceable when results are
compacted.
"""

from __future__ import annotations

import time

from hypothesis import given
from hypothesis import strategies as st

from cuprum import (
    CommandResult,
    Program,
    ScopeConfig,
    scoped,
    sh,
)
from cuprum.concurrent import (
    ConcurrentConfig,
    ConcurrentResult,
    run_concurrent_sync,
)
from tests.helpers.catalogue import python_catalogue


def _run_sleeper_then_failure(exit_code: int) -> tuple[ConcurrentResult, float]:
    """Run a cancelled sleeper alongside a command failing with *exit_code*.

    Returns the fail-fast result and the elapsed wall-clock seconds. The
    sleeper is submitted first so fail-fast must cancel it, which is what
    compacts ``results`` and makes the position-based and submission-stable
    index views diverge.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)

    sleeper = python("-c", "import time; time.sleep(2); print('slow')")
    failing = python("-c", f"import sys; sys.exit({exit_code})")

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        start = time.perf_counter()
        result = run_concurrent_sync(
            sleeper, failing, config=ConcurrentConfig(fail_fast=True)
        )
        elapsed = time.perf_counter() - start
    return result, elapsed


class TestCollectAllExecution:
    """Verify collect-all result and failure mapping."""

    @staticmethod
    def test_collect_all_mode_continues_after_failure() -> None:
        """Collect-all mode (default) continues executing after a failure."""
        catalogue, python_program = python_catalogue()
        python = sh.make(python_program, catalogue=catalogue)

        cmd1 = python("-c", "print('first')")
        cmd2 = python("-c", "import sys; sys.exit(1)")  # Fails
        cmd3 = python("-c", "print('third')")

        with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
            result = run_concurrent_sync(
                cmd1, cmd2, cmd3, config=ConcurrentConfig(fail_fast=False)
            )

        assert result.ok is False, "collect-all still reports overall failure"
        assert len(result.results) == 3, (
            "collect-all runs every command despite the failure"
        )
        assert result.failures == (1,), (
            "only the second of the three commands exits non-zero"
        )
        assert result.results[0].ok is True, "the first command succeeded"
        assert result.results[1].ok is False, "the second command is the failing one"
        assert result.results[2].ok is True, "the third command still ran to completion"

    @staticmethod
    def test_failures_tuple_contains_correct_indices() -> None:
        """The failures tuple contains indices of failed commands in order."""
        catalogue, python_program = python_catalogue()
        python = sh.make(python_program, catalogue=catalogue)

        cmd0 = python("-c", "print('ok')")
        cmd1 = python("-c", "import sys; sys.exit(1)")  # Fails
        cmd2 = python("-c", "print('ok')")
        cmd3 = python("-c", "import sys; sys.exit(2)")  # Fails

        with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
            result = run_concurrent_sync(cmd0, cmd1, cmd2, cmd3)

        assert result.failures == (1, 3), (
            "the second and fourth commands fail, in ascending result order"
        )
        assert result.first_failure is result.results[1], (
            "first_failure indexes into results, not submissions"
        )

    @staticmethod
    def test_collect_all_submission_indices_are_identity_end_to_end() -> None:
        """A completed collect-all run exposes identity submission indices.

        Without cancellation, ``results`` is not compacted, so every submission
        index equals its result position and the two failure views coincide.
        """
        catalogue, python_program = python_catalogue()
        python = sh.make(python_program, catalogue=catalogue)

        cmd0 = python("-c", "print('ok')")
        cmd1 = python("-c", "import sys; sys.exit(3)")
        cmd2 = python("-c", "print('ok')")

        with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
            result = run_concurrent_sync(cmd0, cmd1, cmd2)

        assert len(result.results) == 3, "collect-all keeps every command's result"
        assert result.submission_indices == (0, 1, 2), (
            "without cancellation the mapping is the identity sequence"
        )
        assert result.failures == (1,), (
            "the second command is the only failure, at result position 1"
        )
        assert result.failure_submission_indices == (1,), (
            "result position and submission index coincide when nothing is cancelled"
        )


class TestFailFastExecution:
    """Verify fail-fast cancellation and submission mapping properties."""

    @staticmethod
    def test_fail_fast_mode_cancels_pending() -> None:
        """Fail-fast mode cancels pending commands after first failure."""
        catalogue, python_program = python_catalogue()
        python = sh.make(python_program, catalogue=catalogue)

        # First command fails immediately, second sleeps
        cmd1 = python("-c", "import sys; sys.exit(42)")
        cmd2 = python("-c", "import time; time.sleep(1); print('should not complete')")

        with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
            start = time.perf_counter()
            result = run_concurrent_sync(
                cmd1, cmd2, config=ConcurrentConfig(fail_fast=True)
            )
            elapsed = time.perf_counter() - start

        assert result.ok is False, "the failing command makes the run unsuccessful"
        # The slow command should be cancelled, so elapsed time should be short
        assert elapsed < 1.0, f"Expected < 1.0s with fail-fast, got {elapsed:.3f}s"

        # Verify shape of results and failures
        # At minimum cmd1 completed (failed); cmd2 may or may not be in results
        assert len(result.results) >= 1, (
            "At least the failed command should be in results"
        )
        assert result.failures == (0,), "First result should be the failure"
        assert result.first_failure is not None, (
            "a failed run must expose its first failure"
        )
        assert result.first_failure is result.results[0], (
            "the failure is the first entry in the compacted results"
        )
        assert result.first_failure.exit_code == 42, (
            "the reported exit code must match the failing command"
        )

    @staticmethod
    @given(
        items=st.lists(
            st.one_of(st.none(), st.integers(min_value=0, max_value=5)),
            max_size=10,
        )
    )
    def test_failure_submission_mapping_is_uniform(items: list[int | None]) -> None:
        """Property: every failure maps back to the correct original submission.

        Each generated item is either ``None`` (a cancelled command) or an exit
        code at its submission index. Across arbitrary mixes the shared collect
        loop must record completed results in order and expose a
        ``failure_submission_indices`` that names exactly the originally-submitted
        commands that exited non-zero.

        Parameters
        ----------
        items : list[int | None]
            Generated per-submission exit codes; ``None`` marks a cancelled command.
        """
        from cuprum.concurrent import _collect_results_and_failures

        indexed = [
            (
                submission_index,
                None
                if code is None
                else CommandResult(Program("x"), (), code, 1, None, None),
            )
            for submission_index, code in enumerate(items)
        ]
        results, submission_indices, failures = _collect_results_and_failures(indexed)

        completed = [code for code in items if code is not None]
        assert len(results) == len(completed), (
            "only completed commands appear in results"
        )
        assert len(submission_indices) == len(results), (
            "the submission mapping stays parallel to results"
        )
        assert all(not results[position].ok for position in failures), (
            "every failure index must point at a failed result"
        )

        concurrent_result = ConcurrentResult(
            results=tuple(results),
            failures=tuple(failures),
            submission_indices=tuple(submission_indices),
        )
        expected = tuple(
            submission_index
            for submission_index, code in enumerate(items)
            if code is not None and code != 0
        )
        assert concurrent_result.failure_submission_indices == expected, (
            "each failure maps back to its original submission index"
        )

    @staticmethod
    def test_fail_fast_first_failure_indexes_correctly_when_earlier_cancelled() -> None:
        """first_failure indexes correctly when earlier commands are cancelled.

        When fail-fast cancels earlier (slower) commands, the failures tuple must
        use remapped indices into the compacted results array, not original
        submission indices. This test ensures first_failure doesn't raise IndexError
        or return the wrong result when a later command fails first.
        """
        result, elapsed = _run_sleeper_then_failure(99)

        # Should complete quickly due to fail-fast
        assert elapsed < 1.0, f"Expected < 1.0s with fail-fast, got {elapsed:.3f}s"
        assert result.ok is False, "the failing command makes the run unsuccessful"

        # Verify shape: only the failed command should be in results because
        # the slow command was cancelled.
        assert len(result.results) >= 1, (
            "At least the failed command should be in results"
        )
        assert len(result.failures) == 1, "Exactly one failure expected"

        # failures tuple must use remapped indices into the compacted results array
        assert result.failures[0] < len(result.results), (
            f"Failure index {result.failures[0]} out of range for results of "
            f"length {len(result.results)}"
        )

        # first_failure must correctly index the failed result
        assert result.first_failure is not None, (
            "a failed run must expose its first failure"
        )
        assert result.first_failure is result.results[result.failures[0]], (
            "first_failure must follow the failures index"
        )
        assert result.first_failure.exit_code == 99, (
            "the reported exit code must match the failing command"
        )

    @staticmethod
    def test_fail_fast_submission_indices_map_to_original_positions() -> None:
        """End-to-end fail-fast run exposes submission-stable failure indices.

        When fail-fast cancels an earlier, slower command, ``results`` is compacted,
        so ``failures`` (positions within ``results``) diverges from the original
        submission positions. ``submission_indices`` and
        ``failure_submission_indices`` must still name the originally-submitted
        command that failed.
        """
        result, elapsed = _run_sleeper_then_failure(7)

        # Fail-fast must cancel the 2s sleeper long before it could finish.
        assert elapsed < 1.0, f"Expected < 1.0s with fail-fast, got {elapsed:.3f}s"
        assert result.ok is False, "the failing command makes the run unsuccessful"

        # Invariants that hold regardless of scheduling.
        assert len(result.submission_indices) == len(result.results), (
            "the submission mapping stays parallel to results"
        )
        assert result.failure_submission_indices == tuple(
            result.submission_indices[position] for position in result.failures
        ), "the submission-stable view must follow the backfilled mapping"

        # The sleeper cannot have completed within the elapsed bound, so results is
        # compacted to the single failed command (originally submitted at index 1).
        assert len(result.results) == 1, (
            "only the completed command remains after cancellation"
        )
        assert result.failures == (0,), (
            "after compaction the sole survivor sits at result position 0"
        )
        assert result.submission_indices == (1,), (
            "the surviving result was submitted second, at index 1"
        )
        # The position-based view says "result 0"; the submission-stable view names
        # the originally-submitted command 1 — the regression this guards.
        assert result.failure_submission_indices == (1,), (
            "the failure maps back to submission index 1, not result position 0"
        )
        assert result.first_failure is not None, (
            "a failed run must expose its first failure"
        )
        assert result.first_failure.exit_code == 7, (
            "the reported exit code must match the failing command"
        )
