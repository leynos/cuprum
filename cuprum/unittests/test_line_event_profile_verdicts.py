"""Inconclusive-verdict tests for the 5.2.1 construction-share classifier.

The classifier has three outcomes: it passes, it fails above the limit, or it
reports that it could not measure. The third exists so a capture the gate
cannot read is never scored as zero, which would read as a comfortable pass.
These tests pin every route into that verdict — a missing denominator, no
matched frames, an empty capture, an unreadable or malformed rules file — and
pin the exit statuses, so an inconclusive run cannot exit 0.

Whether the arithmetic and the caller attribution *work* is covered in
``test_line_event_profile`` and ``test_line_event_profile_callers``.
"""

from __future__ import annotations

import json
import typing as typ

import pytest

from benchmarks import summarize_line_event_profile as classifier
from cuprum.unittests.test_line_event_profile_support import (
    CONSUME,
    EMIT_LINE,
    GENERATED_INIT,
    UNRELATED,
    _line,
    _rules_file,
    _run,
    _write,
)

if typ.TYPE_CHECKING:
    import pathlib as pth


class TestInconclusiveInputs:
    """Missing or unusable input is reported, never scored as zero."""

    def test_all_constructor_profile_fails_the_gate(self, tmp_path: pth.Path) -> None:
        """A profile that is nothing but construction cannot pass at 100%."""
        capture = _line(f"a;{CONSUME};{EMIT_LINE};{GENERATED_INIT}", 100)
        status, result = _run(tmp_path, capture)

        assert result["construction_share_percent"] == pytest.approx(100.0)
        assert status == 1

    def test_zero_denominator_is_inconclusive(self, tmp_path: pth.Path) -> None:
        """No consume samples means the capture cannot be measured."""
        capture = _line(f"a;{UNRELATED};{GENERATED_INIT}", 10)
        status, result = _run(tmp_path, capture)

        assert result["consume_samples"] == 0
        assert result["status"] == "inconclusive_no_consume_samples"
        assert status == 2

    def test_no_matched_frames_and_no_known_callers_is_inconclusive(
        self, tmp_path: pth.Path
    ) -> None:
        """A subtree the rules cannot recognize at all is inconclusive.

        This is the weakest signal the classifier can give: no construction
        frame matched *and* none of the rules' callers ran, so the capture
        offers no evidence about the gate either way. A capture whose callers
        *did* run is diagnosed more precisely, as drift.
        """
        capture = _line(f"a;{CONSUME};{UNRELATED}", 100)
        status, result = _run(tmp_path, capture)

        assert result["matched_frames"] == {}
        assert result["caller_samples"] == {}
        assert result["status"] == "inconclusive_no_matched_construction_frames"
        assert status == 2

    def test_empty_capture_is_inconclusive(self, tmp_path: pth.Path) -> None:
        """An empty capture exits 2 rather than reporting a 0% share.

        No result document is written, because there is no measurement to
        report — the exit status alone carries the verdict.
        """
        folded = _write(tmp_path, "stacks.folded", "")
        output = tmp_path / "result.json"
        status = classifier.main(
            [
                str(folded),
                "--rules",
                str(_rules_file(tmp_path)),
                "--output",
                str(output),
            ],
        )

        assert status == 2
        assert not output.exists(), "an inconclusive run must not publish a result"

    def test_malformed_lines_are_skipped_not_fatal(self, tmp_path: pth.Path) -> None:
        """Unparsable lines drop out while the rest still measures."""
        capture = (
            "not a stack\n"
            + _line(f"a;{CONSUME};{EMIT_LINE};{GENERATED_INIT}", 1)
            + "trailing-nonsense\n"
            + _line(f"a;{CONSUME};{EMIT_LINE}", 99)
        )
        status, result = _run(tmp_path, capture)

        assert result["consume_samples"] == 100
        assert status == 0

    def test_missing_rules_file_is_inconclusive(
        self, tmp_path: pth.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unreadable rules file exits 2 with a message on stderr."""
        folded = _write(tmp_path, "stacks.folded", _line(f"a;{CONSUME}", 1))
        status = classifier.main(
            [
                str(folded),
                "--rules",
                str(tmp_path / "absent.json"),
                "--output",
                str(tmp_path / "out.json"),
            ],
        )

        assert status == 2
        assert "absent.json" in capsys.readouterr().err

    def test_rules_without_callers_are_rejected(self, tmp_path: pth.Path) -> None:
        """A rule that cannot disambiguate a generated frame is invalid."""
        payload = {
            "consume_frames": [{"function": "_consume_stream_with_lines"}],
            "construction_rules": [
                {"name": "unscoped", "frame": {"function": "__init__"}, "callers": []},
            ],
        }
        rules = _write(tmp_path, "bad.json", json.dumps(payload))
        folded = _write(tmp_path, "stacks.folded", _line(f"a;{CONSUME}", 1))
        status = classifier.main(
            [
                str(folded),
                "--rules",
                str(rules),
                "--output",
                str(tmp_path / "out.json"),
            ],
        )

        assert status == 2

    def test_rules_missing_a_top_level_key_are_rejected(
        self, tmp_path: pth.Path
    ) -> None:
        """A rules file without the gate's denominators is invalid."""
        rules = _write(
            tmp_path,
            "partial.json",
            json.dumps({"construction_rules": []}),
        )
        with pytest.raises(classifier._ProfileInputError, match="consume_frames"):
            classifier.load_rules(rules)
