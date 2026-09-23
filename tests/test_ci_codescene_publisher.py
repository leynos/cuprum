"""Contracts on how the trunk publisher holds and spends the CodeScene token.

The boundary contracts keep the token away from pull requests. These cover the
one job that does hold it, ``coverage-main.yml``'s ``coverage-upload``:

* the token is checked for by a step that reads the secret without placing it
  in any ``env``, because the composite upload action passes a step's ``env``
  on to every step nested inside it;
* the upload runs only when that check says the token exists and the ref is
  ``main``, split on ``&&`` with any unquoted ``||`` refused;
* the token reaches the upload as its ``access-token`` input and through no
  ``env`` at the workflow, job or step scope; and
* the publisher's runs share one slot per ref and are never cancelled.

The developers' guide records two consequences a reader cannot see from the
workflow, and the last clause holds it to them.
"""

from __future__ import annotations

import typing as typ

from tests.helpers.ci_codescene import (
    CREDENTIAL_CHECK_COMMAND,
    CREDENTIAL_CHECK_ID,
    missing_upload_conjuncts,
    token_findings,
)
from tests.helpers.ci_runners import steps, workflow_document
from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Step

TRUNK_PUBLISHER = ("coverage-main.yml", "coverage-upload")

#: The upload step, found by its action rather than its name.
CODESCENE_ACTION = "leynos/shared-actions/.github/actions/upload-codescene-coverage@"

#: The only value the upload's `access-token` input may take.
DIRECT_CREDENTIAL = "${{ secrets.CS_ACCESS_TOKEN }}"

#: The publisher's concurrency, asserted exactly. The group is the ref alone
#: under the workflow's own prefix, so triggered runs on `main` share one slot:
#: the running one is never cancelled, and the newest trigger takes the
#: pending slot.
PUBLISHER_CONCURRENCY = {
    "group": "coverage-main-${{ github.ref }}",
    "cancel-in-progress": False,
}

#: Sentences the developers' guide must carry, each a consequence of the
#: publisher's triggers and concurrency that the workflow cannot show.
GUIDE_FACTS = (
    "Dependabot's automerge fires no push",
    "a dispatch that replaces a pending push leaves the baseline one commit behind",
)


def _normalized(value: object) -> str:
    """Collapse runs of whitespace so formatting cannot decide a comparison."""
    return " ".join(str(value).split())


def _single(matches: list[Step], what: str) -> Step:
    """Return the one step in ``matches``, failing with ``what`` otherwise."""
    assert len(matches) == 1, f"{what}: expected exactly one, found {len(matches)}"
    return matches[0]


def _upload() -> Step:
    """Return the publisher's one CodeScene upload step."""
    workflow_name, job_name = TRUNK_PUBLISHER
    return _single(
        [
            step
            for step in steps(workflow_name, job_name)
            if str(step.get("uses", "")).startswith(CODESCENE_ACTION)
        ],
        f"{workflow_name}:{job_name} CodeScene upload",
    )


def _token_check() -> Step:
    """Return the publisher's one token-check step."""
    workflow_name, job_name = TRUNK_PUBLISHER
    return _single(
        [
            step
            for step in steps(workflow_name, job_name)
            if step.get("id") == CREDENTIAL_CHECK_ID
        ],
        f"{workflow_name}:{job_name} step with id {CREDENTIAL_CHECK_ID!r}",
    )


def test_the_token_check_reads_the_secret_and_nothing_else() -> None:
    """The check's whole command is the one line, with no guard and no ``env``.

    A guard could skip it and leave the upload's condition reading an empty
    output; an ``env`` would put the token back where the action leaks it.
    """
    check = _token_check()
    assert _normalized(check.get("run")) == CREDENTIAL_CHECK_COMMAND, (
        f"the token check must run exactly {CREDENTIAL_CHECK_COMMAND!r}, got "
        f"{check.get('run')!r}"
    )
    assert "if" not in check, f"the token check must not be guarded: {check['if']!r}"
    assert "env" not in check, "the token check must declare no env"


def test_the_token_is_checked_before_the_upload_reads_the_answer() -> None:
    """An upload that runs first reads an empty output and skips in silence."""
    workflow_name, job_name = TRUNK_PUBLISHER
    job_steps = steps(workflow_name, job_name)
    check = job_steps.index(_token_check())
    upload = job_steps.index(_upload())
    assert check < upload, (
        f"the token check (step {check}) must precede the upload (step {upload})"
    )


def test_the_upload_is_guarded_to_main_and_to_a_present_token() -> None:
    """The upload's guard must require both the check's answer and the main ref.

    ``coverage-main.yml`` also answers ``workflow_dispatch``, which may start
    from any branch, so the trigger's ``branches: [main]`` filter does not
    cover a dispatch. The guard is split on ``&&`` and an unquoted ``||``
    refused, since an ``||`` hidden in an extra narrowing conjunct leaves both
    required conjuncts whole while making them optional.
    """
    guard = _upload().get("if")
    missing = missing_upload_conjuncts(guard)
    assert not missing, f"the upload must be guarded on {missing}, got {guard!r}"


def test_the_token_reaches_the_upload_as_its_input_and_through_no_env() -> None:
    """The positive half and the prohibition, asserted together.

    Prohibiting ``env`` alone would let the token vanish from the upload, which
    then runs with no credential and fails; requiring the input alone would let
    a leftover ``env`` keep leaking it into the action's nested steps.
    """
    workflow_name, job_name = TRUNK_PUBLISHER
    upload = _upload()
    inputs = upload.get("with")
    assert isinstance(inputs, dict), "the upload must declare inputs"
    token = _normalized(inputs.get("access-token"))
    assert token == DIRECT_CREDENTIAL, (
        f"the upload must take access-token {DIRECT_CREDENTIAL!r}, got {token!r}"
    )
    document = workflow_document(workflow_name)
    job = typ.cast("dict[str, object]", document["jobs"])[job_name]
    scopes = {
        f"{workflow_name} env": document.get("env"),
        f"{workflow_name}:{job_name} env": typ.cast("dict[str, object]", job).get(
            "env"
        ),
        **{
            f"{workflow_name}:{job_name} step {index} env": step.get("env")
            for index, step in enumerate(steps(workflow_name, job_name))
        },
    }
    findings = token_findings({name: env for name, env in scopes.items() if env})
    assert not findings, f"the token must reach no env scope: {findings}"


def test_the_publisher_shares_one_slot_per_ref_and_never_cancels() -> None:
    """A cancelled publisher abandons its upload and its baseline write."""
    concurrency = workflow_document("coverage-main.yml").get("concurrency")
    assert concurrency == PUBLISHER_CONCURRENCY, (
        f"coverage-main.yml must declare concurrency {PUBLISHER_CONCURRENCY}, "
        f"got {concurrency!r}"
    )


def test_the_guide_states_what_the_publisher_cannot_show() -> None:
    """The guide must say why dispatches exist and what one can cost."""
    guide = _normalized(
        (repo_root() / "docs" / "developers-guide.md").read_text(encoding="utf-8")
    )
    missing = [fact for fact in GUIDE_FACTS if fact not in guide]
    assert not missing, f"the developers' guide must state: {missing}"
