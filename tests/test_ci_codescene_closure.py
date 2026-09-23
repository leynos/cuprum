"""Tests for the readers behind the CodeScene boundary contracts.

`tests/test_ci_codescene_boundary.py` runs over this repository's workflows,
which are correct, so it passes whether or not its readers discriminate. These
tests drive the readers with constructed workflows instead: the closure through
reusable-workflow calls, the token and contact walks, the upload-guard split,
the trigger reader, and the duplicate-key refusal.
"""

from __future__ import annotations

import textwrap
import typing as typ

import pytest

from tests.helpers.ci_closure import (
    PULL_REQUEST_EVENTS,
    local_calls,
    reachable,
    triggers,
)
from tests.helpers.ci_codescene import (
    contact_findings,
    guard_conjuncts,
    missing_upload_conjuncts,
    token_findings,
)
from tests.helpers.ci_workflows import read_workflow
from tests.helpers.strict_yaml import load

if typ.TYPE_CHECKING:
    from pathlib import Path

#: A pull-request workflow calling a local reusable workflow with every secret.
CALLER = """\
on: pull_request
jobs:
  probe:
    uses: ./.github/workflows/probe.yml
    secrets: inherit
"""
#: The episodic probe: a `workflow_call`-only workflow that curls CodeScene's
#: API with the token its caller handed it.
PROBE = """\
on: workflow_call
jobs:
  curl:
    runs-on: ubuntu-latest
    steps:
      - run: |
          curl -H "Authorization: ${{ secrets.CS_ACCESS_TOKEN }}" \\
            https://api.codescene.io/v2/projects
"""


def _workflows(directory: Path, **documents: str) -> Path:
    """Write named workflow documents into ``directory`` and return it."""
    for stem, text in documents.items():
        (directory / f"{stem}.yml").write_text(textwrap.dedent(text), encoding="utf-8")
    return directory


def test_the_closure_reaches_a_called_workflow_and_its_findings(tmp_path: Path) -> None:
    """A called ``workflow_call`` workflow is reached, with its token and contact."""
    closure = reachable(
        PULL_REQUEST_EVENTS, _workflows(tmp_path, ci=CALLER, probe=PROBE)
    )

    assert sorted(closure) == ["ci.yml", "probe.yml"], sorted(closure)
    tokens = token_findings(closure)
    assert any(finding.startswith("probe.yml") for finding in tokens), tokens
    assert any(finding.startswith("ci.yml:probe") for finding in tokens), tokens
    contacts = contact_findings(closure)
    assert any(finding.startswith("probe.yml") for finding in contacts), contacts


def test_a_workflow_no_pull_request_reaches_stays_outside(tmp_path: Path) -> None:
    """The closure is not every workflow: an unreached dispatch-only one is out."""
    directory = _workflows(tmp_path, ci="on: pull_request\njobs: {}\n", probe=PROBE)
    (directory / "manual.yml").write_text(
        "on: workflow_dispatch\njobs: {}\n", encoding="utf-8"
    )

    reached = sorted(reachable(PULL_REQUEST_EVENTS, directory))
    assert reached == ["ci.yml"], f"only ci.yml is reachable, got {reached}"


def test_pull_request_target_starts_the_closure(tmp_path: Path) -> None:
    """``pull_request_target`` runs with the base repository's secrets."""
    directory = _workflows(tmp_path, auto="on: {pull_request_target: {}}\njobs: {}\n")

    reached = sorted(reachable(PULL_REQUEST_EVENTS, directory))
    assert reached == ["auto.yml"], f"auto.yml must be reached, got {reached}"


def test_an_upper_case_extension_is_still_a_workflow(tmp_path: Path) -> None:
    """GitHub reads ``.YAML`` too, so the enumeration must not skip it."""
    (tmp_path / "ci.YAML").write_text(CALLER, encoding="utf-8")
    (tmp_path / "probe.yml").write_text(PROBE, encoding="utf-8")

    reached = sorted(reachable(PULL_REQUEST_EVENTS, tmp_path))
    assert reached == ["ci.YAML", "probe.yml"], f"both must be reached, got {reached}"


@pytest.mark.parametrize(
    "reference",
    [
        "./.github/workflows/w.yml",
        ".github/workflows/w.yml",
        " ./.github/workflows/w.yml ",
    ],
)
def test_a_local_call_is_matched_by_shape(reference: str) -> None:
    """Any spelling that resolves under the workflow directory is a local call."""
    called = local_calls({"jobs": {"j": {"uses": reference}}}, "ci.yml")
    assert called == {"w.yml"}, f"{reference!r} must read as w.yml, got {called}"


