"""Tests for the 5.2.1 construction-share classifier.

The classifier decides whether hoisting invariant execution metadata brought
event construction under the plan's 10% share of the per-line consume subtree.
Its arithmetic is small enough to check exactly, and its interesting failure
modes are all about *identification*: a generated ``__init__`` renders as
``__init__ (<string>:N)`` because :func:`dataclasses.dataclass` builds it with
:func:`exec`, so every generated constructor in a capture looks alike. These
tests pin the caller-based disambiguation that makes that tractable, and pin
the exit statuses so an inconclusive run can never be read as a passing one.

Synthetic captures are built through the same parser the real path uses, so a
test cannot pass by constructing internally inconsistent input.
"""

from __future__ import annotations

import json
import typing as typ

import pytest

from benchmarks import summarize_line_event_profile as classifier

if typ.TYPE_CHECKING:
    import pathlib as pth

# The consume symbol whose presence forms the gate's denominator.
CONSUME = "_consume_stream_with_lines (cuprum/_stream_line_consumer.py:69)"
# A generated constructor: indistinguishable across every dataclass.
GENERATED_INIT = "__init__ (<string>:3)"
# The line-callback frame that reaches the constructors.
EMIT_LINE = "emit_line (cuprum/_line_callbacks.py:104)"
# The generic observation emit, reached by non-line phases.
OBSERVATION_EMIT = "emit (cuprum/_pipeline_types.py:120)"
# An unrelated caller that must never resolve a generated frame.
UNRELATED = "build (cuprum/context.py:41)"


def _write(tmp_path: pth.Path, name: str, text: str) -> pth.Path:
    """Write ``text`` to ``name`` under ``tmp_path`` and return the path."""
    path = tmp_path / name
    path.write_text(text)
    return path


def _rules_file(tmp_path: pth.Path) -> pth.Path:
    """Write a minimal valid rules file exercising caller disambiguation."""
    payload = {
        "consume_frames": [
            {
                "function": "_consume_stream_with_lines",
                "location": "cuprum/_stream_line_consumer.py",
            },
        ],
        "construction_rules": [
            {
                "name": "ExecEvent via observation emit",
                "frame": {"function": "__init__", "location": "<string>"},
                "callers": [
                    {"function": "emit", "location": "cuprum/_pipeline_types.py"},
                ],
            },
            {
                "name": "per-line payload",
                "frame": {"function": "__init__", "location": "<string>"},
                "callers": [
                    {
                        "function": "emit_line",
                        "location": "cuprum/_line_callbacks.py",
                    },
                ],
            },
        ],
    }
    return _write(tmp_path, "rules.json", json.dumps(payload))


def _run(
    tmp_path: pth.Path,
    capture_text: str,
    *,
    rules_path: pth.Path | None = None,
) -> tuple[int, dict[str, object]]:
    """Run the classifier over ``capture_text`` and return its exit and result."""
    folded = _write(tmp_path, "stacks.folded", capture_text)
    output = tmp_path / "result.json"
    status = classifier.main(
        [
            str(folded),
            "--rules",
            str(_rules_file(tmp_path) if rules_path is None else rules_path),
            "--output",
            str(output),
        ],
    )
    return status, typ.cast("dict[str, object]", json.loads(output.read_text()))


def _line(frames: str, samples: int) -> str:
    """Render one folded-stack line."""
    return f"{frames} {samples}\n"


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


class TestFrameParsing:
    """Rendered frames parse into their parts, including awkward shapes."""

    @pytest.mark.parametrize(
        ("text", "function", "location", "line"),
        [
            ("emit (a.py:12)", "emit", "a.py", "12"),
            (
                "_load (<frozen importlib._bootstrap>:935)",
                "_load",
                "<frozen importlib._bootstrap>",
                "935",
            ),
            ("<module> (b.py:1)", "<module>", "b.py", "1"),
            ("__init__ (<string>:3)", "__init__", "<string>", "3"),
            ("bare_function", "bare_function", "", ""),
            ("with (parens)", "with (parens)", "", ""),
            ("<module> (dataclasses.py:3)", "<module>", "dataclasses.py", "3"),
        ],
    )
    def test_frames_parse(
        self, text: str, function: str, location: str, line: str
    ) -> None:
        """Each rendered form yields the expected parts."""
        frame = classifier.parse_frame(text)

        assert (frame.function, frame.location, frame.line) == (
            function,
            location,
            line,
        )

    def test_pattern_leaves_unset_fields_wild(self) -> None:
        """A pattern that omits ``line`` matches any line."""
        pattern = classifier.FramePattern(function="__init__", location="<string>")
        frame = classifier.parse_frame("__init__ (<string>:999)")

        assert pattern.matches(frame)

    def test_pattern_rejects_a_different_location(self) -> None:
        """A pattern with a location does not match another file's frame."""
        pattern = classifier.FramePattern(function="__init__", location="<string>")
        assert not pattern.matches(classifier.parse_frame("__init__ (other.py:3)"))


class TestShippedRulesFile:
    """The committed rules file loads and describes the real call paths."""

    def test_shipped_rules_load_and_name_the_consume_symbol(self) -> None:
        """The repository's own rules file is valid and correctly scoped."""
        import pathlib as pth

        path = (
            pth.Path(__file__).resolve().parents[2]
            / "docs"
            / "profiling"
            / "5-2-1-line-event-emission"
            / "classifier-rules.json"
        )
        rules = classifier.load_rules(path)

        consume_functions = {pattern.function for pattern in rules.consume_frames}
        assert "_consume_stream_with_lines" in consume_functions, (
            "the denominator must name the real per-line consume symbol"
        )
        assert rules.construction_rules, "the gate needs construction rules"

    def test_every_shipped_rule_names_a_caller(self) -> None:
        """Caller-scoped matching is what makes a generated frame resolvable."""
        import pathlib as pth

        path = (
            pth.Path(__file__).resolve().parents[2]
            / "docs"
            / "profiling"
            / "5-2-1-line-event-emission"
            / "classifier-rules.json"
        )
        rules = classifier.load_rules(path)

        for rule in rules.construction_rules:
            assert rule.callers, f"rule {rule.name!r} has no caller patterns"
            assert rule.frame.function is not None, (
                f"rule {rule.name!r} must name the frame it classifies"
            )


class TestModuleSplit:
    """The gate's single entry point stays complete after the split.

    The parsed shapes and their parsers live in the model module, and this one
    re-exports them so callers and tests import a single name. A handwritten
    ``__all__`` drifts silently, so every exported name is checked to resolve
    and to be the model's own object rather than a lookalike.
    """

    def test_every_exported_name_resolves_to_the_models_object(self) -> None:
        """Each re-export is the model's own attribute, not a redefinition."""
        import benchmarks.summarize_line_event_profile as gate
        from benchmarks import _line_event_profile_model as model

        for name in gate.__all__:
            assert hasattr(gate, name), f"{name!r} is exported but missing"
            exported = getattr(gate, name)
            assert not hasattr(model, name) or exported is getattr(model, name), (
                f"{name!r} was redefined in the gate instead of re-exported"
            )

    def test_the_model_owns_the_parsers(self) -> None:
        """Parsing lives in the model, so the gate cannot diverge from it."""
        from benchmarks import _line_event_profile_model as model

        assert classifier.parse_frame is model.parse_frame
        assert classifier.parse_capture is model.parse_capture
        assert classifier.load_rules is model.load_rules
        assert classifier._ProfileInputError is model._ProfileInputError
