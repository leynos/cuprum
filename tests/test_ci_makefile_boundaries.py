"""Drive the Makefile readers with documents the estate never produces.

`tests/test_ci_suite_wiring_contract.py` and
`tests/test_ci_test_selection_contract.py` ask what the Makefile *declares* by
reading this repository's own file, and that is the right way to ask it. It is
the wrong way to test the *reader*: every case it can express is a case the
estate already satisfies, so a malformed document, an unassigned variable, and
a target that is not there are never driven at all. A reader could stop
refusing anything and every contract above it would keep passing.

So the Makefile readers are driven here with injected parser documents:

* `tests/helpers/makeutil.py` — the process boundary and its error translation;
* `tests/helpers/makefile.py` — the narrowing layer over the parsed document;
* `tests/helpers/recipe_read.py` — the recipe text and its shell words.

Each reader's *success* path is exercised too, because a refusal test alone
cannot tell a reader that refuses malformed input from one that refuses
everything. The estate-wide contracts remain the authority on what this
repository declares; these cases are the authority on what the readers do when
what they read is wrong.

The workflow readers — `parse_document`, `narrow_steps`, and the `run_scripts`
sweep — are the sibling module `tests/test_ci_helper_boundaries.py`. They were
split apart when this family crossed the 400-line limit `AGENTS.md` sets and the
lint gate enforces; the boundary between the two is the one the helpers are
already split along, so each module drives the readers answering one kind of
question.
"""

from __future__ import annotations

import json
import subprocess  # ruff: ignore[suspicious-subprocess-import] - test doubles for a fixed argv.
import typing as typ

import pytest

from tests.helpers.makefile import (
    Runner,
    makeutil_document,
    recipe_of,
    recipe_tokens,
    variable_expansion,
)

if typ.TYPE_CHECKING:
    # `Path` appears only in the `tmp_path` parameters below; those annotations
    # are never evaluated, so the import stays out of the runtime namespace.
    from pathlib import Path


