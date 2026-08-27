"""Unit tests for the ``ConcurrentConfig`` and ``ConcurrentResult`` contracts.

These exercise the dataclasses directly, without running commands: argument
validation, the ``ok``/``first_failure`` properties, and the failure-index and
submission-index invariants enforced during construction.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import (
    CommandResult,
    Program,
)
from cuprum.concurrent import (
    ConcurrentConfig,
    ConcurrentResult,
)


class TestConcurrentConfig:
    """Validate concurrent execution configuration."""

    @staticmethod
    @pytest.mark.parametrize("concurrency", [0, -1], ids=["zero", "negative"])
    def test_concurrency_below_one_raises_value_error(concurrency: int) -> None:
        """Concurrency below 1 raises ValueError."""
        with pytest.raises(ValueError, match="concurrency must be >= 1"):
            ConcurrentConfig(concurrency=concurrency)

    @staticmethod
    @pytest.mark.parametrize(
        ("concurrency", "type_name"),
        [
            # ``True`` is an ``int`` subclass, so the exact-type check must reject it.
            pytest.param(True, "bool", id="bool"),
            pytest.param(typ.cast("int", 1.5), "float", id="float"),
        ],
    )
    def test_non_int_concurrency_raises_type_error(
        concurrency: int,
        type_name: str,
    ) -> None:
        """A non-integer concurrency raises TypeError naming the offending type."""
        with pytest.raises(
            TypeError, match=f"concurrency must be an int, got {type_name}"
        ):
            ConcurrentConfig(concurrency=concurrency)


class TestConcurrentResult:
    """Validate concurrent execution result invariants."""

    @staticmethod
    def test_concurrent_result_ok_property() -> None:
        """ConcurrentResult.ok returns True only when all commands succeed."""
        # All success
        result_ok = ConcurrentResult(
            results=(
                CommandResult(Program("echo"), (), 0, 1, "out", ""),
                CommandResult(Program("echo"), (), 0, 2, "out", ""),
            ),
            failures=(),
        )
        assert result_ok.ok is True, "a result with no failures must report ok"

        # One failure
        result_fail = ConcurrentResult(
            results=(
                CommandResult(Program("echo"), (), 0, 1, "out", ""),
                CommandResult(Program("echo"), (), 1, 2, "out", ""),
            ),
            failures=(1,),
        )
        assert result_fail.ok is False, "a result with any failure must not be ok"

    @staticmethod
    def test_concurrent_result_first_failure_property() -> None:
        """ConcurrentResult.first_failure returns the first failed result."""
        result1 = CommandResult(Program("echo"), (), 0, 1, "out", "")
        result2 = CommandResult(Program("echo"), (), 1, 2, "out", "")
        result3 = CommandResult(Program("echo"), (), 2, 3, "out", "")

        concurrent_result = ConcurrentResult(
            results=(result1, result2, result3),
            failures=(1, 2),
        )

        assert concurrent_result.first_failure is result2, (
            "first_failure must be the earliest failed result"
        )

        # No failures
        ok_result = ConcurrentResult(results=(result1,), failures=())
        assert ok_result.first_failure is None, (
            "first_failure must be None when nothing failed"
        )

    @staticmethod
    def test_collect_all_submission_indices_are_identity() -> None:
        """In collect-all mode submission indices match result positions."""
        results = (
            CommandResult(Program("echo"), (), 0, 1, "out", ""),
            CommandResult(Program("echo"), (), 1, 2, "out", ""),
            CommandResult(Program("echo"), (), 0, 3, "out", ""),
        )
        concurrent_result = ConcurrentResult(results=results, failures=(1,))

        assert concurrent_result.submission_indices == (0, 1, 2), (
            "collect-all mode must backfill the identity submission mapping"
        )
        # With identity submission indices the two failure views coincide.
        assert (
            concurrent_result.failure_submission_indices == concurrent_result.failures
        ), "identity submission indices make both failure views coincide"

    @staticmethod
    def test_fail_fast_failure_maps_to_original_submission() -> None:
        """A compacted fail-fast failure maps back to its submission position."""
        # The first command (submission index 0) was cancelled, so only the second
        # (submission index 1), which failed, is present in ``results``.
        failed = CommandResult(Program("echo"), (), 99, 2, None, None)
        concurrent_result = ConcurrentResult(
            results=(failed,),
            failures=(0,),
            submission_indices=(1,),
        )

        # The position-based view says "result 0"; the submission-stable view
        # correctly identifies the originally-submitted command 1.
        assert concurrent_result.failures == (0,), (
            "failures index into the compacted results tuple"
        )
        assert concurrent_result.failure_submission_indices == (1,), (
            "the submission-stable view must report the originally submitted index"
        )

    @staticmethod
    def test_mismatched_submission_indices_length_is_rejected() -> None:
        """A supplied submission_indices must match the results length."""
        results = (
            CommandResult(Program("echo"), (), 0, 1, "out", ""),
            CommandResult(Program("echo"), (), 1, 2, "out", ""),
        )
        # Two results but only one submission index: fail fast during
        # construction rather than defer to an IndexError from
        # failure_submission_indices.
        with pytest.raises(ValueError, match="submission_indices length"):
            ConcurrentResult(results=results, failures=(1,), submission_indices=(0,))

    @staticmethod
    def test_explicit_empty_submission_indices_with_results_is_rejected() -> None:
        """An explicit empty submission_indices differs from omitting it."""
        results = (CommandResult(Program("echo"), (), 0, 1, "out", ""),)
        # Omitting submission_indices (None) backfills the identity sequence...
        assert ConcurrentResult(results=results, failures=()).submission_indices == (
            0,
        ), "omitting submission_indices backfills the identity mapping"
        # ...but an explicit empty tuple is a supplied length-0 sequence, so it is
        # rejected against the single result rather than silently backfilled.
        with pytest.raises(ValueError, match="submission_indices length"):
            ConcurrentResult(results=results, failures=(), submission_indices=())

    @staticmethod
    @pytest.mark.parametrize(
        ("failure", "type_name"),
        [
            # ``True`` equals ``1`` and would otherwise pass the range check, so the
            # exact-type guard must reject it with TypeError.
            pytest.param(True, "bool", id="bool"),
            pytest.param(typ.cast("int", 0.0), "float", id="float"),
        ],
    )
    def test_non_int_failure_index_is_rejected(failure: int, type_name: str) -> None:
        """A non-integer failure index is rejected before range/ordering checks."""
        results = (CommandResult(Program("echo"), (), 0, 1, "out", ""),)
        with pytest.raises(
            TypeError, match=f"failures index must be an int, got {type_name}"
        ):
            ConcurrentResult(results=results, failures=(failure,))

    @staticmethod
    def test_omitted_submission_indices_backfill_empty_results() -> None:
        """Omitting submission_indices for empty results backfills an empty tuple."""
        assert ConcurrentResult(results=(), failures=()).submission_indices == (), (
            "omitted submission_indices must backfill the identity tuple, which is "
            "empty when there are no results"
        )

    @staticmethod
    def test_out_of_range_failure_index_is_rejected() -> None:
        """A failure index beyond the results range raises ValueError."""
        results = (CommandResult(Program("echo"), (), 0, 1, "out", ""),)
        with pytest.raises(ValueError, match="is out of range for 1 results"):
            ConcurrentResult(results=results, failures=(5,))

    @staticmethod
    @pytest.mark.parametrize(
        ("field", "indices"),
        [
            pytest.param("failures", (0, 0), id="failure-duplicate"),
            pytest.param("failures", (1, 0), id="failure-descending"),
            pytest.param("submission_indices", (0, 0), id="submission-duplicate"),
            pytest.param("submission_indices", (1, 0), id="submission-descending"),
        ],
    )
    def test_non_ascending_indices_are_rejected(
        field: str,
        indices: tuple[int, ...],
    ) -> None:
        """Failure and submission indices must be ascending and unique."""
        results = (
            CommandResult(Program("echo"), (), 0, 1, "out", ""),
            CommandResult(Program("echo"), (), 1, 2, "out", ""),
        )

        def construct_result() -> ConcurrentResult:
            """Construct a result with the selected index field."""
            if field == "failures":
                return ConcurrentResult(results=results, failures=indices)
            return ConcurrentResult(results=results, submission_indices=indices)

        with pytest.raises(ValueError, match="strictly ascending and unique"):
            construct_result()

    @staticmethod
    def test_submission_index_beyond_results_length_is_allowed() -> None:
        """Fail-fast compaction leaves survivors whose submission index is larger."""
        results = (CommandResult(Program("echo"), (), 0, 1, "out", ""),)
        # The first two commands were cancelled, so the sole survivor keeps its
        # original submission position even though ``results`` has length 1.
        concurrent_result = ConcurrentResult(results=results, submission_indices=(2,))

        assert concurrent_result.submission_indices == (2,), (
            "submission indices are original positions, not bounded by result count"
        )

    @staticmethod
    @pytest.mark.parametrize(
        ("submission_indices", "expected", "match"),
        [
            pytest.param(
                (typ.cast("int", 0.0),),
                TypeError,
                "submission_indices must be ints",
                id="float",
            ),
            pytest.param(
                (True,), TypeError, "submission_indices must be ints", id="bool"
            ),
            pytest.param((-1,), ValueError, "must be non-negative", id="negative"),
        ],
    )
    def test_invalid_submission_index_values_are_rejected(
        submission_indices: tuple[int, ...],
        expected: type[Exception],
        match: str,
    ) -> None:
        """Submission indices must be non-negative, exact ints."""
        results = (CommandResult(Program("echo"), (), 0, 1, "out", ""),)
        with pytest.raises(expected, match=match):
            ConcurrentResult(results=results, submission_indices=submission_indices)
