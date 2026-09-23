"""Contracts for where CodeScene publication may happen, and what it needs.

CodeScene accepts an upload only for a branch it analyses, so publication
belongs to ``coverage-main.yml`` and nothing a pull request runs may reach for
it even speculatively. Four separate controls hold that line, and each one
reads as satisfied while the others are intact:

* no workflow a pull request can run may mention CodeScene or its client,
* none of them may name, forward, or inherit the access token,
* the trunk publisher must ask for the upload mode rather than the
  pull-request check mode, and
* the upload's guard must require the main ref as well as the token.

"A workflow a pull request can run" is a closure, not a trigger list: it
follows same-repository reusable-workflow calls, because a called workflow runs
on the caller's pull request and ``secrets: inherit`` hands it the token.
``tests/test_ci_codescene_closure.py`` proves each reader against constructed
workflows, since these contracts, parametrized over this repository's correct
files, pass whether or not the readers discriminate.

A second group of contracts covers what the publisher may ask the shared
action for. The action's committed CLI manifest is now the trust anchor for the
binary it installs, so the checksum input that once carried that trust is
deprecated and rejected, the repository variable behind it has no consumer, and
the dispatch that maintained the variable is dead code. Each of those is its
own clause, and the pin they depend on is asserted by value so a stale
revision cannot quietly bring the input back.

Every contract here is a property of the workflows as shipped, so none of them
needs a live CodeScene project. Absence is asserted against the parsed
document rather than the raw text, because a token or project URL sitting in
the file is harmless while no scope hands it to a step, and only the resolved
scope says which of the two it is.
"""

from __future__ import annotations

import re
import typing as typ

from tests.helpers.ci_closure import PULL_REQUEST_EVENTS, reachable
from tests.helpers.ci_codescene import (
    contact_findings,
    missing_upload_conjuncts,
    token_findings,
)
from tests.helpers.ci_runners import (
    WORKFLOW_DIR,
    step_inputs,
    steps,
    workflow_sources,
)

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Step

#: The pull-request lane. It compares against the baseline main publishes and
#: must never publish, upload, or hold a credential.
PULL_REQUEST_LANE = ("ci.yml", "coverage")

#: The trunk publisher, which owns both the baseline and the CodeScene upload.
TRUNK_PUBLISHER = ("coverage-main.yml", "coverage-upload")

#: Workflows a pull request is known to run, directly or through a call.
#: Named rather than derived, so the traversal is asserted to reach them; the
#: clauses themselves read whatever the traversal finds, including anything
#: added later.
PULL_REQUEST_WORKFLOWS = frozenset({
    "build-wheels.yml",
    "ci.yml",
    "dependabot-automerge.yml",
    "rust-boundaries.yml",
})


#: The CodeScene action's `uses:` prefix. Matched as a prefix rather than a
#: substring so a step naming a differently owned action that happens to
#: contain the phrase cannot satisfy, or trip, the contract.
CODESCENE_ACTION = "leynos/shared-actions/.github/actions/upload-codescene-coverage@"

#: The one approved revision of the CodeScene action. From this revision the
#: committed `cli-manifest.json` is the trust anchor for the CLI archive, and
#: the action rejects a non-empty `installer-checksum` with a hard failure
#: rather than ignoring it.
#:
#: Asserted as an allowlist rather than as a floor. A floor would require
#: ordering two SHAs, which cannot be computed from a checkout, so naming the
#: approved revision is what keeps the contract hermetic; it fails closed on
#: any other value, including a tag or a branch name.
CODESCENE_ACTION_PIN = "a5765019912a8ab6882b12db049c7cde635f3a85"

#: Matches the revision of every CodeScene action reference in a workflow.
CODESCENE_ACTION_REFERENCE = re.compile(
    r"leynos/shared-actions/\.github/actions/upload-codescene-coverage@(\S+)"
)

#: The deprecated input. It took the SHA-256 of the installer script the action
#: used to download, and that download is gone.
DEPRECATED_INPUT = "installer-checksum"

#: The repository variable that fed the deprecated input. Its only consumer was
#: that input, so a reference to it now is dead weight or a returning mistake.
DEPRECATED_VARIABLE = "CODESCENE_CLI_SHA256"

#: The `workflow_dispatch` that hashed the installer script and wrote the
#: variable back through the API, named without an extension. Nothing reads
#: what it wrote, and the clause below keeps it from returning under either
#: GitHub extension.
REFRESH_WORKFLOW_STEM = "get-codescene-sha"

#: The extensions GitHub accepts for a workflow document.
WORKFLOW_EXTENSIONS = (".yml", ".yaml")


