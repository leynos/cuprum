"""Parsing and configuration tests for the 5.2.1 construction-share gate.

Split from ``test_line_event_profile``, which owns the attribution and scoring
tests. What lives here is the layer beneath scoring: rendered frame text
parsing into its parts, the committed rules file being valid and correctly
scoped, and the module split between the gate and its model staying complete.

These are cheap to separate because they share nothing with the scoring tests
except value literals, and they fail for different reasons: a scoring failure
means the gate measured the wrong thing, while a failure here means it could
not read its input or its rules at all.
"""

from __future__ import annotations

import pytest

from benchmarks import summarize_line_event_profile as classifier
from cuprum.unittests.test_line_event_profile_support import _shipped_rules_path


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
        rules = classifier.load_rules(_shipped_rules_path())

        consume_functions = {pattern.function for pattern in rules.consume_frames}
        assert "_consume_stream_with_lines" in consume_functions, (
            "the denominator must name the real per-line consume symbol"
        )
        assert rules.construction_rules, "the gate needs construction rules"

    def test_every_shipped_rule_names_a_caller(self) -> None:
        """Caller-scoped matching is what makes a generated frame resolvable."""
        rules = classifier.load_rules(_shipped_rules_path())

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
