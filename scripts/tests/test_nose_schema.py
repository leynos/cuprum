"""Schema validation of the nose report envelope.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``
(``scripts/tests/test_nose_detector.py``), the merged revision of PR #276,
under the ISC terms in ``LICENSE``.

The schema module owns two guarantees: a family entry must be well formed, and
the report's summary must describe the families array it accompanies. The
first is covered in ``test_nose_detector.py`` alongside the detector that
consumes it; the second is covered here, because a summary that understates its
own array is a valid-looking report whose findings list is silently incomplete.
"""

from __future__ import annotations

import copy
import typing as typ

import pytest

from scripts.tests.duplication_gate_test_support import (
    STUB_REPORT,
    detector,
)


class TestUntruncatedSurface:
    """The summary must describe the families array it accompanies."""

    @pytest.mark.parametrize(
        ("summary", "diagnostic"),
        [
            pytest.param(
                {"families": 63, "shown": 30},
                r"reported 63 families but emitted only 30.*tool\.nose\.top = 0",
                id="ranked-bound-hid-the-tail",
            ),
            pytest.param(
                {"families": 4, "shown": 4},
                r"families array holds 1.*no count in it can be trusted",
                id="summary-overstates-the-array",
            ),
            pytest.param(
                {"families": 0, "shown": 1},
                r"families array holds 1",
                id="summary-understates-the-array",
            ),
            pytest.param(
                {"families": 1},
                r"summary\.shown must be a non-negative integer",
                id="missing-shown-count",
            ),
        ],
    )
    def test_rejects_a_report_the_summary_does_not_describe(
        self,
        summary: dict[str, int],
        diagnostic: str,
    ) -> None:
        """A summary disagreeing with its own array is refused, not adjudicated."""
        report = copy.deepcopy(STUB_REPORT)
        typ.cast("dict[str, typ.Any]", report)["summary"] = summary

        with pytest.raises(detector.GateConfigError, match=diagnostic):
            detector.require_untruncated_surface(report)

    def test_accepts_a_report_that_genuinely_found_nothing(self) -> None:
        """A clean scan's zero-family report is structurally valid.

        The empty-scope guard refuses a zero-family report for a *different*
        reason — the roots selected no files. A summary agreeing with an empty
        array is well-formed, and the two refusals must not be conflated. The
        shape is nose 0.20.0's own: a clean scan emits ``families: []``, so a
        report that omits the array is malformed rather than clean.
        """
        detector.require_untruncated_surface({
            "summary": {"families": 0, "shown": 0},
            "families": [],
        })

    def test_accepts_the_full_stub_report(self) -> None:
        """The untruncated stub report passes the guard it was built for."""
        detector.require_untruncated_surface(copy.deepcopy(STUB_REPORT))

    def test_rejects_a_non_array_families_field(self) -> None:
        """A families field that is not an array cannot be counted or matched."""
        with pytest.raises(detector.GateConfigError, match="must be an array"):
            detector.require_untruncated_surface({
                "summary": {"families": 0, "shown": 0},
                "families": None,
            })