def _checkout(workflow_name: str, job_name: str) -> Step:
    """Return the single checkout step of a job.

    Parameters
    ----------
    workflow_name : str
        File name of the workflow.
    job_name : str
        Job whose checkout step is wanted.

    Returns
    -------
    Step
        The checkout step.
    """
    checkouts = [
        step
        for step in steps(workflow_name, job_name)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkouts) == 1, (
        f"{workflow_name}:{job_name} must check out exactly once, "
        f"found {len(checkouts)}"
    )
    return checkouts[0]


def _codescene_steps(workflow_name: str, job_name: str) -> list[Step]:
    """Return every step of a job that invokes the CodeScene action."""
    return [
        step
        for step in steps(workflow_name, job_name)
        if str(step.get("uses", "")).startswith(CODESCENE_ACTION)
    ]


def _pull_request_closure() -> dict[str, dict[object, object]]:
    """Return every workflow a pull request can run, calls included.

    Returns
    -------
    dict[str, dict[object, object]]
        Each reachable workflow's name mapped to its parsed document. The
        known members are asserted present, because a traversal that reached
        nothing would satisfy every prohibition below.
    """
    closure = reachable(PULL_REQUEST_EVENTS)
    missing = sorted(PULL_REQUEST_WORKFLOWS - closure.keys())
    assert not missing, (
        f"the pull-request closure must reach {missing}, or the clauses over "
        "it assert nothing about them"
    )
    return closure


def test_no_pull_request_workflow_contacts_codescene() -> None:
    """Nothing a pull request can run may name CodeScene, its host, or its client.

    The clause reads the closure through reusable-workflow calls, not the
    coverage job alone: a ``workflow_call`` workflow called from a pull-request
    job runs on that pull request, and a plain ``curl`` to the API names
    neither the shared action nor a step this contract used to read.
    """
    findings = contact_findings(_pull_request_closure())
    assert not findings, (
        f"pull-request workflows must not contact CodeScene: {findings}"
    )


def test_no_pull_request_workflow_can_read_the_codescene_token() -> None:
    """No scope a pull request can reach may name or forward the token.

    Every key and string value is read, so a ``run`` body, an action input, an
    ``env`` value at any of the three scopes, and a ``secrets:`` forwarding all
    count, as do ``secrets: inherit`` and ``toJSON(secrets)``, which hand over
    the token without naming it. A secret that fails to resolve is empty
    inside a step but not at the job or workflow scope, where an
    ``if: env.CS_ACCESS_TOKEN != ''`` guard would read a token and admit the
    step.
    """
    findings = token_findings(_pull_request_closure())
    assert not findings, (
        f"pull-request workflows must not reach the CodeScene token: {findings}"
    )


def test_the_upload_is_guarded_to_main_and_to_a_present_token() -> None:
    """The upload's guard must require both the main ref and the token.

    ``coverage-main.yml`` also answers ``workflow_dispatch``, which may start
    from any branch, so the trigger's ``branches: [main]`` filter does not
    cover a dispatch. The guard is split on ``&&`` and an unquoted ``||``
    refused, since an ``||`` hidden in an extra narrowing conjunct leaves both
    required conjuncts whole while making them optional.
    """
    workflow_name, job_name = TRUNK_PUBLISHER
    uploads = _codescene_steps(workflow_name, job_name)
    assert len(uploads) == 1, (
        f"{workflow_name}:{job_name} must upload to CodeScene exactly once"
    )
    guard = uploads[0].get("if")
    missing = missing_upload_conjuncts(guard)
    assert not missing, (
        f"{workflow_name}:{job_name} must guard its upload on {missing}, got {guard!r}"
    )


def test_the_trunk_publisher_uploads_to_codescene() -> None:
    """Main owns CodeScene publication, so it must ask for the upload mode.

    The input is load-bearing and silent when it is wrong. Dropping ``mode``
    falls back to the shared action's default, and ``check`` would switch the
    step to the pull-request comparison: either retires main-branch
    publication while every other contract in this module still passes.
    """
    workflow_name, job_name = TRUNK_PUBLISHER
    uploads = _codescene_steps(workflow_name, job_name)

    assert len(uploads) == 1, (
        f"{workflow_name}:{job_name} must upload to CodeScene exactly once, "
        f"found {len(uploads)}"
    )
    inputs = step_inputs(
        uploads[0],
        f"{workflow_name}:{job_name} CodeScene upload must declare inputs",
    )

    assert inputs.get("mode") == "upload", (
        f"{workflow_name}:{job_name} must publish with mode: upload, got "
        f"{inputs.get('mode')!r}; publication on main would stop without it"
    )


