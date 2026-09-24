"""Edge cases of the pull-request closure and of the strict readers.

Split from `tests/test_ci_codescene_closure.py`, which proves the readers'
ordinary shapes. These cover the routes the first review found the closure
did not follow: a chain of calls deeper than one hop, a downstream
`workflow_run` workflow, the `$/` spelling, and a call to this repository by
its qualified name. They also prove that every workflow and action reader,
not only the shared loader, refuses a duplicated key.
"""

from __future__ import annotations

import textwrap
import typing as typ

import pytest

from tests.helpers import composite_actions
from tests.helpers.ci_closure import PULL_REQUEST_EVENTS, local_calls, reachable
from tests.helpers.ci_codescene import contact_findings, token_findings
from tests.helpers.workflow import parse_workflow

if typ.TYPE_CHECKING:
    from pathlib import Path

#: The workflow at the end of every chain below: it curls CodeScene with the
#: token, so reaching it is observable as findings.
PROBE = """\
on: workflow_call
jobs:
  curl:
    runs-on: ubuntu-latest
    steps:
      - run: curl ${{ secrets.CS_ACCESS_TOKEN }} https://api.codescene.io/
"""


def _write(directory: Path, **documents: str) -> Path:
    """Write named workflow documents into ``directory`` and return it."""
    for stem, text in documents.items():
        (directory / f"{stem}.yml").write_text(textwrap.dedent(text), encoding="utf-8")
    return directory


def test_a_chain_of_calls_is_followed_to_its_end(tmp_path: Path) -> None:
    """Caller, intermediate, callee: a one-hop traversal would stop halfway."""
    directory = _write(
        tmp_path,
        ci="on: pull_request\njobs:\n  a:\n    uses: ./.github/workflows/middle.yml\n",
        middle=(
            "on: workflow_call\njobs:\n  b:\n    uses: ./.github/workflows/probe.yml\n"
        ),
        probe=PROBE,
    )
    closure = reachable(PULL_REQUEST_EVENTS, directory)
    assert sorted(closure) == ["ci.yml", "middle.yml", "probe.yml"], sorted(closure)
    tokens = token_findings(closure)
    assert any(finding.startswith("probe.yml") for finding in tokens), tokens
    contacts = contact_findings(closure)
    assert any(finding.startswith("probe.yml") for finding in contacts), contacts


def test_a_downstream_workflow_run_is_reached(tmp_path: Path) -> None:
    """A run triggered by a pull-request workflow's completion holds secrets."""
    directory = _write(
        tmp_path,
        ci="name: CI\non: pull_request\njobs: {}\n",
        after=(
            "on:\n  workflow_run:\n    workflows: [CI]\n    types: [completed]\n"
            "jobs:\n  a:\n    uses: ./.github/workflows/probe.yml\n"
        ),
        probe=PROBE,
        unrelated="on:\n  workflow_run:\n    workflows: [Nightly]\njobs: {}\n",
    )
    closure = sorted(reachable(PULL_REQUEST_EVENTS, directory))
    assert closure == ["after.yml", "ci.yml", "probe.yml"], closure


def test_a_workflow_run_without_a_workflow_list_is_refused(tmp_path: Path) -> None:
    """An unreadable watch list fails rather than reading as watching nothing."""
    directory = _write(
        tmp_path,
        ci="name: CI\non: pull_request\njobs: {}\n",
        after="on:\n  workflow_run: {}\njobs: {}\n",
    )
    with pytest.raises(AssertionError, match="readable workflows list"):
        reachable(PULL_REQUEST_EVENTS, directory)


def test_the_dollar_spelling_is_a_local_call() -> None:
    """``$/.github/workflows/x.yml`` is read as local by shape."""
    called = local_calls(
        {"jobs": {"j": {"uses": "$/.github/workflows/w.yml"}}}, "ci.yml"
    )
    assert called == {"w.yml"}, f"the $/ spelling must read as w.yml, got {called}"


@pytest.mark.parametrize(
    "reference",
    [
        "leynos/cuprum/.github/workflows/probe.yml@main",
        "Leynos/Cuprum/.github/workflows/probe.yml@0123456789abcdef",
    ],
)
def test_a_self_qualified_call_is_refused(reference: str) -> None:
    """A call to this repository at an unreadable revision fails closed."""
    with pytest.raises(AssertionError, match="qualified name"):
        local_calls({"jobs": {"j": {"uses": reference}}}, "ci.yml")


def test_the_workflow_model_reader_refuses_a_duplicate_key() -> None:
    """`parse_workflow` feeds the benchmark-gate contracts; it must refuse too."""
    source = "on: push\njobs:\n  j:\n    runs-on: a\n    runs-on: b\n"
    with pytest.raises(AssertionError, match="duplicate key 'runs-on'"):
        parse_workflow(source)


def test_the_action_reader_refuses_a_duplicate_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`action_document` reads composite actions the shell tests execute."""
    action = tmp_path / "act"
    action.mkdir()
    (action / "action.yml").write_text(
        "runs:\n  using: composite\n  using: node20\n", encoding="utf-8"
    )
    monkeypatch.setattr(composite_actions, "ROOT", tmp_path)
    with pytest.raises(AssertionError, match=r"(?s)act/action\.yml.*duplicate key"):
        composite_actions.action_document("act")
