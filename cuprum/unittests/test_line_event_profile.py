"""Scoring tests for the 5.2.1 construction-share classifier.

The classifier decides whether hoisting invariant execution metadata brought
event construction under the configured share limit — see
``CONSTRUCTION_SHARE_LIMIT_PERCENT`` for the value in force — of the per-line
consume subtree. Its arithmetic is small enough to check exactly, so these
tests pin the boundary either side of the limit, the denominator's scope, and
the once-per-stack rule for the numerator.

Caller attribution is covered in ``test_line_event_profile_callers``, the
inconclusive verdicts in ``test_line_event_profile_verdicts``, and parsing in
``test_line_event_profile_parsing``.
"""

from __future__ import annotations

import typing as typ

import pytest

from benchmarks import summarize_line_event_profile as classifier
from cuprum.unittests.test_line_event_profile_support import (
    CONSUME,
    EMIT_LINE,
    GENERATED_INIT,
    UNRELATED,
    _line,
    _run,
)

if typ.TYPE_CHECKING:
    import pathlib as pth


class TestWeightedFractions:
    """Counts and percentages are exact and use the documented denominator."""

    def test_just_below_the_limit_passes(self, tmp_path: pth.Path) -> None:
        """One sample under the limit passes, at the limit's own boundary."""
        limit = classifier.CONSTRUCTION_SHARE_LIMIT_PERCENT
        construction = int(limit) - 1
        capture = _line(
            f"a;{CONSUME};{EMIT_LINE};{GENERATED_INIT}", construction
        ) + _line(f"a;{CONSUME};{EMIT_LINE}", 100 - construction)
        status, result = _run(tmp_path, capture)

        assert result["consume_samples"] == 100
        assert result["construction_samples"] == construction
        assert result["construction_share_percent"] == pytest.approx(
            float(construction)
        )
        assert result["status"] == "pass"
        assert status == 0

    def test_just_above_the_limit_fails(self, tmp_path: pth.Path) -> None:
        """One sample over the limit fails, at the limit's own boundary."""
        limit = classifier.CONSTRUCTION_SHARE_LIMIT_PERCENT
        construction = int(limit) + 1
        capture = _line(
            f"a;{CONSUME};{EMIT_LINE};{GENERATED_INIT}", construction
        ) + _line(f"a;{CONSUME};{EMIT_LINE}", 100 - construction)
        status, result = _run(tmp_path, capture)

        assert result["construction_share_percent"] == pytest.approx(
            float(construction)
        )
        assert result["status"] == "fail_above_limit"
        assert status == 1, "a valid measurement above the limit exits 1"

    def test_exactly_the_limit_passes(self, tmp_path: pth.Path) -> None:
        """The limit is inclusive: a share equal to it passes."""
        limit = classifier.CONSTRUCTION_SHARE_LIMIT_PERCENT
        construction = int(limit)
        capture = _line(
            f"a;{CONSUME};{EMIT_LINE};{GENERATED_INIT}", construction
        ) + _line(f"a;{CONSUME};{EMIT_LINE}", 100 - construction)
        status, result = _run(tmp_path, capture)

        assert result["construction_share_percent"] == pytest.approx(
            float(construction)
        )
        assert status == 0

    def test_samples_outside_the_consume_subtree_are_excluded(
        self, tmp_path: pth.Path
    ) -> None:
        """Stacks without the consume symbol stay out of both terms."""
        capture = (
            _line(f"a;{CONSUME};{EMIT_LINE};{GENERATED_INIT}", 5)
            + _line(f"a;{CONSUME};{EMIT_LINE}", 5)
            + _line(f"b;{UNRELATED};{GENERATED_INIT}", 990)
        )
        _, result = _run(tmp_path, capture)

        assert result["parent_samples"] == 1000
        assert result["consume_samples"] == 10
        assert result["construction_samples"] == 5
        assert result["construction_share_percent"] == pytest.approx(50.0)
        assert result["construction_share_of_all_parent_percent"] == pytest.approx(0.5)

    def test_nested_constructor_frames_count_once_per_stack(
        self, tmp_path: pth.Path
    ) -> None:
        """Two matching frames in one stack add that stack's weight once."""
        capture = _line(
            f"a;{CONSUME};{EMIT_LINE};{GENERATED_INIT};{EMIT_LINE};{GENERATED_INIT}",
            30,
        ) + _line(f"a;{CONSUME};{EMIT_LINE}", 70)
        _, result = _run(tmp_path, capture)

        assert result["construction_samples"] == 30, (
            "a stack must contribute its weight to the numerator at most once"
        )
        matched = typ.cast("dict[str, int]", result["matched_frames"])
        assert sum(matched.values()) == 60, (
            "per-rule counts are unweighted-by-stack and may exceed the numerator"
        )