def test_the_pull_request_coverage_checkout_does_not_want_full_history() -> None:
    """Nothing in the pull-request lane reads history, so it must not fetch it.

    Full history was requested only so a CodeScene changed-line gate could
    resolve the merge base. That gate has moved to main, and a deep checkout is
    not an inert leftover: it slows every run and would let a history-reading
    gate be reintroduced without anyone noticing the depth to run it had come
    back.
    """
    workflow_name, job_name = PULL_REQUEST_LANE
    inputs = step_inputs(
        _checkout(workflow_name, job_name),
        f"{workflow_name}:{job_name} checkout must declare inputs",
    )

    assert "fetch-depth" not in inputs, (
        f"{workflow_name}:{job_name} requests fetch-depth="
        f"{inputs.get('fetch-depth')!r}; nothing in this lane reads history"
    )


def _sources() -> dict[str, str]:
    """Return every workflow's source text, keyed by file name.

    Returns
    -------
    dict[str, str]
        The workflow file name mapped to its UTF-8 source. The mapping is
        asserted to be non-empty, because every contract below would
        otherwise pass having read nothing.
    """
    sources = dict(workflow_sources())
    assert sources, (
        "no workflow was found, so every contract in this module would pass vacuously"
    )
    return sources


def test_no_workflow_passes_the_deprecated_installer_checksum() -> None:
    """The action rejects a non-empty value, so no workflow may pass it.

    This is not a tidy-up. At the pinned revision the action fails its own
    input validation when the value is non-empty, so a workflow that still
    passes the repository variable breaks the upload step the moment that
    variable holds anything.
    """
    offenders = sorted(
        name for name, source in _sources().items() if DEPRECATED_INPUT in source
    )
    assert not offenders, (
        f"{DEPRECATED_INPUT} is deprecated and rejected by the CodeScene "
        f"action at {CODESCENE_ACTION_PIN}; remove it from "
        f"{', '.join(offenders)}"
    )


def test_no_workflow_references_the_deprecated_checksum_variable() -> None:
    """The variable existed only to feed the rejected input, so it must go.

    Kept separate from the input contract because the two regress apart: an
    `env:` line or a guard can name the variable in a workflow that passes no
    input at all, and that reference is what a later reviewer would take as
    evidence the variable is still wanted.
    """
    offenders = sorted(
        name for name, source in _sources().items() if DEPRECATED_VARIABLE in source
    )
    assert not offenders, (
        f"{DEPRECATED_VARIABLE} fed {DEPRECATED_INPUT} and has no remaining "
        f"consumer; remove it from {', '.join(offenders)}"
    )


def test_every_codescene_action_reference_is_pinned_to_the_approved_revision() -> None:
    """One approved revision, so a stale pin cannot reintroduce the input.

    The set of references is checked for content before it is checked for
    compliance: deleting the upload step would otherwise satisfy this contract
    rather than fail it, and this repository is expected to publish coverage
    from main.

    Every match is retained as its own ``(workflow, revision)`` pair rather
    than collapsed into a mapping keyed by workflow. A mapping keeps only the
    last match per file, so one workflow holding a stale reference followed by
    an approved one would satisfy a contract whose whole claim is "every
    reference".
    """
    references = [
        (name, match.group(1))
        for name, source in _sources().items()
        for match in CODESCENE_ACTION_REFERENCE.finditer(source)
    ]
    assert references, (
        "no CodeScene action reference was found, so the pin assertion would "
        "pass vacuously; main is expected to publish coverage"
    )
    wrong = [(name, pin) for name, pin in references if pin != CODESCENE_ACTION_PIN]
    assert not wrong, (
        f"every CodeScene action reference must be pinned to "
        f"{CODESCENE_ACTION_PIN}; found {wrong}"
    )


def test_the_checksum_refresh_workflow_is_absent() -> None:
    """Nothing reads the variable it wrote, so the dispatch is dead code.

    Asserted against the filesystem rather than the parsed workflows, because
    a workflow that exists but is never triggered still appears in no step
    list, and its absence is the property that matters.

    Both extensions are checked. A real refresh workflow written as ``.yaml``
    would also fail the variable contract above, because it names the
    variable, but this clause must not lean on that: a placeholder of that
    name which references nothing is exactly the shape this clause exists to
    catch, and under one extension only it would have passed.
    """
    present = [
        f"{REFRESH_WORKFLOW_STEM}{extension}"
        for extension in WORKFLOW_EXTENSIONS
        if (WORKFLOW_DIR / f"{REFRESH_WORKFLOW_STEM}{extension}").exists()
    ]
    assert not present, (
        f"{present} refreshed {DEPRECATED_VARIABLE}, which no workflow reads "
        "any more; delete it rather than leaving a dispatch that maintains an "
        "unread repository variable"
    )
