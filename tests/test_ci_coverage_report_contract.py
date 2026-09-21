"""Contracts tying the generated coverage report to the step that consumes it.

The coverage step writes a report and the CodeScene step reads one; nothing in
either action connects them. Each accepts a ``format`` and a ``path``
independently, and both have defaults that differ between the two actions, so a
change to one lane's inputs can silently point the consumer at a file nobody
produced — or at a file whose encoding the consumer's parser cannot read.

That failure is quiet where it matters most. The CodeScene action only runs
with a token present and, in the trunk lane, only on main, so a mismatched pair
is discovered after a merge rather than on the pull request that introduced it.

Four contracts are asserted here, and each covers a way the pair can drift:

* the report the publish lane consumes must be the one its own job generates,
* the generator must run before the uploader that reads its file,
* the two lanes must generate the same format, and
* the format each lane declares must be one the consuming CLI actually parses.

Absence is asserted against the parsed document rather than the raw text: a
``format`` mentioned in a comment is inert, and only the resolved input value
says what the step will do.
"""

from __future__ import annotations

from tests.helpers.ci_runners import (
    GENERATE_COVERAGE,
    single_step_position_using,
    single_step_using,
    step_inputs,
)

#: The workflow and job that both generate the report and upload it to
#: CodeScene. Only this lane has both steps, so only here can the two be
#: compared directly.
PUBLISH_LANE = ("coverage-main.yml", "coverage-upload")

#: The pull-request lane, which generates the report it ratchets against.
PULL_REQUEST_LANE = ("ci.yml", "coverage")

#: Both coverage lanes, for the contracts that are properties of the estate
#: rather than of one workflow.
COVERAGE_LANES = (PULL_REQUEST_LANE, PUBLISH_LANE)

#: The CodeScene action's `uses:` prefix. The revision is deliberately not part
#: of this locator: the revision is not what these four contracts are about, and
#: a locator that carries one goes stale the moment a pin moves on the trunk.
#: The suite then fails from a pull request that never touched the workflow, as
#: it did when the uploader moved to `a5765019`. The revision is asserted by
#: `tests/test_ci_codescene_boundary.py`, which the pin-moving commit updates in
#: the same change, so a re-pin still fails closed rather than quietly
#: re-pointing these contracts at a different action.
CODESCENE_ACTION = "leynos/shared-actions/.github/actions/upload-codescene-coverage@"

#: The formats the pinned CodeScene action accepts. Its `Validate inputs` step
#: rejects anything else before the CLI is installed, so a format outside this
#: set fails the run rather than the upload.
ACCEPTED_FORMATS = frozenset({"cobertura", "lcov"})

#: The report file each accepted format must be written to. The action's
#: `Determine coverage file` step substitutes exactly these names when `path`
#: is left at its sentinel, and `cs-coverage check` infers the format from the
#: file extension for the LCOV case.
DEFAULT_REPORT_PATH = {"cobertura": "coverage.xml", "lcov": "lcov.info"}


def _declared(inputs: dict[str, object], key: str, where: str) -> str:
    """Return one ``with`` input of a step, as a non-empty string."""
    # Every contract below reads a value the step is expected to declare rather
    # than one it inherits: both actions default these inputs, so a lane that
    # stops declaring one keeps working until someone changes the other side,
    # and the contract would stop noticing.
    declared = inputs.get(key)
    # Requiring the declaration is what keeps the pair explicit.
    assert isinstance(declared, str), (
        f"{where} must declare {key}, found {declared!r}; a defaulted value is "
        f"invisible to the other side of this contract"
    )
    assert declared, f"{where} {key} must not be empty, found {declared!r}"
    return declared


def _generated(workflow_name: str, job_name: str, key: str) -> str:
    """Return one declared input of the job's generate-coverage step."""
    # A thin composition of ``single_step_using`` and ``_declared``; the
    # ``where`` label is what makes a failure message name the offending step.
    where = f"{workflow_name}:{job_name}"
    step = single_step_using(workflow_name, job_name, uses=GENERATE_COVERAGE)
    return _declared(step_inputs(step, f"{where} must declare inputs"), key, where)


