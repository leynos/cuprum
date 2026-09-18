"""Tests for the placement reader itself, driven by synthetic declarations.

The placement contracts in `tests/test_ci_runner_placement.py` are
parametrized over this repository's own workflows, which are correct. A rule
cannot be proved by the sources it guards: parametrized over correct files it
passes whether or not it discriminates, so the reader is driven directly here
with the shapes a workflow could acquire.

The shapes that must be *refused* matter as much as the ones that must be
read. An unparsable `runs-on` recorded as one opaque label carries no vendor
prefix, so the Ubicloud classifier drops the lane and it sits exempt from every
placement, ceiling and registry assertion while still asking for a paid runner
(axinite #372).
"""

from __future__ import annotations

import pytest

from tests.helpers import ci_placement as reader

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
OWNED = "ubicloud-standard-2"
HOSTED = "ubuntu-latest"
FORK_EXPRESSION = f"${{{{ {reader.FORK_FIELD} && '{HOSTED}' || '{OWNED}' }}}}"


def _declare(monkeypatch: pytest.MonkeyPatch, declared: dict[str, object]) -> None:
    """Make the reader see one synthetic job declaration."""
    monkeypatch.setattr(reader, "job", lambda *_: declared)


def test_a_bare_label_is_read_as_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the common case simple: one literal label, no arms, no references."""
    _declare(monkeypatch, {"runs-on": "windows-2022", "steps": []})
    placed = reader.placement("w.yml", "j")
    assert placed.kind == "literal", f"a bare label is a literal, got {placed.kind}"
    assert placed.owned == "windows-2022", f"owned arm was {placed.owned!r}"
    assert placed.fork is None, (
        f"a literal placement has no fork arm, got {placed.fork!r}"
    )
    assert placed.labels == frozenset({"windows-2022"}), (
        f"a literal resolves to one label, got {sorted(placed.labels)}"
    )
    assert placed.references == frozenset(), (
        f"a literal reads no expression, got {sorted(placed.references)}"
    )


def test_the_fork_expression_is_read_by_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report which arm is which, so a caller can assert them separately."""
    _declare(monkeypatch, {"runs-on": FORK_EXPRESSION, "steps": []})
    placed = reader.placement("w.yml", "j")
    assert placed.kind == "fork", f"the fallback is a fork placement, got {placed.kind}"
    assert placed.fork == HOSTED, f"fork arm was {placed.fork!r}"
    assert placed.owned == OWNED, f"owned arm was {placed.owned!r}"
    assert placed.labels == frozenset({HOSTED, OWNED}), (
        f"both arms are labels in use, got {sorted(placed.labels)}"
    )
    assert placed.references == frozenset({reader.FORK_FIELD}), (
        f"the condition must be the fork field, got {sorted(placed.references)}"
    )


def test_swapped_arms_are_reported_as_swapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discriminate the defect a membership test cannot see.

    Nile-valley #106's contract asserted that the expression named one hosted
    and one Ubicloud label somewhere. This declaration satisfies that and sends
    every fork to a runner no fork can obtain.
    """
    swapped = f"${{{{ {reader.FORK_FIELD} && '{OWNED}' || '{HOSTED}' }}}}"
    _declare(monkeypatch, {"runs-on": swapped, "steps": []})
    placed = reader.placement("w.yml", "j")
    assert placed.fork == OWNED, "the reader must report the arm as declared"
    assert placed.owned == HOSTED, "the owned arm here is the hosted label"


def test_a_sibling_field_is_reported_as_a_different_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Separate a well-formed expression from a correct one.

    `head.repo.private` produces an expression with the right shape and the
    right two arms that branches on the wrong thing. Only the condition itself
    distinguishes it, so the reader returns the condition rather than a
    boolean.
    """
    sibling = (
        "${{ github.event.pull_request.head.repo.private"
        f" && '{HOSTED}' || '{OWNED}' }}}}"
    )
    _declare(monkeypatch, {"runs-on": sibling, "steps": []})
    placed = reader.placement("w.yml", "j")
    assert placed.kind == "fork", f"the shape is still a fork arm, got {placed.kind}"
    assert placed.references != frozenset({reader.FORK_FIELD}), (
        "a sibling field must be distinguishable from the fork field"
    )


