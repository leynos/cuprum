"""Exercise the parsing boundaries the CI contracts are built on.

The contracts in `tests/test_ci_suite_wiring_contract.py` and
`tests/test_ci_test_selection_contract.py` read real artefacts — this
repository's Makefile and its workflows — and that is the right way to assert
what the repository does. It is the wrong way to test the *readers*: every case
they can express is a case the estate already satisfies, so a reader's
malformed-input handling, its refusals, and its empty-input behaviour are never
driven at all. A reader could stop refusing anything and every contract above
it would keep passing on a healthy tree.

So this module drives the three parsing boundaries with synthetic input:

* `tests/helpers/makefile.py` — the Makefile read, and its process boundary;
* `tests/helpers/ci_documents.py` — the parse-and-narrow layer;
* `tests/helpers/ci_run_scripts.py` — the workflow sweep.

Each boundary's *success* path is also exercised, because a refusal test alone
cannot tell a reader that refuses malformed input from one that refuses
everything. The estate-wide contracts remain the authority on what this
repository declares; these cases are the authority on what the readers do when
what they read is wrong.
"""

from __future__ import annotations

import json
import subprocess  # ruff: ignore[suspicious-subprocess-import] - test doubles for a fixed argv.
import typing as typ

import pytest

from tests.helpers.ci_documents import (
    narrow_steps,
    parse_document,
)
from tests.helpers.ci_run_scripts import run_scripts
from tests.helpers.ci_workflows import workflow_sources
from tests.helpers.makefile import (
    Runner,
    makeutil_document,
    variable_expansion,
)

if typ.TYPE_CHECKING:
    # `Path` appears only in the `tmp_path` parameters below; those annotations
    # are never evaluated, so the import stays out of the runtime namespace.
    from pathlib import Path

    from tests.helpers.workflow_types import Job


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
        return _completed(argv, returncode, stdout)

    return run


def _runner_raising(error: BaseException) -> Runner:
    """Return a fake runner that raises instead of returning."""

    def run(_argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
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
        assert variable_expansion("A", runner=runner) == ("one.py", "two.py")


class TestDocumentNarrowing:
    """The parse-and-narrow layer must name what it refuses."""

    def test_a_well_formed_workflow_narrows_to_its_jobs(self) -> None:
        """The success path yields jobs, so the refusals are not blanket."""
        document = parse_document("jobs:\n  build:\n    steps:\n      - run: x\n", "w")
        assert "build" in typ.cast("dict[str, object]", document["jobs"])

    def test_a_document_that_is_not_a_mapping_is_refused(self) -> None:
        """A YAML list is not a workflow document."""
        with pytest.raises(AssertionError, match=r"must parse to a mapping"):
            parse_document("- one\n- two\n", "w.yml")

    def test_invalid_yaml_is_refused_by_name(self) -> None:
        """Malformed YAML cites the file rather than raising from the parser."""
        with pytest.raises(AssertionError, match=r"w\.yml"):
            parse_document("key: [unclosed\n", "w.yml")

    def test_a_job_without_steps_yields_none(self) -> None:
        """A reusable-workflow call legitimately declares no steps."""
        job = typ.cast("Job", {"uses": "owner/repo/.github/workflows/x.yml@main"})
        assert narrow_steps(job, "w:job") == []

    def test_steps_of_the_wrong_shape_are_refused_not_read_as_empty(self) -> None:
        """`steps: {}` is malformed, not empty.

        The distinction is load-bearing: reporting a malformed job as "no
        steps" lets every "no step does X" contract over it pass having read
        nothing, which is a silent vacuous pass.
        """
        with pytest.raises(AssertionError, match=r"must declare steps as a list"):
            narrow_steps(typ.cast("Job", {"steps": {}}), "w:job")

    def test_a_step_that_is_not_a_mapping_is_refused(self) -> None:
        """A string where a step belongs is refused, citing its index."""
        with pytest.raises(AssertionError, match=r"step 0 must be a mapping"):
            narrow_steps(typ.cast("Job", {"steps": ["not a mapping"]}), "w:job")


class TestWorkflowSweep:
    """The sweep must report its own emptiness rather than return none."""

    def test_a_directory_with_no_workflow_is_refused(self, tmp_path: Path) -> None:
        """An empty sweep would satisfy every "no step does X" contract."""
        with pytest.raises(AssertionError, match=r"not a workflow directory|holds no"):
            run_scripts(tmp_path)

    def test_the_success_path_finds_this_repositorys_scripts(self) -> None:
        """The estate sweep finds scripts, so the refusals are not blanket."""
        found = run_scripts()
        assert found, "this repository's workflows must hold at least one run: step"
        assert len(workflow_sources()) > 1, (
            "the sweep is only meaningful over more than one workflow"
        )