def _consumed_path(workflow_name: str, job_name: str) -> str | None:
    """Return the report path a job's CodeScene step reads, if it declares one."""
    where = f"{workflow_name}:{job_name} CodeScene step"
    step = single_step_using(
        workflow_name, job_name, uses=CODESCENE_ACTION, prefix=True
    )
    inputs = step_inputs(step, f"{where} must declare inputs")
    declared = inputs.get("path")
    # ``None`` means the step left ``path`` unset or at its sentinel, which is
    # not the same as letting the caller choose: the action then resolves a
    # name of its own, and that name is what the file must be called. The
    # caller resolves both to a concrete name before comparing.
    if declared is None or declared == "__auto__":
        return None
    return _declared(inputs, "path", where)


class TestCoverageReportContract:
    """The four ways the coverage generator and its consumer can drift apart."""

    def test_the_publish_lane_uploads_the_report_its_own_job_generates(self) -> None:
        """The generated path and the uploaded path must name the same file.

        The two actions are independent: the generator writes wherever
        ``output-path`` says and the uploader reads wherever ``path`` says, and
        both default to the same name by coincidence rather than by
        construction. The upload step runs last, held there by the ordering
        contract below, so a mismatch fails after the whole instrumented suite
        has run.
        """
        workflow_name, job_name = PUBLISH_LANE
        generated = _generated(workflow_name, job_name, "output-path")
        consumed = _consumed_path(workflow_name, job_name)

        if consumed is None:
            consumed = DEFAULT_REPORT_PATH[
                _generated(workflow_name, job_name, "format")
            ]
        assert generated == consumed, (
            f"{workflow_name}:{job_name} generates {generated!r} but uploads "
            f"{consumed!r}; the CodeScene step would fail on a missing file"
        )

    def test_the_generator_runs_before_the_step_that_reads_its_file(self) -> None:
        """The upload must come after the generation it depends on.

        A job's steps run in file order, so this is the whole of the sequencing
        the pair gets: move the upload above the generator and it reads last
        run's report, or none at all, while every input still matches and the
        contracts above stay green. CI would catch it only on main, after the
        merge that broke it, and only when the token is present.
        """
        workflow_name, job_name = PUBLISH_LANE
        generated_at = single_step_position_using(
            workflow_name, job_name, uses=GENERATE_COVERAGE
        )
        consumed_at = single_step_position_using(
            workflow_name, job_name, uses=CODESCENE_ACTION, prefix=True
        )

        assert generated_at < consumed_at, (
            f"{workflow_name}:{job_name} uploads at step {consumed_at} but "
            f"generates at step {generated_at}; the upload would read a report "
            f"that does not exist yet"
        )

    def test_both_coverage_lanes_generate_the_same_format(self) -> None:
        """The two lanes must write comparable reports from the same test suite.

        They run the same suite with the same flags, so a format that differed
        between them would mean the baseline main stores and the report a pull
        request ratchets against were produced by different tooling. The ratchet
        compares the numbers, not the shapes, so the drift would surface only as
        an unexplained coverage change.
        """
        formats = {
            f"{workflow_name}:{job_name}": _generated(workflow_name, job_name, "format")
            for workflow_name, job_name in COVERAGE_LANES
        }

        assert len(set(formats.values())) == 1, (
            f"coverage lanes must generate one format, found {formats!r}"
        )

    def test_the_generated_format_is_one_the_consumer_accepts(self) -> None:
        """Each lane must declare a format the pinned CodeScene action parses.

        The action's validate step hard-fails on anything outside its accepted
        set, which would make this an obvious failure — except that a format the
        action accepts is still not necessarily a format its CLI parses. Pinning
        the accepted set here means a change to the declaration has to confront
        the consumer's contract rather than only the workflow's.
        """
        for workflow_name, job_name in COVERAGE_LANES:
            declared = _generated(workflow_name, job_name, "format")
            assert declared in ACCEPTED_FORMATS, (
                f"{workflow_name}:{job_name} declares format {declared!r}; the "
                f"pinned CodeScene action accepts {sorted(ACCEPTED_FORMATS)}"
            )
