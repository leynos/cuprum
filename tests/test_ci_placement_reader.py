"""Tests for the placement reader itself, driven by synthetic declarations.

The placement contracts in `tests/test_ci_runner_placement.py` are
parametrized over this repository's own workflows, which are correct. A rule
cannot be proved by the sources it guards: parametrized over correct files it
passes whether or not it discriminates, so the reader is driven directly here
with the shapes a workflow could acquire.

The shapes that must be *refused* matter as much as the ones that must be
read. An unparsable `runs-on` recorded as one opaque label carries no vendor
prefix, so the Ubicloud classifier drops the lane and it sits exempt from every
placement, ceiling, and registry assertion while still asking for a paid runner
(axinite #372).
"""

from __future__ import annotations

import pytest

from tests.helpers import ci_placement as reader

#: Every workflow the traversal must reach. Named rather than derived, so the
#: claim that `all_jobs` sees the whole estate is asserted rather than merely
#: true: a file the reader skipped would otherwise be invisible.
ESTATE_WORKFLOWS = frozenset({
    "build-wheels.yml",
    "ci.yml",
    "coverage-main.yml",
    "delayed-pr-comment.yml",
    "dependabot-automerge.yml",
    "get-codescene-sha.yml",
    "mutation-testing.yml",
    "release.yml",
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


def test_a_hyphenated_condition_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read a condition whose property path carries a hyphen.

    `matrix.python-version` is a legitimate condition, and a character class
    without `-` refused the whole expression as unmodellable. The refusal
    failed safe, so nothing went unchecked, but it would have rejected a
    correct lane. Found by the generated property in
    `test_ci_placement_properties.py` rather than by any example.
    """
    hyphenated = f"${{{{ matrix.python-version && '{HOSTED}' || '{OWNED}' }}}}"
    _declare(monkeypatch, {"runs-on": hyphenated, "steps": []})
    placed = reader.placement("w.yml", "j")
    assert placed.kind == "fork", f"read as {placed.kind}"
    assert placed.references == frozenset({"matrix.python-version"}), (
        f"condition read as {sorted(placed.references)}"
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


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param(f" {FORK_EXPRESSION}", id="leading-space"),
        pytest.param(f"{FORK_EXPRESSION} ", id="trailing-space"),
        pytest.param(" ${{ inputs.chosen-os }}", id="padded-opaque"),
    ],
)
def test_a_padded_expression_is_refused(
    monkeypatch: pytest.MonkeyPatch, declared: str
) -> None:
    """Refuse a padded expression rather than reading it as the bare one.

    GitHub interpolates into the surrounding string and keeps what is around
    the expression, so `" ${{ ... }}"` resolves to a label with a leading space
    that matches no runner. Reading it as the unpadded expression would report
    a broken lane as a correct fork placement, which is the dangerous
    direction: the contract would go green on a job that can never be
    scheduled.
    """
    _declare(monkeypatch, {"runs-on": declared, "steps": []})
    with pytest.raises(AssertionError, match="interpolates its runner label"):
        reader.placement("w.yml", "j")


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
