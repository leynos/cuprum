"""Tests for the reader's guards: whether a job runs, and what it reads.

Split from `test_ci_placement_reader.py`, which reads runner *shapes*. These
cover the two questions asked alongside the shape: whether a job's own guard
lets it run at all, and which context properties a declaration reads, which is
what the job-name stability rule compares.

Both are driven with synthetic declarations rather than this repository's own
correct workflows, because a rule parametrized over correct sources passes
whether or not it discriminates.
"""

from __future__ import annotations

import pytest

from tests.helpers import ci_job_rules as rules
from tests.helpers import ci_placement as reader

OWNED = "ubicloud-standard-2"
HOSTED = "ubuntu-latest"
#: Every workflow the traversal must reach. Named rather than derived, so the
#: claim that `all_jobs` sees the whole estate is asserted rather than merely
#: true: a file the reader skipped would otherwise be invisible.
ESTATE_WORKFLOWS = frozenset({
    "benchmark-gate-harness.yml",
    "build-wheels.yml",
    "ci.yml",
    "coverage-main.yml",
    "delayed-pr-comment.yml",
    "dependabot-automerge.yml",
    "loom.yml",
    "mutation-testing.yml",
    "release.yml",
    "rust-boundaries.yml",
})


def _declare(monkeypatch: pytest.MonkeyPatch, declared: dict[str, object]) -> None:
    """Make both readers see one synthetic job declaration.

    The shape reader and the job rules each import `job` into their own
    namespace, so patching one would leave the other reading the repository's
    real workflows and the test would silently assert nothing.
    """
    monkeypatch.setattr(reader, "job", lambda *_: declared)
    monkeypatch.setattr(rules, "job", lambda *_: declared)


@pytest.mark.parametrize(
    "condition",
    [
        "false",
        "${{ false }}",
        "0",
        "0.0",
        "''",
        '""',
        "null",
        "false && matrix.target == 'x'",
        "null && matrix.target == 'x'",
        "0 && matrix.target == 'x'",
        # YAML types these before GitHub sees them: an unquoted `if: false`
        # arrives as a bool, and `if: 0` as a number.
        False,
        0,
        0.0,
    ],
)
def test_a_constant_false_guard_is_reported(
    monkeypatch: pytest.MonkeyPatch, condition: object
) -> None:
    """Catch a lane that satisfies every declaration rule and runs nothing."""
    _declare(monkeypatch, {"runs-on": HOSTED, "steps": [], "if": condition})
    assert rules.never_runs("w.yml", "j"), (
        f"{condition!r} can never be true, so the job gates nothing"
    )


@pytest.mark.parametrize(
    "condition",
    [
        "github.event_name == 'pull_request'",
        "needs.changes.outputs.bench == 'true'",
        "${{ !cancelled() }}",
        "matrix.python-suite",
        # GitHub treats a non-empty string as truthy, so a quoted `false` is a
        # job that runs. Refusing it would be a false positive on a lane that
        # executes, which is why it sits here rather than above.
        "'false'",
        '"false"',
        "falsey",
        "false_positive_guard",
        True,
        1,
    ],
)
def test_a_real_guard_is_not_reported_as_never_running(
    monkeypatch: pytest.MonkeyPatch, condition: object
) -> None:
    """Prove the rule narrow: a legitimate condition must still pass.

    Sufficiency alone is not enough. A guard rule that also refused the
    conditions this repository genuinely uses would be switched off, and a
    contract with a false positive gates nothing (femtologging #480).
    """
    _declare(monkeypatch, {"runs-on": HOSTED, "steps": [], "if": condition})
    assert not rules.never_runs("w.yml", "j"), (
        f"{condition!r} is a legitimate guard and must not be refused"
    )


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        pytest.param({"steps": []}, True, id="steps"),
        pytest.param({"uses": "./w.yml"}, False, id="caller"),
        pytest.param({"uses": "./w.yml", "steps": []}, False, id="both"),
        pytest.param({}, False, id="neither"),
    ],
)
def test_the_caller_exemption_is_keyed_on_uses(
    monkeypatch: pytest.MonkeyPatch, declared: dict[str, object], expected: bool
) -> None:
    """Exempt a reusable-workflow caller, and refuse a job with neither key.

    A blanket tolerance of a missing `runs-on` would exempt a malformed job
    too; keying on `uses` leaves that job refused (whitaker #438).
    """
    _declare(monkeypatch, declared)
    assert reader.declares_steps("w.yml", "j") is expected, (
        f"{sorted(declared)} must read as declares_steps={expected}"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("plain", frozenset(), id="no-reference"),
        pytest.param("${{ matrix.os }}", frozenset({"matrix.os"}), id="one"),
        pytest.param(
            "a ${{ matrix.a }} b ${{ matrix.b }}",
            frozenset({"matrix.a", "matrix.b"}),
            id="two",
        ),
        # The regression case. A body-level reader using `[^}]*` stops at the
        # `}` inside `{0}` and reports no reference at all, so a name built
        # this way shared nothing with a `runs-on` reading the same key and
        # the stability rule passed on an unstable name.
        pytest.param(
            "${{ format('{0}', matrix.os) }}",
            frozenset({"matrix.os"}),
            id="nested-function",
        ),
        pytest.param(
            "${{ format('{0}-{1}', matrix.os, matrix.arch) }}",
            frozenset({"matrix.os", "matrix.arch"}),
            id="nested-two-args",
        ),
        # A quoted label carrying a dot is a literal, not a property path.
        pytest.param(
            "${{ matrix.os == 'ubuntu-22.04' }}",
            frozenset({"matrix.os"}),
            id="quoted-dotted-literal",
        ),
        pytest.param(
            f"${{{{ {reader.FORK_FIELD} && 'a' || 'b' }}}}",
            frozenset({reader.FORK_FIELD}),
            id="fork-expression",
        ),
        pytest.param(None, frozenset(), id="absent"),
        pytest.param(True, frozenset(), id="non-string"),
    ],
)
def test_references_are_read_from_any_declaration(
    value: object, expected: frozenset[str]
) -> None:
    """Read every reference, so a name and a runner can be compared."""
    assert rules.references(value) == expected, (
        f"{value!r} reads {sorted(rules.references(value))}"
    )


