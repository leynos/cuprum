"""Caller-attribution tests for the 5.2.1 construction-share classifier.

A generated ``__init__`` renders as ``__init__ (<string>:N)`` because
:func:`dataclasses.dataclass` builds it with :func:`exec`, so every generated
constructor in a capture looks alike and the frame alone identifies nothing.
These tests pin the caller-based disambiguation that makes that tractable: the
nearest preceding caller wins, a caller must precede the frame it reaches, and
a rule whose callers ran but never matched is reported as drift rather than
silently scoring zero.

Frame parsing and the committed rules file are covered in
``test_line_event_profile_parsing``; the exit-status contract in
``test_line_event_profile_verdicts``.
"""

from __future__ import annotations

import json
import typing as typ

import pytest

from cuprum.unittests.test_line_event_profile_support import (
    CONSUME,
    EMIT_LINE,
    GENERATED_INIT,
    _line,
    _run,
    _write,
)

if typ.TYPE_CHECKING:
    import pathlib as pth


class TestCallerDisambiguation:
    """A generated frame resolves only through a rule's caller patterns."""

    def test_rule_whose_caller_runs_but_never_matches_is_inconclusive(
        self, tmp_path: pth.Path
    ) -> None:
        """A rule whose caller carries weight but never matches has drifted."""
        payload = {
            "consume_frames": [{"function": "_consume_stream_with_lines"}],
            "construction_rules": [
                {
                    "name": "drifted",
                    "frame": {"function": "no_such_constructor"},
                    "callers": [{"function": "emit_line"}],
                },
            ],
        }
        rules = _write(tmp_path, "drifted.json", json.dumps(payload))
        capture = _line(f"a;{CONSUME};{EMIT_LINE};{GENERATED_INIT}", 50)
        status, result = _run(tmp_path, capture, rules_path=rules)

        assert result["matched_frames"] == {}
        assert typ.cast("dict[str, int]", result["unresolved_frames"]) == {
            "drifted": 50,
        }, "the drift report should name the rule, not a bare frame"
        assert result["status"] == "inconclusive_drifted_rules"
        assert status == 2, "an inconclusive run must not exit 0"

    def test_drifted_rule_makes_an_otherwise_passing_run_inconclusive(
        self, tmp_path: pth.Path
    ) -> None:
        """Drift outranks a passing arithmetic share."""
        payload = {
            "consume_frames": [{"function": "_consume_stream_with_lines"}],
            "construction_rules": [
                {
                    "name": "live",
                    "frame": {"function": "__init__", "location": "<string>"},
                    "callers": [{"function": "emit_line"}],
                },
                {
                    "name": "drifted",
                    "frame": {"function": "no_such_constructor"},
                    "callers": [{"function": "emit_line"}],
                },
            ],
        }
        rules = _write(tmp_path, "mixed.json", json.dumps(payload))
        capture = _line(f"a;{CONSUME};{EMIT_LINE};{GENERATED_INIT}", 1) + _line(
            f"a;{CONSUME};{EMIT_LINE}", 99
        )
        status, result = _run(tmp_path, capture, rules_path=rules)

        assert result["construction_share_percent"] == pytest.approx(1.0)
        assert result["status"] == "inconclusive_drifted_rules"
        assert status == 2

    def test_rule_absent_from_the_workload_is_not_reported_as_drift(
        self, tmp_path: pth.Path
    ) -> None:
        """A rule whose callers never run is absent, not drifted.

        One workload cannot exercise every construction rule, so an unused
        rule must not make the run inconclusive. Only a rule whose callers
        *did* run carries evidence that something changed.
        """
        capture = _line(f"a;{CONSUME};{EMIT_LINE};{GENERATED_INIT}", 5) + _line(
            f"a;{CONSUME};{EMIT_LINE}", 95
        )
        status, result = _run(tmp_path, capture)

        # rules.json declares an observation-emit rule whose caller never
        # appears in this capture; it must stay silent.
        assert result["unresolved_frames"] == {}
        assert result["status"] == "pass"
        assert status == 0

    def test_replacement_helper_frame_still_resolves(self, tmp_path: pth.Path) -> None:
        """A constructor moved into a new helper frame still counts.

        This is the EP-M2 case: the hoist may introduce a frame between the
        callback and the constructor. The rule names the production callers
        rather than only the immediate one, so the moved frame still resolves.
        """
        payload = {
            "consume_frames": [{"function": "_consume_stream_with_lines"}],
            "construction_rules": [
                {
                    "name": "hoisted event emission",
                    "frame": {"function": "__init__", "location": "<string>"},
                    "callers": [
                        {"function": "emit_line"},
                        {"function": "_emit_line_event"},
                    ],
                },
            ],
        }
        rules = _write(tmp_path, "hoisted.json", json.dumps(payload))
        # The frame directly reaching the constructor is the *new* helper; the
        # original callback sits further up the same stack. Matching on the
        # immediate caller alone would leave this unresolved.
        capture = _line(
            f"a;{CONSUME};emit_line (_line_callbacks.py:104);"
            "_emit_line_event (_line_callbacks.py:2);"
            f"{GENERATED_INIT}",
            4,
        ) + _line(f"a;{CONSUME};emit_line (_line_callbacks.py:104)", 96)
        status, result = _run(tmp_path, capture, rules_path=rules)

        assert result["matched_frames"] == {"hoisted event emission": 4}
        assert result["unresolved_frames"] == {}
        assert result["construction_share_percent"] == pytest.approx(4.0)
        assert status == 0, "a helper-reached constructor still counts as construction"

    def test_caller_after_the_frame_does_not_resolve_it(
        self, tmp_path: pth.Path
    ) -> None:
        """A caller must precede the frame it reaches."""
        capture = _line(f"a;{CONSUME};{GENERATED_INIT};{EMIT_LINE}", 20)
        _, result = _run(tmp_path, capture)

        assert result["matched_frames"] == {}
        assert result["unresolved_frames"]

    def test_nearest_caller_decides_between_overlapping_rules(
        self, tmp_path: pth.Path
    ) -> None:
        """Two rules matching one frame are separated by caller proximity.

        Every generated constructor renders identically, so overlapping rules
        are the normal case, not an edge case. The rule whose caller sits
        closest to the frame is the one that actually invokes it. This test
        deliberately declares the *broader* rule first: resolving by
        declaration order would let it absorb the narrower rule's frames.
        """
        payload = {
            "consume_frames": [{"function": "_consume_stream_with_lines"}],
            "construction_rules": [
                {
                    "name": "outer",
                    "frame": {"function": "__init__", "location": "<string>"},
                    "callers": [{"function": "emit_line"}],
                },
                {
                    "name": "inner",
                    "frame": {"function": "__init__", "location": "<string>"},
                    "callers": [{"function": "_event_details"}],
                },
            ],
        }
        rules = _write(tmp_path, "overlap.json", json.dumps(payload))
        # Both rules' callers are present, but ``_event_details`` is nearer.
        capture = _line(
            f"a;{CONSUME};emit_line (_line_callbacks.py:109);"
            "_event_details (_line_callbacks.py:58);"
            f"{GENERATED_INIT}",
            10,
        )
        _, result = _run(tmp_path, capture, rules_path=rules)

        assert result["matched_frames"] == {"inner": 10}, (
            "the nearest caller must win regardless of rule order"
        )

    def test_a_rule_with_no_present_caller_does_not_absorb_the_frame(
        self, tmp_path: pth.Path
    ) -> None:
        """A frame resolves to a rule only when that rule's caller is present."""
        payload = {
            "consume_frames": [{"function": "_consume_stream_with_lines"}],
            "construction_rules": [
                {
                    "name": "absent caller",
                    "frame": {"function": "__init__", "location": "<string>"},
                    "callers": [{"function": "nowhere_in_this_stack"}],
                },
                {
                    "name": "present caller",
                    "frame": {"function": "__init__", "location": "<string>"},
                    "callers": [{"function": "emit_line"}],
                },
            ],
        }
        rules = _write(tmp_path, "partial.json", json.dumps(payload))
        capture = _line(
            f"a;{CONSUME};emit_line (_line_callbacks.py:109);{GENERATED_INIT}",
            10,
        )
        _, result = _run(tmp_path, capture, rules_path=rules)

        assert result["matched_frames"] == {"present caller": 10}
        assert result["unresolved_frames"] == {}