def test_a_remote_reusable_workflow_is_not_local() -> None:
    """Another repository's workflow is not followed; its path merely looks alike."""
    reference = "leynos/shared-actions/.github/workflows/w.yml@0123456789abcdef"
    called = local_calls({"jobs": {"j": {"uses": reference}}}, "ci.yml")
    assert not called, f"a remote workflow must not be followed, got {called}"


@pytest.mark.parametrize(
    "reference", ["./.github/workflows/nested/w.yml", "./.github/workflows/w.yml@main"]
)
def test_an_unreadable_local_call_is_refused(reference: str) -> None:
    """A local-shaped call GitHub would not accept is refused, not skipped."""
    with pytest.raises(AssertionError, match="unreadable local workflow"):
        local_calls({"jobs": {"j": {"uses": reference}}}, "ci.yml")


def test_a_call_to_a_missing_workflow_is_refused(tmp_path: Path) -> None:
    """A callee that does not exist fails the traversal rather than shrinking it."""
    with pytest.raises(
        AssertionError, match=r"calls probe\.yml, which is not a workflow"
    ):
        reachable(PULL_REQUEST_EVENTS, _workflows(tmp_path, ci=CALLER))


@pytest.mark.parametrize(
    "workflow",
    [
        {"jobs": {"j": {"steps": [{"run": "echo ${{ secrets.CS_ACCESS_TOKEN }}"}]}}},
        {
            "jobs": {
                "j": {
                    "steps": [
                        {
                            "uses": "a/b@c",
                            "with": {"t": "${{ secrets.CS_ACCESS_TOKEN }}"},
                        }
                    ]
                }
            }
        },
        {"jobs": {"j": {"steps": [{"env": {"T": "${{ secrets.CS_ACCESS_TOKEN }}"}}]}}},
        {"jobs": {"j": {"env": {"CS_ACCESS_TOKEN": "x"}}}},
        {"env": {"CS_ACCESS_TOKEN": "x"}},
        {
            "jobs": {
                "j": {
                    "uses": "a/b/.github/workflows/w.yml@c",
                    "secrets": {"CS_ACCESS_TOKEN": "x"},
                }
            }
        },
        {
            "jobs": {
                "j": {"uses": "a/b/.github/workflows/w.yml@c", "secrets": "inherit"}
            }
        },
        {"jobs": {"j": {"steps": [{"run": "echo '${{ toJSON(secrets) }}'"}]}}},
        {"jobs": {"j": {"steps": [{"run": "echo ${{ secrets.cs_access_token }}"}]}}},
        {
            "jobs": {
                "j": {"steps": [{"if": "env.CS_ACCESS_TOKEN != ''", "run": "true"}]}
            }
        },
    ],
    ids=[
        "run-body",
        "action-input",
        "step-env-value",
        "job-env-key",
        "workflow-env-key",
        "named-forwarding",
        "inherit",
        "all-secrets",
        "lower-case",
        "guard",
    ],
)
def test_every_route_to_the_token_is_found(workflow: dict[object, object]) -> None:
    """Each place a token can reach a step is a finding, whatever the scope."""
    assert token_findings({"x.yml": workflow}), f"missed the token in {workflow}"


def test_an_unrelated_secret_is_not_a_token_finding() -> None:
    """The walk looks for this token, not for secrets in general."""
    workflow = {"jobs": {"j": {"steps": [{"run": "echo ${{ secrets.GITHUB_TOKEN }}"}]}}}
    findings = token_findings({"x.yml": workflow})
    assert not findings, f"GITHUB_TOKEN is not the CodeScene token: {findings}"


@pytest.mark.parametrize(
    "step",
    [
        {"run": "curl https://api.codescene.io/v2/projects"},
        {"run": "cs-coverage check"},
        {"uses": "leynos/shared-actions/.github/actions/upload-codescene-coverage@abc"},
    ],
    ids=["host", "client", "action"],
)
def test_every_route_to_codescene_is_found(step: dict[str, str]) -> None:
    """The host, the client, and the shared action are each a contact."""
    findings = contact_findings({"x.yml": {"jobs": {"j": {"steps": [step]}}}})
    assert findings, f"missed the contact in {step}"


def test_a_comment_is_not_a_contact() -> None:
    """The walk reads the parsed document, so a comment cannot trip it."""
    document = load("# codescene.io is main's business\njobs: {}\n", "x.yml")
    findings = contact_findings({"x.yml": typ.cast("dict[object, object]", document)})
    assert not findings, f"a comment must not be a finding: {findings}"