def _completed(
    argv: list[str], returncode: int, stdout: str, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build the completed process a fake runner returns."""
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


#: A minimal document carrying one assignment and one rule, so the success path
#: has something to read rather than merely something to not-refuse.
_GOOD_DOCUMENT = json.dumps({
    "variables": [{"name": "X", "raw_value": "a b", "operator": "?="}],
    "rules": [{"targets": ["t"], "recipes": [{"text": "echo hi"}]}],
})


def _runner_returning(stdout: str, returncode: int = 0) -> Runner:
    """Return a fake runner that reports the given output."""

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Report the canned output, echoing the argv it was handed."""
        return _completed(argv, returncode, stdout)

    return run


def _runner_raising(error: BaseException) -> Runner:
    """Return a fake runner that raises instead of returning."""

    def run(_argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Raise the supplied error, standing in for a process that cannot start."""
        raise error

    return run


class TestMakeutilProcessBoundary:
    """The Makefile read must report every failure as its documented error."""

    def test_the_success_path_reads_the_document(self) -> None:
        """A well-formed document is returned, so the refusals are not blanket."""
        document = makeutil_document(runner=_runner_returning(_GOOD_DOCUMENT))
        assert document["variables"], "the parsed document must carry variables"

    def test_a_missing_binary_is_reported_as_a_contract_error(self) -> None:
        """`makeutil` absent from PATH is the documented `AssertionError`.

        `subprocess.run` raises `FileNotFoundError` for a binary it cannot
        start, which is not the error this read API documents — so a caller
        catching the documented type would miss the one failure that says the
        toolchain is broken rather than the Makefile.
        """
        runner = _runner_raising(FileNotFoundError(2, "No such file", "makeutil"))
        with pytest.raises(AssertionError, match=r"not on PATH"):
            makeutil_document(runner=runner)

    def test_a_timeout_is_reported_as_a_contract_error(self) -> None:
        """A wedged parser fails by name rather than escaping as a traceback."""
        runner = _runner_raising(subprocess.TimeoutExpired("makeutil", 60))
        with pytest.raises(AssertionError, match=r"did not parse"):
            makeutil_document(runner=runner)

    def test_a_non_zero_exit_carries_the_parsers_diagnostic(self) -> None:
        """The parser's own stderr reaches the message; exit status is not enough."""
        runner = _runner_returning("", returncode=2)
        with pytest.raises(AssertionError, match=r"exit 2"):
            makeutil_document(runner=runner)

    def test_output_that_is_not_json_is_refused(self) -> None:
        """Non-JSON output cannot be read as a document."""
        with pytest.raises(AssertionError, match=r"did not emit JSON"):
            makeutil_document(runner=_runner_returning("not a document"))

    def test_json_that_is_not_an_object_is_refused(self) -> None:
        """A JSON list is valid JSON and still not a Makefile document."""
        with pytest.raises(AssertionError, match=r"must emit a JSON object"):
            makeutil_document(runner=_runner_returning("[]"))

    def test_the_working_directory_is_the_one_supplied(self, tmp_path: Path) -> None:
        """`root` reaches the parser, rather than being silently re-derived."""
        seen: dict[str, object] = {}

        def spy(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            """Capture the keywords the reader passes, and report success."""
            seen.update(kwargs)
            return _completed(argv, 0, _GOOD_DOCUMENT)

        makeutil_document(root=tmp_path, runner=spy)
        assert seen["cwd"] == tmp_path, (
            f"the parse must run in the directory it was given; got {seen['cwd']!r}"
        )


class TestMakefileNarrowing:
    """The variable reader must refuse what it cannot resolve honestly."""

    def _document(self, variables: list[dict[str, str]]) -> Runner:
        """Return a runner serving a document with the given assignments."""
        return _runner_returning(json.dumps({"variables": variables, "rules": []}))

    def test_an_unassigned_variable_is_refused(self) -> None:
        """Asking for a name the Makefile never assigns fails by name."""
        runner = self._document([{"name": "A", "raw_value": "1", "operator": "="}])
        with pytest.raises(AssertionError, match=r"must assign B"):
            variable_expansion("B", runner=runner)

    def test_a_reference_to_an_undefined_variable_is_refused(self) -> None:
        """A `$(MISSING)` reference cannot resolve to an empty string.

        Substituting nothing would shrink the selector, and a selector that is
        too small makes every coverage assertion pass for the wrong reason.
        """
        runner = self._document([
            {"name": "A", "raw_value": "$(MISSING)", "operator": "="}
        ])
        with pytest.raises(AssertionError, match=r"never assigns it"):
            variable_expansion("A", runner=runner)

    def test_a_reference_naming_a_function_is_refused_as_such(self) -> None:
        """A `make` function call is not an unassigned variable.

        `$(foreach t,$(S),$(t))` has no `)` of its own until the call closes,
        so a reader that scans to the first one extracts the name
        `foreach t,$(S` and reports it as a variable nobody wrote. That sends
        a reader looking for an assignment that was never meant to exist, so
        the name is refused as the function call it is.
        """
        runner = self._document([
            {"name": "A", "raw_value": "$(foreach t,$(B),$(t))", "operator": "="}
        ])
        with pytest.raises(AssertionError) as raised:
            variable_expansion("A", runner=runner)
        assert "function call or a nested" in str(raised.value), (
            f"the extracted name is a function call, not a variable; got {raised.value}"
        )
        assert "never assigns" not in str(raised.value), (
            "reporting the call as an unassigned variable names a variable "
            f"nobody wrote; got {raised.value}"
        )

    def test_a_nested_reference_is_refused(self) -> None:
        """A reference inside a reference is not a variable name either.

        `$(B$(C))` nests one lookup inside another, which this reader does not
        implement; the name it extracts stops at `C`'s closer and is refused
        for the same reason a function call is.
        """
        runner = self._document([
            {"name": "A", "raw_value": "$(B$(C))", "operator": "="}
        ])
        with pytest.raises(AssertionError, match=r"function call or a nested"):
            variable_expansion("A", runner=runner)

    def test_a_reference_cycle_is_refused_rather_than_recursed(self) -> None:
        """A self-referential assignment is reported, not looped on."""
        runner = self._document([{"name": "A", "raw_value": "$(A)", "operator": "="}])
        with pytest.raises(AssertionError, match=r"expands itself"):
            variable_expansion("A", runner=runner)

    def test_a_missing_variables_list_is_refused(self) -> None:
        """A document with no `variables` fails rather than reading nothing."""
        with pytest.raises(AssertionError, match=r"must carry a `variables` list"):
            variable_expansion("A", runner=_runner_returning("{}"))

    def test_an_empty_assignment_table_is_refused(self) -> None:
        """No assignments at all is a parser change, not a Makefile with none."""
        runner = _runner_returning(json.dumps({"variables": [], "rules": []}))
        with pytest.raises(AssertionError, match=r"at least one assignment"):
            variable_expansion("A", runner=runner)

    def test_an_unimplemented_operator_is_refused(self) -> None:
        """An operator this reader does not model is reported, not read as `=`."""
        runner = self._document([{"name": "A", "raw_value": "1", "operator": "+="}])
        with pytest.raises(AssertionError, match=r"does not implement"):
            variable_expansion("A", runner=runner)

    def test_continuations_are_collapsed_before_splitting(self) -> None:
        """A continued list yields its words, not a stray backslash.

        The continuation backslash is not a `.py` path, so leaving it in place
        would put junk in the selector beside the patterns that do resolve.
        """
        runner = self._document([
            {"name": "A", "raw_value": "one.py \\\n  two.py", "operator": "="}
        ])
        resolved = variable_expansion("A", runner=runner)
        assert resolved == ("one.py", "two.py"), (
            f"a continuation must collapse to one space; got {resolved!r}, whose "
            "extra token would be a backslash standing in for a path pattern"
        )


class TestRecipeReading:
    """The recipe reader must report its own structure honestly.

    `recipe_of` is the boundary every recipe claim above it rests on: the
    wiring contract's data-flow check, the exemption's consumption check, and
    the tokenizer that both of them read through. On a healthy tree the estate
    satisfies every one of those, so the reader's own edge cases are never
    driven by them.
    """

    def _document(self, recipes: list[dict[str, str]]) -> Runner:
        """Return a runner serving a document with one `t` rule."""
        rule = {"targets": ["t"], "recipes": recipes}
        return _runner_returning(json.dumps({"variables": [], "rules": [rule]}))

    def test_recipe_entries_are_preserved_on_their_own_lines(self) -> None:
        """Two entries come back as two lines, not one joined command.

        The distinction is what makes a comment harmless: a `#` ends at its own
        entry's newline, so joining the entries onto one line would let a
        comment in the first entry disable the second.
        """
        runner = self._document([{"text": "echo one"}, {"text": "echo two"}])
        recipe = recipe_of("t", runner=runner)
        assert recipe == "echo one\necho two", (
            f"each entry must keep its own line; got {recipe!r}, which would "
            "let a comment in one entry disable the next"
        )

    def test_a_continuation_is_collapsed_within_an_entry(self) -> None:
        r"""A backslash-newline is collapsed, so the entry reads as one line.

        Asserted through the tokenizer because that is what the collapse is
        *for*. `shlex` implements no line continuation, so an uncollapsed
        backslash-newline arrives as a word containing the newline, and a token
        check would then be reasoning about a word the shell never sees.
        """
        runner = self._document([{"text": "echo one \\\n  two"}])
        recipe = recipe_of("t", runner=runner)
        assert "\\" not in recipe, (
            f"a continued entry is one logical line; got {recipe!r}, whose "
            "backslash would reach the tokenizer as part of a word"
        )
        assert "\n" not in recipe, (
            "the entry's continuation must not survive as a newline; got "
            f"{recipe!r}, which the tokenizer would read as a word of its own"
        )
        assert recipe_tokens(recipe) == recipe_tokens("echo one two"), (
            "the continued entry must tokenize as the one-line form does; got "
            f"{recipe_tokens(recipe)!r} from {recipe!r}, which is how a "
            "continuation would reach a caller as a word of its own"
        )

    def test_a_leading_silencing_marker_is_removed(self) -> None:
        """`@` suppresses make's own echo; it is not part of the command."""
        runner = self._document([{"text": "@echo one"}])
        recipe = recipe_of("t", runner=runner)
        assert recipe == "echo one", (
            f"the silencing marker is make's, not the shell's; got {recipe!r}"
        )

    def test_a_missing_target_is_refused_by_name(self) -> None:
        """A target the Makefile does not declare is reported, not read as empty."""
        runner = self._document([{"text": "echo one"}])
        with pytest.raises(AssertionError, match=r"must declare a absent target"):
            recipe_of("absent", runner=runner)

    def test_a_missing_rules_list_is_refused(self) -> None:
        """A document with no `rules` fails rather than reading no recipe."""
        with pytest.raises(AssertionError, match=r"must carry a `rules` list"):
            recipe_of("t", runner=_runner_returning("{}"))

    def test_a_comment_ends_at_its_own_entry(self) -> None:
        """The tokenizer reflects what the shell would run, entry by entry.

        `recipe_of` preserves the entry newline and `shlex`'s commenters run to
        the end of the line, so a commented first entry contributes nothing
        while the second still does. A reader that joined the entries would
        report the opposite for both halves.
        """
        tokens = recipe_tokens("# echo gone\necho kept")
        assert tokens == ("echo", "kept"), (
            f"only the live entry may contribute words; got {tokens!r}, so a "
            "comment is disabling commands it should not reach"
        )

    def test_a_quoted_hash_is_a_word_not_a_comment(self) -> None:
        """Quoting is honoured, so a literal `#` in an argument survives."""
        tokens = recipe_tokens("echo '# not a comment'")
        assert tokens == ("echo", "# not a comment"), (
            f"a quoted hash is an argument, not a comment opener; got {tokens!r}"
        )

    @pytest.mark.parametrize(
        ("rules", "expected"),
        [
            ([{"targets": "t", "recipes": []}], r"targets as a list"),
            ([{"targets": 3, "recipes": []}], r"targets as a list"),
            ([{"targets": ["t"], "recipes": "echo one"}], r"recipes as a list"),
            ([{"targets": ["t"], "recipes": ["echo one"]}], r"recipe entries"),
            ([{"targets": ["t"], "recipes": [{"text": 7}]}], r"as a string"),
            (["not-a-mapping"], r"rule must be a mapping"),
        ],
        ids=(
            "targets-as-string",
            "targets-as-int",
            "recipes-as-string",
            "entry-as-scalar",
            "text-as-int",
            "rule-as-scalar",
        ),
    )
    def test_a_malformed_document_is_refused_by_field(
        self,
        rules: list[object],
        expected: str,
    ) -> None:
        """Each malformed field is refused by name, not by its Python type.

        The reader narrows the `makeutil` document field by field, and each
        refusal has to say which field is wrong. Without the narrowing a
        wrong-typed `targets` is silently *iterated* — `"t"` is a list of one
        character that contains no target — so the reader would report the
        target missing while the document declared it, and everything above
        this reader would be reasoning about a file that is not there.
        """
        runner = _runner_returning(json.dumps({"variables": [], "rules": rules}))
        with pytest.raises(AssertionError, match=expected):
            recipe_of("t", runner=runner)

    def test_a_target_declared_by_another_rule_is_not_matched(self) -> None:
        """Membership, not substring: `t-extra` must not satisfy a `t` lookup.

        A narrower rule declaring `t-extra` sits before the `t` rule, so a
        reader testing the target list for a substring would return the wrong
        recipe — a mistake that reads as a passing contract everywhere above.
        """
        rules = [
            {"targets": ["t-extra"], "recipes": [{"text": "echo wrong"}]},
            {"targets": ["t"], "recipes": [{"text": "echo right"}]},
        ]
        runner = _runner_returning(json.dumps({"variables": [], "rules": rules}))
        assert recipe_of("t", runner=runner) == "echo right", (
            "a rule declaring `t-extra` must not answer a lookup for `t`"
        )