def test_a_matrix_leg_missing_the_runner_key_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse an incomplete matrix rather than silently dropping a leg."""
    _declare(
        monkeypatch,
        {
            "runs-on": "${{ matrix.os }}",
            "steps": [],
            "strategy": {"matrix": {"include": [{"os": HOSTED}, {"arch": "x86_64"}]}},
        },
    )
    with pytest.raises(AssertionError, match="declares no 'os'"):
        reader.placement("w.yml", "j")


def test_all_jobs_reaches_every_workflow() -> None:
    """Name the traversal, or the claim that it is complete is unasserted.

    Falcon-pachinko #149: a `workflow_call` file with no caller is one line
    from gaining one, and a caller can live in another repository, so the
    registry counts its labels rather than excluding the file by rule.
    """
    reached = {workflow_name for workflow_name, _ in reader.all_jobs()}
    assert reached == ESTATE_WORKFLOWS, (
        f"the traversal must reach every workflow; reached {sorted(reached)}"
    )


def test_the_frozen_hosted_set_holds_no_paid_label() -> None:
    """Keep the exemption list free of the labels it exists to expose."""
    assert all(
        not label.startswith(("ubicloud-", "namespace-", "buildjet-", "warpbuild-"))
        for label in reader.FROZEN_HOSTED_LABELS
    ), "a paid provider's label must never be frozen out of the registry question"


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        pytest.param(True, "integer timeout-minutes", id="yaml-true"),
        pytest.param(False, "integer timeout-minutes", id="yaml-false"),
        pytest.param("45", "integer timeout-minutes", id="string"),
        pytest.param(45.0, "integer timeout-minutes", id="float"),
        pytest.param(None, "integer timeout-minutes", id="absent"),
        pytest.param(0, "positive timeout-minutes", id="zero"),
        pytest.param(-5, "positive timeout-minutes", id="negative"),
    ],
)
def test_an_unusable_ceiling_is_refused(
    monkeypatch: pytest.MonkeyPatch, declared: object, expected: str
) -> None:
    """Refuse the ceilings that bound nothing.

    Driven directly rather than over this repository's workflows, which all
    declare sensible values. Parametrized over correct sources the rule passes
    whether or not it discriminates, and a mutation from `type(...) is int`
    back to `isinstance` survived until this test existed: `True` is an `int`
    to Python, so `timeout-minutes: true` would have satisfied the contract
    while bounding the job at one minute.
    """
    _declare(monkeypatch, {"runs-on": HOSTED, "steps": [], "timeout-minutes": declared})
    with pytest.raises(AssertionError, match=expected):
        rules.ceiling("w.yml", "j")


def test_a_usable_ceiling_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove the rule narrow: an ordinary positive integer must pass."""
    _declare(monkeypatch, {"runs-on": HOSTED, "steps": [], "timeout-minutes": 45})
    assert rules.ceiling("w.yml", "j") == 45, "a positive integer is a valid ceiling"
