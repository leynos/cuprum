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
    CONSUMER_EMIT_LINE,
    EMIT_EVENT,
    EMIT_EXEC_EVENT,
    EMIT_LINE,
    FOREIGN_EMIT_EXEC_EVENT,
    GENERATED_INIT,
    HOOK_BODY,
    OBSERVATION_EMIT,
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


class TestObserveHookBoundary:
    """Construction reached through hook dispatch is not event emission.

    An observe hook runs below the dispatcher, so a hook that builds its own
    dataclass per event presents a generated ``__init__`` whose nearest
    emission caller is still ``emit_line``. Without a structural guard that
    work would be counted toward the numerator as though it were ``ExecEvent``.

    These cases drive every emission entry point -- the hoisted per-line path
    (``emit_line``), the non-line phases (``emit``), and the fail-fast path --
    through the boundary, and check that the same shape counts when the
    construction happens *above* the dispatcher.
    """

    @pytest.mark.parametrize(
        ("label", "emission"),
        [
            ("emit_line", EMIT_LINE),
            ("emit", "emit (cuprum/_pipeline_types.py:120)"),
            ("emit_fail_fast", "emit_fail_fast (cuprum/_pipeline_types.py:140)"),
        ],
    )
    @pytest.mark.parametrize(
        ("dispatcher", "hook_owned"),
        [
            (EMIT_EVENT, True),
            (EMIT_EXEC_EVENT, True),
            ("", False),
        ],
        ids=["via-emit_event", "via-emit_exec_event", "direct"],
    )
    def test_construction_is_counted_only_when_it_precedes_the_dispatcher(
        self,
        tmp_path: pth.Path,
        label: str,
        emission: str,
        dispatcher: str,
        hook_owned: bool,
    ) -> None:
        """Every emission entry point is guarded, and every one still counts.

        The rows pair a direct construction with the same construction placed
        below a dispatcher frame, so the guard has to resolve one and reject
        the other. A guard that simply refused every frame would pass the
        rejection rows and fail the direct ones.

        The rules are written here rather than taken from the shared fixture
        because that fixture keys on ``emit`` and ``emit_line`` alone, and
        ``emit_fail_fast`` is a distinct function name rather than a phase of
        either.
        """
        payload = {
            "consume_frames": [{"function": "_consume_stream_with_lines"}],
            "construction_rules": [
                {
                    "name": "emission",
                    "frame": {"function": "__init__", "location": "<string>"},
                    "callers": [
                        {"function": "emit", "location": "cuprum/_pipeline_types.py"},
                        {
                            "function": "emit_fail_fast",
                            "location": "cuprum/_pipeline_types.py",
                        },
                        {
                            "function": "emit_line",
                            "location": "cuprum/_line_callbacks.py",
                        },
                    ],
                },
            ],
        }
        rules = _write(tmp_path, "emission.json", json.dumps(payload))
        path = f"{CONSUME};{emission}"
        if dispatcher:
            path += f";{dispatcher}"
        capture = _line(f"a;{path};{GENERATED_INIT}", 30)
        _, result = _run(tmp_path, capture, rules_path=rules)

        expected = {} if hook_owned else {"emission": 30}
        assert result["matched_frames"] == expected, (
            f"{label} construction above the dispatcher must count; below it must not"
        )

    def test_hook_owned_construction_is_excluded(self, tmp_path: pth.Path) -> None:
        """A hook that constructs per event does not enter the numerator.

        ``_emit_event`` dispatches to ``_emit_exec_event``, which invokes the
        hooks; the constructor is reached from inside a hook body, so the whole
        path is hook work.

        With nothing left matched, the run reports drift rather than a clean
        zero numerator. That is the conservative reading and it is deliberate:
        a capture in which the emission caller runs but constructs nothing
        through it is indistinguishable from construction having genuinely
        moved behind the dispatcher, which is a change to the production call
        path worth surfacing rather than quietly scoring as "no construction".
        """
        capture = _line(
            f"a;{CONSUME};{EMIT_LINE};{EMIT_EVENT};{EMIT_EXEC_EVENT};"
            f"{HOOK_BODY};{GENERATED_INIT}",
            45,
        )
        status, result = _run(tmp_path, capture)

        assert result["matched_frames"] == {}
        assert result["construction_samples"] == 0
        assert result["construction_share_percent"] == pytest.approx(0.0)
        assert result["status"] == "inconclusive_drifted_rules"
        assert status == 2

    def test_hook_owned_construction_does_not_reach_fail_above_limit(
        self, tmp_path: pth.Path
    ) -> None:
        """Hook work cannot push an otherwise passing run over the limit.

        This is the gate-level consequence of the attribution bug: a heavy
        per-event hook would previously have inflated ``construction_samples``
        until a compliant run reported ``fail_above_limit``.
        """
        payload = {
            "consume_frames": [{"function": "_consume_stream_with_lines"}],
            "construction_rules": [
                {
                    "name": "emission",
                    "frame": {"function": "__init__", "location": "<string>"},
                    "callers": [{"function": "emit_line"}],
                },
            ],
        }
        rules = _write(tmp_path, "mixed.json", json.dumps(payload))
        capture = _line(
            f"a;{CONSUME};{EMIT_LINE};{GENERATED_INIT}",
            10,
        ) + _line(
            f"a;{CONSUME};{EMIT_LINE};{EMIT_EVENT};{EMIT_EXEC_EVENT};{HOOK_BODY};"
            f"{GENERATED_INIT}",
            90,
        )
        status, result = _run(tmp_path, capture, rules_path=rules)

        assert result["consume_samples"] == 100
        assert result["construction_samples"] == 10, (
            "only the direct construction is emission work"
        )
        assert result["construction_share_percent"] == pytest.approx(10.0)
        assert result["status"] == "pass"
        assert status == 0, "hook-owned construction must not fail a passing run"

    def test_emission_nested_below_a_dispatcher_still_counts(
        self, tmp_path: pth.Path
    ) -> None:
        """A hook that emits its own event constructs genuine emission work.

        This is the guard's hardest case, because the stack contains *both* a
        dispatcher frame and a real construction underneath it. The inner
        ``emit_line`` is below the outer dispatch seam and is the nearest
        caller to the constructor, so nothing intervenes between caller and
        frame and the candidate counts. A guard that rejected on the mere
        *presence* of a dispatcher frame anywhere above would lose this work
        and understate the share.
        """
        capture = _line(
            f"a;{CONSUME};{EMIT_LINE};{EMIT_EVENT};{EMIT_EXEC_EVENT};"
            f"{HOOK_BODY};{EMIT_LINE};{GENERATED_INIT}",
            40,
        )
        _, result = _run(tmp_path, capture)

        assert result["matched_frames"] == {"per-line payload": 40}, (
            "the inner emit_line reaches the constructor without crossing a "
            "boundary, so the construction is emission work"
        )
        assert result["unresolved_frames"] == {}

    def test_excluded_hook_sample_remains_in_the_consume_denominator(
        self, tmp_path: pth.Path
    ) -> None:
        """Exclusion is from the numerator only.

        A hook still runs inside the consume subtree, so its samples stay in
        ``D``. Dropping them from the denominator as well would shrink the
        measurement rather than correct the attribution.
        """
        capture = _line(
            f"a;{CONSUME};{EMIT_LINE};{EMIT_EVENT};{EMIT_EXEC_EVENT};"
            f"{HOOK_BODY};{GENERATED_INIT}",
            7,
        )
        _, result = _run(tmp_path, capture)

        assert result["consume_samples"] == 7
        assert result["construction_samples"] == 0

    def test_hook_owned_construction_does_not_present_as_drift(
        self, tmp_path: pth.Path
    ) -> None:
        """Rejecting a match must not be reported as a drifted rule.

        Drift means the production caller ran but no longer constructs what
        the rule names. Here the caller still constructs; the rejected frame
        is a different construction. Reporting drift would turn a correct
        reading into an inconclusive run.
        """
        capture = _line(
            f"a;{CONSUME};{EMIT_LINE};{GENERATED_INIT}",
            10,
        ) + _line(
            f"a;{CONSUME};{EMIT_LINE};{EMIT_EVENT};{EMIT_EXEC_EVENT};{HOOK_BODY};"
            f"{GENERATED_INIT}",
            90,
        )
        status, result = _run(tmp_path, capture)

        assert result["matched_frames"] == {"per-line payload": 10}
        assert result["unresolved_frames"] == {}
        assert result["construction_share_percent"] == pytest.approx(10.0)
        assert result["status"] == "pass"
        assert status == 0

    def test_a_helper_on_the_emission_path_still_counts(
        self, tmp_path: pth.Path
    ) -> None:
        """An emission-side helper above the dispatcher is not hook work.

        The guard keys on the dispatch boundary, not on the presence of any
        intermediate frame, so a constructor reached through a valid
        construction helper before dispatch is unaffected.
        """
        capture = _line(
            f"a;{CONSUME};{EMIT_LINE};"
            "_build_line_event (cuprum/_line_callbacks.py:90);"
            f"{GENERATED_INIT}",
            12,
        )
        _, result = _run(tmp_path, capture)

        assert result["matched_frames"] == {"per-line payload": 12}
        assert result["unresolved_frames"] == {}

    def test_a_boundary_frame_is_matched_on_location_not_name_alone(
        self, tmp_path: pth.Path
    ) -> None:
        """A foreign frame reusing the dispatcher's name is not the boundary.

        This is the only thing the ``(function, location)`` pair buys, and it
        is the reason the pair is required rather than the bare name. A
        vendored or relocated copy of the dispatcher would render as
        ``_emit_exec_event`` in another module; a name-only boundary would
        treat it as the real seam and silently drop every constructor reached
        beneath it, so the share would fall for the wrong reason.
        """
        capture = _line(
            f"a;{CONSUME};{OBSERVATION_EMIT};{FOREIGN_EMIT_EXEC_EVENT};"
            f"{GENERATED_INIT}",
            25,
        )
        _, result = _run(tmp_path, capture)

        assert result["matched_frames"] == {
            "ExecEvent via observation emit": 25,
        }
        assert result["unresolved_frames"] == {}

    def test_the_consumer_helper_is_not_read_as_an_emission_caller(
        self, tmp_path: pth.Path
    ) -> None:
        """``_emit_line`` is a different name from ``emit_line``.

        The stream consumer's per-line helper renders with its leading
        underscore intact, so it neither resolves the constructor rule nor
        satisfies the dispatcher boundary. It is on these stacks as a sibling
        of the emission path, not as part of it.
        """
        capture = _line(
            f"a;{CONSUME};{CONSUMER_EMIT_LINE};{EMIT_LINE};{GENERATED_INIT}",
            25,
        )
        _, result = _run(tmp_path, capture)

        assert result["matched_frames"] == {"per-line payload": 25}
        assert result["unresolved_frames"] == {}

    def test_the_boundary_frame_alone_does_not_block_a_distant_caller(
        self, tmp_path: pth.Path
    ) -> None:
        """Only a boundary *between* caller and frame rejects the candidate.

        A dispatcher frame above the resolving caller belongs to a different
        concern on the same stack and must not disqualify the match.
        """
        capture = _line(
            f"a;{CONSUME};{EMIT_EVENT};{EMIT_EXEC_EVENT};{EMIT_LINE};{GENERATED_INIT}",
            18,
        )
        _, result = _run(tmp_path, capture)

        assert result["matched_frames"] == {"per-line payload": 18}
        assert result["unresolved_frames"] == {}
