"""Exercise the workflow readers the CI contracts are built on.

`tests/test_ci_workflow_contract.py` and the other workflow contracts ask
questions of this repository's real workflows, and that is the right way to ask
what the repository *does*. It is the wrong way to test the *readers*: every
case they can express is a case the estate already satisfies, so a reader's
malformed-input handling, its refusals, and its empty-input behaviour are never
driven at all. A reader could stop refusing anything and every contract above it
would keep passing on a healthy tree.

So the workflow readers are driven here with synthetic input:

* `tests/helpers/ci_documents.py` — the parse-and-narrow layer;
* `tests/helpers/ci_run_scripts.py` — the workflow sweep.

Each boundary's *success* path is also exercised, because a refusal test alone
cannot tell a reader that refuses malformed input from one that refuses
everything. The estate-wide contracts remain the authority on what this
repository declares; these cases are the authority on what the readers do when
what they read is wrong.

The Makefile readers are the sibling module
`tests/test_ci_makefile_boundaries.py`. They were split apart when this family
crossed the 400-line limit `AGENTS.md` sets and the lint gate enforces; the
boundary between the two is the one the helpers are already split along, so each
module drives the readers answering one kind of question.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.ci_documents import (
    narrow_steps,
    parse_document,
)
from tests.helpers.ci_run_scripts import run_scripts
from tests.helpers.ci_workflows import workflow_sources

if typ.TYPE_CHECKING:
    # `Path` appears only in the `tmp_path` parameters below; those annotations
    # are never evaluated, so the import stays out of the runtime namespace.
    from pathlib import Path

    from tests.helpers.workflow_types import Job


class TestDocumentNarrowing:
    """The parse-and-narrow layer must name what it refuses."""

    def test_a_well_formed_workflow_narrows_to_its_jobs(self) -> None:
        """The success path yields jobs, so the refusals are not blanket."""
        document = parse_document("jobs:\n  build:\n    steps:\n      - run: x\n", "w")
        jobs = typ.cast("dict[str, object]", document["jobs"])
        assert "build" in jobs, (
            f"the job's name must survive narrowing; got {sorted(jobs)!r}, so a "
            "reader that refused everything would pass every refusal case above"
        )

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
        steps = narrow_steps(job, "w:job")
        assert steps == [], (
            f"a `uses:` job must yield no steps, not be refused; got {steps!r}, "
            "which would mean the reader cannot tell a call from a malformed job"
        )

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

    def test_the_success_path_finds_the_estates_scripts(self) -> None:
        """The estate sweep finds scripts, so the refusals are not blanket."""
        found = run_scripts()
        assert found, "this repository's workflows must hold at least one run: step"
        assert len(workflow_sources()) > 1, (
            "the sweep is only meaningful over more than one workflow"
        )


class TestWorkflowSweepNarrowing:
    """The sweep must carry each script's location, and refuse the rest.

    `run_scripts` is what the exemption guard asks "does any CI step run this
    target" through, so a sweep that silently dropped a job, misnumbered a
    step, or read a reusable-workflow call as an empty job would change which
    exemptions are honoured. On the estate's own tree there is exactly one
    answer, and it is a satisfiable one, so none of those questions is settled
    by the sweep of this repository.
    """

    @staticmethod
    def _workflow(tmp_path: Path, name: str, body: str) -> Path:
        """Write one workflow into the sweep directory and return the directory."""
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(body, encoding="utf-8")
        return tmp_path

    def test_each_script_keeps_its_own_location(self, tmp_path: Path) -> None:
        """Every tuple names the workflow, job, and step the script came from.

        The step index counts *all* steps rather than only the `run:` ones, so
        the location matches the YAML a reader would open. Pairing a script with
        the wrong index would point a failure at an innocent step.
        """
        directory = self._workflow(
            tmp_path,
            "ci.yml",
            "jobs:\n"
            "  first:\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - run: make test\n"
            "      - name: named\n"
            "        run: make lint\n"
            "  second:\n"
            "    steps:\n"
            "      - run: make markdownlint\n",
        )
        assert run_scripts(directory) == [
            ("ci.yml", "first", "1", "make test"),
            ("ci.yml", "first", "2", "make lint"),
            ("ci.yml", "second", "0", "make markdownlint"),
        ], (
            "each script must carry the location it was declared at; a "
            "different pairing reports failures against the wrong step, and a "
            "missing tuple reports a contract as vacuously satisfied"
        )

    def test_a_reusable_workflow_call_contributes_no_script(
        self, tmp_path: Path
    ) -> None:
        """A `uses:` job declares no steps, and that is not a malformed job.

        The two must stay distinguishable: a call genuinely has no script to
        contribute, while a job whose `steps:` is the wrong shape has to be
        reported. Reading the call as malformed would refuse a valid workflow.
        """
        directory = self._workflow(
            tmp_path,
            "reusable.yml",
            "jobs:\n"
            "  call:\n"
            "    uses: owner/repo/.github/workflows/other.yml@main\n"
            "  local:\n"
            "    steps:\n"
            "      - run: make test\n",
        )
        found = run_scripts(directory)
        expected = [("reusable.yml", "local", "0", "make test")]
        assert found == expected, (
            "a reusable-workflow call has no `run:` script and a job beside it "
            "still does; a sweep that refused the call would report a valid "
            "workflow as malformed"
        )

    def test_a_step_without_a_run_key_contributes_nothing(self, tmp_path: Path) -> None:
        """A `uses:` step inside a job is skipped, not read as an empty script.

        An empty script would match a substring search for almost any command,
        so a step that runs nothing must contribute no tuple at all.
        """
        directory = self._workflow(
            tmp_path,
            "mixed.yml",
            "jobs:\n"
            "  build:\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - run: make test\n",
        )
        found = run_scripts(directory)
        assert [script for *_rest, script in found] == ["make test"], (
            f"only the `run:` step may contribute a script; got {found!r}, "
            "whose empty word would satisfy a substring search for anything"
        )

    def test_a_job_with_malformed_steps_is_refused(self, tmp_path: Path) -> None:
        """`steps:` of the wrong shape is reported, not swept as empty.

        Reporting it as empty is the silent vacuous pass: every "no step does
        X" contract over the job would pass having read nothing at all.
        """
        directory = self._workflow(
            tmp_path, "bad.yml", "jobs:\n  build:\n    steps: {}\n"
        )
        with pytest.raises(AssertionError, match=r"bad\.yml:build must declare steps"):
            run_scripts(directory)

    def test_a_workflow_that_is_not_a_mapping_is_refused(self, tmp_path: Path) -> None:
        """A YAML list is refused by file name rather than swept as empty."""
        directory = self._workflow(tmp_path, "list.yml", "- one\n- two\n")
        with pytest.raises(AssertionError, match=r"list\.yml must parse to a mapping"):
            run_scripts(directory)

    def test_a_workflow_declaring_no_jobs_is_refused(self, tmp_path: Path) -> None:
        """No `jobs:` key is a fault, not a workflow with nothing to run."""
        directory = self._workflow(tmp_path, "empty.yml", "name: nothing\n")
        with pytest.raises(AssertionError, match=r"empty\.yml must declare jobs"):
            run_scripts(directory)
