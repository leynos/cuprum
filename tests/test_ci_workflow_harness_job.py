"""Contract tests for the opt-in job that runs the `act` harness in CI.

The harness itself lives in `tests/integration/` and is exercised by
`make test-act`. CI declares it in `benchmark-gate-harness.yml`. Every
way that declaration can go wrong fails *quietly*, which is why it is asserted
here rather than left to the harness's own tests:

- drop `CUPRUM_REQUIRE_ACT=1` and a machine with no container runtime turns
  every scenario into a skip, so the job is green for having run nothing;
- move the job onto a metered runner and the cheapest job in the workflow
  becomes a paid one, for work that sleeps on a container start;
- drop the `act` install and the job fails at the first scenario rather than
  at the step that was supposed to provide it;
- provide a container runtime that leaves no socket behind, and every scenario
  fails the skip refusal on a job whose runtime step reported success;
- admit the job on `pull_request` and every pull request pays the scenario
  cost for a boundary that changes rarely.

`make test-act` is where the requirement is actually enforced — the Makefile
sets the variable, so the declaration cannot drift from the command that
consumes it. These tests read the job's declaration and assert the command the
job runs is that target.
"""

from __future__ import annotations

import re
import typing as typ

import pytest

from tests.helpers.act_harness import IMAGE
from tests.helpers.ci_runners import GITHUB_HOSTED_JOBS
from tests.helpers.ci_workflows import workflow_document
from tests.helpers.workflow import job, script_of, step_named, steps
from tests.helpers.workflow_shell import script_runs_command

if typ.TYPE_CHECKING:
    from tests.helpers.workflow import Workflow

JOB = "workflow-harness"
HARNESS_STEP = "Run the workflow integration harness"
INSTALL_ACT_STEP = "Install act"
#: The Makefile target that runs the scenarios. It is the one place
#: `CUPRUM_REQUIRE_ACT=1` is set, so the job must invoke it rather than
#: `pytest` directly — a direct invocation would drop the refusal and let the
#: job pass on a machine where every scenario skipped.
HARNESS_TARGET = "make test-act"
#: The runtime the scenarios bind to. The probe in
#: `tests/helpers/act_runtime.py` accepts either command, so this is the job's
#: choice rather than the harness's: the GitHub-hosted image installs and
#: starts exactly one daemon, and it is this one.
RUNTIME = "docker"
#: The pinned image. The harness pins the same tag; this asserts the job warms
#: the image the harness will ask for, rather than one that merely looks close.
HARNESS_WORKFLOW = "benchmark-gate-harness.yml"


@pytest.fixture
def workflow_data() -> Workflow:
    """Read the dedicated harness workflow.

    Returns
    -------
    Workflow
        Parsed workflow under contract test.
    """
    return typ.cast("Workflow", workflow_document(HARNESS_WORKFLOW))


def _harness_step(workflow_data: Workflow) -> dict[str, object]:
    """Return the declared step that runs the harness."""
    return step_named(workflow_data, JOB, HARNESS_STEP)


def test_the_harness_job_stays_on_a_github_hosted_runner() -> None:
    """Keep sleeping, container-bound work off a metered build slot.

    The placement manifest classifies this job; this asserts the classification
    the manifest asks for, so a job that moved to Ubicloud fails here and in
    `tests/test_ci_runner_placement.py` rather than only in the latter.
    """
    declared = GITHUB_HOSTED_JOBS[HARNESS_WORKFLOW]
    assert JOB in declared, (
        f"{JOB} must be classified in the GitHub-hosted manifest in "
        f"tests/helpers/ci_runners.py; found {declared!r}"
    )


def test_the_harness_job_runs_the_target_that_refuses_a_skip(
    workflow_data: Workflow,
) -> None:
    """Run `make test-act`, which is the one place the refusal is set.

    The scenario suite skips where no container runtime is present, so a job
    that invoked `pytest` directly would be green on a runner where the runtime
    silently failed to install. The Makefile target sets
    `CUPRUM_REQUIRE_ACT=1`, which turns that skip into a failure.
    """
    script = script_of(_harness_step(workflow_data))
    assert script is not None, f"{JOB} must run a shell command"
    assert HARNESS_TARGET in script, (
        f"{JOB} must run `{HARNESS_TARGET}`, which sets CUPRUM_REQUIRE_ACT=1; "
        f"a direct pytest invocation would let a skipped suite pass. Script: "
        f"{script!r}"
    )


def test_the_harness_job_installs_act_under_a_verified_checksum(
    workflow_data: Workflow,
) -> None:
    """Fetch `act` by version and checksum, not by tag.

    `act` is absent from the runner image, and the suite skips rather than
    fails when it is missing. An install that silently stopped working would
    therefore produce a green job that tested nothing, so the download is
    checksum-verified before the archive is unpacked.
    """
    script = script_of(step_named(workflow_data, JOB, INSTALL_ACT_STEP))
    assert script is not None, f"{JOB} must install act with a shell command"
    for required in ("ACT_VERSION='0.2.89'", "sha256sum --check", "ACT_SHA256="):
        assert required in script, (
            f"{JOB}'s act install must pin and verify the archive; "
            f"{required!r} missing from {script!r}"
        )