def test_an_alternative_hidden_in_an_extra_conjunct_is_refused() -> None:
    """Only the ``||`` refusal catches an escape that keeps both conjuncts whole.

    Splitting on ``&&`` leaves the token and ref conjuncts intact here, so a
    subset check passes, and the trailing alternative uploads from every
    dispatched branch. This is the case that proves the refusal is needed.
    """
    guard = (
        "env.CS_ACCESS_TOKEN != '' && github.ref == 'refs/heads/main' "
        "&& github.actor != 'x' || github.event_name == 'workflow_dispatch'"
    )
    with pytest.raises(AssertionError, match=r"must not contain \|\|"):
        missing_upload_conjuncts(guard)


def test_an_appended_alternative_is_not_accepted() -> None:
    """An ``||`` appended to the ref breaks that conjunct, so it fails either way.

    This case cannot prove the refusal: with the refusal deleted, the ref
    conjunct is no longer whole and is reported missing. It is kept so the
    contract's behaviour on the obvious escape is pinned all the same.
    """
    guard = (
        "env.CS_ACCESS_TOKEN != '' && github.ref == 'refs/heads/main' "
        "|| github.event_name == 'workflow_dispatch'"
    )
    try:
        missing = missing_upload_conjuncts(guard)
    except AssertionError:
        return
    assert missing, f"{guard!r} must not satisfy the upload guard"


def test_a_quoted_bar_pair_is_not_an_alternative() -> None:
    """``||`` inside a literal is data, not an operator."""
    conjuncts = guard_conjuncts("${{ a == 'x||y' &&  b }}")
    assert conjuncts == {"a == 'x||y'", "b"}, f"got {sorted(conjuncts)}"


def test_a_missing_guard_is_refused() -> None:
    """An upload with no guard at all is not an empty set of conjuncts."""
    with pytest.raises(AssertionError, match="must be an expression"):
        guard_conjuncts(None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("on: push\n", {"push"}),
        ("on: [push, pull_request]\n", {"push", "pull_request"}),
        ("on:\n  push:\n  pull_request_target:\n", {"push", "pull_request_target"}),
        ('"on": pull_request\n', {"pull_request"}),
    ],
    ids=["scalar", "sequence", "mapping", "string-key"],
)
def test_every_trigger_form_is_read(text: str, expected: set[str]) -> None:
    """Scalar, sequence and mapping, under the boolean key and the string one."""
    document = typ.cast("dict[object, object]", load(text, "x.yml"))
    read = triggers(document, "x.yml")
    assert read == expected, f"{text!r} must read as {expected}, got {read}"


@pytest.mark.parametrize(
    "document",
    [{}, {True: 5}, {True: []}, {True: ["push", 3]}, {True: "push", "on": "push"}],
    ids=["absent", "number", "empty", "non-string-event", "both-keys"],
)
def test_an_unreadable_trigger_is_refused(document: dict[object, object]) -> None:
    """A trigger the reader cannot read fails rather than reading as no events."""
    with pytest.raises(AssertionError, match="trigger"):
        triggers(document, "x.yml")


def test_a_duplicated_runs_on_is_refused(tmp_path: Path) -> None:
    """PyYAML would keep the last ``runs-on``; the strict loader names the file."""
    workflow = tmp_path / "twice.yml"
    workflow.write_text(
        "on: push\njobs:\n  j:\n"
        "    runs-on: ubicloud-standard-2\n"
        "    runs-on: ubuntu-latest\n",
        encoding="utf-8",
    )
    with pytest.raises(
        AssertionError, match=r"(?s)twice\.yml .*duplicate key 'runs-on'"
    ):
        read_workflow(workflow)


def test_on_and_true_collide_as_one_key() -> None:
    """``on`` and ``true`` both parse to ``True``, so declaring both is a duplicate."""
    with pytest.raises(AssertionError, match="duplicate key True"):
        load("on: push\ntrue: pull_request\n", "x.yml")


def test_a_merge_key_may_be_overridden() -> None:
    """A merge supplies defaults the mapping overrides; that is not a duplicate."""
    text = "base: &base {a: 1, b: 2}\nderived:\n  <<: *base\n  b: 3\n"
    parsed = typ.cast("dict[str, object]", load(text, "x.yml"))
    assert parsed["derived"] == {"a": 1, "b": 3}, f"got {parsed['derived']!r}"
