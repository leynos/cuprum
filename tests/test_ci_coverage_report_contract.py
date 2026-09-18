"""Contracts tying the generated coverage report to the step that consumes it.

The coverage step writes a report and the CodeScene step reads one; nothing in
either action connects them. Each accepts a ``format`` and a ``path``
independently, and both have defaults that differ between the two actions, so a
change to one lane's inputs can silently point the consumer at a file nobody
produced — or at a file whose encoding the consumer's parser cannot read.

That failure is quiet where it matters most. The CodeScene action only runs
with a token present and, in the trunk lane, only on main, so a mismatched pair
is discovered after a merge rather than on the pull request that introduced it.

Three contracts are asserted here, and each covers a way the pair can drift:

* the report the publish lane consumes must be the one its own job generates,
* the two lanes must generate the same format, and
* the format each lane declares must be one the consuming CLI actually parses.

Absence is asserted against the parsed document rather than the raw text: a
``format`` mentioned in a comment is inert, and only the resolved input value
says what the step will do.
"""

from __future__ import annotations

import typing as typ

from tests.helpers.ci_runners import GENERATE_COVERAGE, step_inputs, steps

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Step

#: The workflow and job that both generate the report and upload it to
#: CodeScene. Only this lane has both steps, so only here can the two be
#: compared directly.
PUBLISH_LANE = ("coverage-main.yml", "coverage-upload")

#: The pull-request lane, which generates the report it ratchets against.
PULL_REQUEST_LANE = ("ci.yml", "coverage")

#: Both coverage lanes, for the contracts that are properties of the estate
#: rather than of one workflow.
COVERAGE_LANES = (PULL_REQUEST_LANE, PUBLISH_LANE)

#: The CodeScene action's `uses:` prefix. Matched as a prefix rather than a
#: substring so a step naming a differently owned action cannot satisfy it.
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


def _coverage_step(workflow_name: str, job_name: str) -> Step:
    """Return the single shared generate-coverage step of a job."""
    matches = [
        step
        for step in steps(workflow_name, job_name)
        if step.get("uses") == GENERATE_COVERAGE
    ]
    assert len(matches) == 1, (
        f"{workflow_name}:{job_name} must invoke the coverage action exactly "
        f"once, found {len(matches)}"
    )
    return matches[0]


def _codescene_step(workflow_name: str, job_name: str) -> Step:
    """Return the single CodeScene upload step of a job."""
    matches = [
        step
        for step in steps(workflow_name, job_name)
        if str(step.get("uses", "")).startswith(CODESCENE_ACTION)
    ]
    assert len(matches) == 1, (
        f"{workflow_name}:{job_name} must invoke the CodeScene action exactly "
        f"once, found {len(matches)}"
    )
    return matches[0]


def _generated_format(workflow_name: str, job_name: str) -> str:
    """Return the coverage format a job's generate step writes.

    The ``format`` input is read directly rather than defaulted: both actions
    default to ``cobertura``, so a lane that stops declaring the input keeps
    working until someone changes the consuming side, and the contract would
    stop noticing. Requiring the declaration keeps the pair explicit.

    Returns
    -------
    str
        The format the lane's generate step declares.
    """
    inputs = step_inputs(
        _coverage_step(workflow_name, job_name),
        f"{workflow_name}:{job_name} generate-coverage must declare inputs",
    )
    declared = inputs.get("format")
    assert isinstance(declared, str), (
        f"{workflow_name}:{job_name} must declare the coverage format "
        f"explicitly, found {declared!r}; a defaulted format is invisible to "
        f"the step that consumes the report"
    )
    return declared


def _generated_path(workflow_name: str, job_name: str) -> str:
    """Return the report path a job's generate step writes.

    Returns
    -------
    str
        The non-empty ``output-path`` the generate step declares.
    """
    inputs = step_inputs(
        _coverage_step(workflow_name, job_name),
        f"{workflow_name}:{job_name} generate-coverage must declare inputs",
    )
    declared = inputs.get("output-path")
    assert isinstance(declared, str), (
        f"{workflow_name}:{job_name} must declare output-path, found {declared!r}"
    )
    assert declared, (
        f"{workflow_name}:{job_name} output-path must not be empty, found {declared!r}"
    )
    return declared


def _consumed_path(workflow_name: str, job_name: str) -> str | None:
    """Return the report path a job's CodeScene step reads, if it declares one.

    ``None`` means the step left ``path`` unset, which is not the same as
    asking for the format's default: the action resolves an unset or sentinel
    path to a name of its own choosing, and that name is what the file must be
    called. The caller resolves both to a concrete name before comparing.

    Returns
    -------
    str | None
        The declared path, or ``None`` when the step leaves it to the action.
    """
    inputs = step_inputs(
        _codescene_step(workflow_name, job_name),
        f"{workflow_name}:{job_name} CodeScene step must declare inputs",
    )
    declared = inputs.get("path")
    if declared is None or declared == "__auto__":
        return None
    assert isinstance(declared, str), (
        f"{workflow_name}:{job_name} CodeScene path must be a string, "
        f"found {declared!r}"
    )
    assert declared, (
        f"{workflow_name}:{job_name} CodeScene path must not be empty, "
        f"found {declared!r}"
    )
    return declared


def test_the_publish_lane_uploads_the_report_its_own_job_generates() -> None:
    """The generated path and the uploaded path must name the same file.

    This is the contract the failing run needed and did not have. The two
    actions are independent: the generator writes wherever ``output-path``
    says and the uploader reads wherever ``path`` says, and both default to
    the same name by coincidence rather than by construction. The upload step
    runs last, so a mismatch fails after the whole instrumented suite has run.
    """
    workflow_name, job_name = PUBLISH_LANE
    generated = _generated_path(workflow_name, job_name)
    consumed = _consumed_path(workflow_name, job_name)

    if consumed is None:
        consumed = DEFAULT_REPORT_PATH[_generated_format(workflow_name, job_name)]
    assert generated == consumed, (
        f"{workflow_name}:{job_name} generates {generated!r} but uploads "
        f"{consumed!r}; the CodeScene step would fail on a missing file"
    )


def test_both_coverage_lanes_generate_the_same_format() -> None:
    """The two lanes must write comparable reports from the same test suite.

    They run the same suite with the same flags, so a format that differed
    between them would mean the baseline main stores and the report a pull
    request ratchets against were produced by different tooling. The ratchet
    compares the numbers, not the shapes, so the drift would surface only as an
    unexplained coverage change.
    """
    formats = {
        f"{workflow_name}:{job_name}": _generated_format(workflow_name, job_name)
        for workflow_name, job_name in COVERAGE_LANES
    }

    assert len(set(formats.values())) == 1, (
        f"coverage lanes must generate one format, found {formats!r}"
    )


def test_the_generated_format_is_one_the_consumer_accepts() -> None:
    """Each lane must declare a format the pinned CodeScene action parses.

    The action's validate step hard-fails on anything outside its accepted set,
    which would make this an obvious failure — except that a format the action
    accepts is still not necessarily a format its CLI parses. Pinning the
    accepted set here means a change to the declaration has to confront the
    consumer's contract rather than only the workflow's.
    """
    for workflow_name, job_name in COVERAGE_LANES:
        declared = _generated_format(workflow_name, job_name)
        assert declared in ACCEPTED_FORMATS, (
            f"{workflow_name}:{job_name} declares format {declared!r}; the "
            f"pinned CodeScene action accepts {sorted(ACCEPTED_FORMATS)}"
        )