def test_the_harness_job_reaches_the_runtime_it_declares(
    workflow_data: Workflow,
) -> None:
    """Make the job's own greenness depend on a usable daemon.

    The assertion is that some step *runs* ``<runtime> info``: that command
    reaches the daemon, so a job carrying it cannot pass while the runtime is
    unusable — which is exactly the failure this guards. A step that installs a
    runtime package and stops there reports success either way, and the
    resulting job fails later inside the harness with the skip refusal rather
    than a message naming the dependency that moved. A substring check for the
    runtime's name would pass on that step; this does not.
    """
    scripts = [
        script for step in steps(workflow_data, JOB) if (script := script_of(step))
    ]
    assert any(script_runs_command(script, f"{RUNTIME} info") for script in scripts), (
        f"{JOB} must run `{RUNTIME} info`, which fails unless the daemon the "
        f"harness binds is actually reachable; scripts were {scripts!r}"
    )


def test_the_harness_job_warms_the_image_the_harness_binds(
    workflow_data: Workflow,
) -> None:
    """Pull the pinned tag before the first scenario needs it.

    The scenarios bind `IMAGE`; without a warm image the first of them spends
    its timeout on the pull instead of on the job under test, and reports it as
    a scenario failure rather than as a cold cache.
    """
    scripts = [
        script for step in steps(workflow_data, JOB) if (script := script_of(step))
    ]
    assert re.fullmatch(r"[^@]+@sha256:[a-f0-9]{64}", IMAGE), (
        "runner image must have an immutable SHA-256 digest"
    )
    assert any(IMAGE in script for script in scripts), (
        f"{JOB} must pull {IMAGE}, the image the harness binds; scripts were "
        f"{scripts!r}"
    )


#: `None` means the `inputs` context has no such key, which is the state on
#: every event that is not a dispatch: GitHub documents that dereferencing a
#: nonexistent property "will evaluate to an empty string", so the input is
#: falsy rather than the job being un-evaluatable. Asserting a `True` input on
#: a push would be asserting an unreachable state and would pass for the wrong
#: reason.
ADMISSION_CASES = [
    pytest.param("pull_request", None, False, id="pull-request-never"),
    pytest.param("push", None, False, id="push-never"),
    pytest.param("schedule", None, True, id="scheduled-always"),
    pytest.param("workflow_dispatch", True, True, id="dispatch-opted-in"),
    pytest.param("workflow_dispatch", False, False, id="dispatch-opted-out"),
]


@pytest.mark.parametrize(("event_name", "dispatch_input", "expected"), ADMISSION_CASES)
def test_the_harness_job_is_admitted_only_where_it_was_asked_for(
    event_name: str, dispatch_input: object, expected: bool, workflow_data: Workflow
) -> None:
    """Keep the scenario cost off every event nobody asked it to run on.

    A pull request or a push would pay 15-27 s per scenario for a boundary that
    changes rarely, and a dispatch that opted out must stay opted out — the
    default has to be off or the input would only ever document a formality.
    """
    condition = str(job(workflow_data, JOB).get("if", ""))
    resolved = _admits(condition, event_name=event_name, dispatch_input=dispatch_input)
    assert resolved is expected, (
        f"{event_name} with workflow-harness={dispatch_input!r} resolves to "
        f"{resolved} for job {JOB!r}, expected {expected}; condition was "
        f"{condition!r}"
    )


def test_the_dispatch_input_defaults_to_off() -> None:
    """Make the opt-in real: a dispatch that says nothing runs nothing."""
    document = workflow_document(HARNESS_WORKFLOW)
    # YAML 1.1 reads the `on:` key as the boolean `True`, which is why this
    # reads both.
    triggers = document.get("on", document.get(True))
    assert isinstance(triggers, dict), "ci.yml must declare an on: mapping"
    dispatch = triggers.get("workflow_dispatch")
    assert isinstance(dispatch, dict), "ci.yml must declare dispatch inputs"
    inputs = dispatch.get("inputs")
    assert isinstance(inputs, dict), "the dispatch trigger must declare a mapping"
    declared = inputs.get("workflow-harness")
    assert isinstance(declared, dict), "workflow-harness must be a mapping"
    assert declared.get("type") == "boolean", "workflow-harness must be a boolean"
    assert declared.get("default") is False, (
        "a dispatch input that defaulted to true would make the opt-in nominal; "
        f"got {declared.get('default')!r}"
    )