def test_a_matrix_placement_resolves_through_its_include(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Follow `runs-on` into the matrix values it selects, not merely to the key."""
    _declare(
        monkeypatch,
        {
            "runs-on": "${{ matrix.os }}",
            "steps": [],
            "strategy": {
                "matrix": {"include": [{"os": "macos-latest"}, {"os": HOSTED}]}
            },
        },
    )
    placed = reader.placement("w.yml", "j")
    assert placed.kind == "matrix", f"a matrix key placement, got {placed.kind}"
    assert placed.owned is None, (
        f"a matrix placement has no single owned label, got {placed.owned!r}"
    )
    assert placed.labels == frozenset({"macos-latest", HOSTED}), (
        f"every include entry is a label, got {sorted(placed.labels)}"
    )
    assert placed.references == frozenset({"matrix.os"}), (
        f"the placement reads the matrix key, got {sorted(placed.references)}"
    )


def test_a_matrix_value_holding_an_expression_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse the shape rstest-bdd #788's contract read straight through.

    A reader that stopped at `matrix.os` never saw that the value behind the
    key was itself the fork expression, so a name reading the fork field shared
    no reference with `runs-on` and the stability contract passed while the
    name rendered differently per event.
    """
    _declare(
        monkeypatch,
        {
            "runs-on": "${{ matrix.os }}",
            "steps": [],
            "strategy": {"matrix": {"include": [{"os": FORK_EXPRESSION}]}},
        },
    )
    with pytest.raises(AssertionError, match="cannot model"):
        reader.placement("w.yml", "j")


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        pytest.param(
            {"runs-on": ["self-hosted", "linux"], "steps": []},
            "list runs-on",
            id="list",
        ),
        pytest.param({"runs-on": [], "steps": []}, "list runs-on", id="empty-list"),
        pytest.param({"steps": []}, "non-empty string", id="absent"),
        pytest.param({"runs-on": "", "steps": []}, "non-empty string", id="blank"),
        pytest.param(
            {"runs-on": "${{ inputs.chosen-os }}", "steps": []},
            "cannot model",
            id="opaque-expression",
        ),
        pytest.param(
            {
                "runs-on": f"${{{{ {reader.FORK_FIELD}\n  && '{HOSTED}' }}}}",
                "steps": [],
            },
            "line break",
            id="folded-scalar",
        ),
    ],
)
def test_unmodelled_declarations_are_refused(
    monkeypatch: pytest.MonkeyPatch, declared: dict[str, object], expected: str
) -> None:
    """Fail on a shape the reader cannot model rather than record it as read."""
    _declare(monkeypatch, declared)
    with pytest.raises(AssertionError, match=expected):
        reader.placement("w.yml", "j")


def test_an_interpolated_label_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse a value that interpolates without being wholly an expression.

    `ubuntu-${{ matrix.release }}` is not a literal label, and recording it as
    one is the same defect as recording an opaque expression as one: the label
    it resolves to at run time is invisible to every placement and registry
    assertion, while the job still asks for a runner.
    """
    _declare(monkeypatch, {"runs-on": "ubuntu-${{ matrix.release }}", "steps": []})
    with pytest.raises(AssertionError, match="interpolates its runner label"):
        reader.placement("w.yml", "j")


def test_incidental_whitespace_around_an_expression_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep a correctly written lane readable when a scalar leaves a space.

    A folded scalar can leave a trailing space. Without tolerating it the whole
    declaration falls through to the literal branch, and a correct fork lane
    quietly stops being read as one, which is a false negative rather than a
    false positive and so is the harder kind to notice.
    """
    _declare(monkeypatch, {"runs-on": f"  {FORK_EXPRESSION} ", "steps": []})
    placed = reader.placement("w.yml", "j")
    assert placed.kind == "fork", (
        f"incidental whitespace must not change the shape, got {placed.kind}"
    )
    assert placed.owned == OWNED, f"owned arm was {placed.owned!r}"
    assert placed.fork == HOSTED, f"fork arm was {placed.fork!r}"


def test_the_refusal_of_an_opaque_expression_is_still_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail if the reader ever learns the shape the test above refuses.

    A refusal contract goes quiet the day the reader gains the ability it was
    written to compensate for, and a quiet contract reads exactly like a
    passing one. This makes that day loud: the refusal becomes redundant, not
    invisible, and both tests are then rewritten together.
    """
    _declare(monkeypatch, {"runs-on": "${{ inputs.chosen-os }}", "steps": []})
    with pytest.raises(AssertionError):
        reader.placement("w.yml", "j")


@pytest.mark.parametrize(
    "condition",
    ["false", "${{ false }}", "'false'", "false && matrix.target == 'x'", "0"],
)
def test_a_constant_false_guard_is_reported(
    monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    """Catch a lane that satisfies every declaration rule and runs nothing."""
    _declare(monkeypatch, {"runs-on": HOSTED, "steps": [], "if": condition})
    assert reader.never_runs("w.yml", "j"), (
        f"{condition!r} can never be true, so the job gates nothing"
    )


@pytest.mark.parametrize(
    "condition",
    [
        "github.event_name == 'pull_request'",
        "needs.changes.outputs.bench == 'true'",
        "${{ !cancelled() }}",
        "matrix.python-suite",
    ],
)
def test_a_real_guard_is_not_reported_as_never_running(
    monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    """Prove the rule narrow: a legitimate condition must still pass.

    Sufficiency alone is not enough. A guard rule that also refused the
    conditions this repository genuinely uses would be switched off, and a
    contract with a false positive gates nothing (femtologging #480).
    """
    _declare(monkeypatch, {"runs-on": HOSTED, "steps": [], "if": condition})
    assert not reader.never_runs("w.yml", "j"), (
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
        pytest.param(None, frozenset(), id="absent"),
        pytest.param(True, frozenset(), id="non-string"),
    ],
)
def test_references_are_read_from_any_declaration(
    value: object, expected: frozenset[str]
) -> None:
    """Read every reference, so a name and a runner can be compared."""
    assert reader.references(value) == expected, (
        f"{value!r} reads {sorted(reader.references(value))}"
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