def _admits(condition: str, *, event_name: str, dispatch_input: object) -> bool:
    """Evaluate the job's `if:` for one event, as Actions would.

    The expression is deliberately small — a disjunction of an event-name
    comparison and a boolean input — so it is evaluated structurally rather
    than by handing the text to a general expression engine. Anything the
    parser does not recognize is a failure, not a silent `False`, so a job
    whose condition was rewritten into an unhandled shape fails here instead of
    being reported as never admitted.

    Parameters
    ----------
    condition : str
        The job's declared `if:` expression.
    event_name : str
        The `github.event_name` the condition is evaluated against.
    dispatch_input : object
        The value of the `workflow-harness` dispatch input, or ``None`` where
        the `inputs` context has no such key.

    Returns
    -------
    bool
        Whether any disjunct of ``condition`` holds for that event.
    """
    assert condition, f"{JOB} must declare an if: condition"
    terms = [term.strip() for term in condition.split("||")]
    results = [
        _term_holds(term, event_name=event_name, dispatch_input=dispatch_input)
        for term in terms
    ]
    return any(results)


def _term_holds(term: str, *, event_name: str, dispatch_input: object) -> bool:
    """Evaluate one disjunct of the job's condition.

    Parameters
    ----------
    term : str
        One disjunct, split on `||`.
    event_name : str
        The `github.event_name` the term is evaluated against.
    dispatch_input : object
        The value of the `workflow-harness` dispatch input. ``None`` is the
        state where the `inputs` context has no such key, and that is falsy for
        the documented reason rather than by accident: GitHub resolves a
        nonexistent property to an empty string.

    Returns
    -------
    bool
        Whether the term holds.

    Raises
    ------
    AssertionError
        If the term is not one this reader understands.
    """
    if term.startswith("github.event_name =="):
        return _comparison_holds(term, event_name)
    if term == "inputs.workflow-harness":
        return dispatch_input is True
    message = (
        f"unhandled term {term!r} in the {JOB} job's condition; extend this "
        "reader rather than leaving the job's admission untested"
    )
    raise AssertionError(message)


#: An event-name comparison whose right-hand side is a quoted literal. The
#: literal is captured rather than searched for, because `event_name in term`
#: would also be satisfied by a *substring* of a longer event name.
_EVENT_NAME_COMPARISON = re.compile(
    r"github\.event_name == (?:'([^']*)'|\"([^\"]*)\")\s*"
)


def _comparison_holds(term: str, event_name: str) -> bool:
    """Return whether an event-name comparison term holds for ``event_name``.

    The comparison must be the whole term, and its right-hand side must be a
    quoted literal. A term carrying a conjunct — ``github.event_name == 'x' &&
    something`` — is rejected rather than answered, because this reader
    evaluates the event-name half only and would otherwise report that half's
    verdict as the whole term's.

    Returns
    -------
    bool
        Whether the event name equals the complete quoted literal.

    Raises
    ------
    AssertionError
        If the term is not a supported equality comparison.
    """
    match = _EVENT_NAME_COMPARISON.fullmatch(term)
    if match is None:
        message = (
            f"unhandled event-name term {term!r} in the {JOB} job's condition; "
            "only a bare `github.event_name == '...'` comparison is understood, "
            "because a term carrying `&&` would have its conjunct ignored"
        )
        raise AssertionError(message)
    return event_name == (match.group(1) or match.group(2))


@pytest.mark.parametrize(
    ("term", "event_name", "expected"),
    [
        pytest.param(
            "github.event_name == 'schedule'", "schedule", True, id="exact-match"
        ),
        pytest.param("github.event_name == 'schedule'", "push", False, id="exact-miss"),
        pytest.param(
            'github.event_name == "schedule"', "schedule", True, id="double-quoted"
        ),
        pytest.param(
            "github.event_name == 'schedule'",
            "dispatch",
            False,
            id="no-substring-admit",
        ),
    ],
)
def test_an_event_name_comparison_matches_the_whole_literal(
    term: str, event_name: str, expected: bool
) -> None:
    """Compare the quoted literal, not a substring that happens to appear in it.

    A substring test admits an event whose name is contained in the literal.
    No such event exists today, so the mistake would sit undetected until the
    condition grew a second comparison that made it matter.
    """
    assert _comparison_holds(term, event_name) is expected, (
        f"{term!r} against event {event_name!r} must be {expected}"
    )


@pytest.mark.parametrize(
    "term",
    [
        pytest.param(
            "github.event_name == 'workflow_dispatch' && inputs.workflow-harness",
            id="conjunction",
        ),
        pytest.param(
            "github.event_name == 'schedule' || true", id="disjunction-in-term"
        ),
        pytest.param("github.event_name != 'push'", id="not-equal"),
        pytest.param("github.event_name", id="bare-context"),
    ],
)
def test_an_unhandled_event_name_term_is_refused(term: str) -> None:
    """Refuse a shape the reader would otherwise answer wrongly.

    The conjunction is the case that matters: answered by its event-name half
    alone, it would report `admitted` for an event that also had to satisfy the
    conjunct. Refusing is the only safe response, and it keeps the reader
    honest if the condition is ever rewritten.
    """
    with pytest.raises(AssertionError, match="unhandled event-name term"):
        _comparison_holds(term, "workflow_dispatch")
