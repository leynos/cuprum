"""Tests for the cache-family resolver itself.

`tests/test_ci_cache_ownership.py` uses this resolver to hold the one-writer
invariant, so a resolver that quietly misread an expression or dropped a
matrix leg would weaken that contract without failing it. These tests exercise
the resolver directly, against synthetic workflow shapes rather than the
repository's own, so a future workflow that is merely unusual is caught here
rather than silently resolved wrongly there.
"""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.helpers import ci_cache_families as families

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Step

if typ.TYPE_CHECKING:
    import collections.abc as cabc

MESSAGE = "synthetic job"
#: Hypothesis's default deadline trips on a loaded developer machine for work
#: this trivial, and a flaky contract test is worse than a slow one.
SETTINGS = settings(deadline=None, max_examples=100)
#: Names a workflow author could plausibly give a matrix key.
MATRIX_NAMES = st.from_regex(r"\A[a-z][a-z0-9-]{0,20}\Z")
#: Values that are not GitHub expressions, so `_resolve` must pass them through.
LITERALS = st.text(min_size=1, max_size=30).filter(lambda s: "${{" not in s)


@SETTINGS
@given(literal=LITERALS, leg=st.dictionaries(MATRIX_NAMES, st.integers(), max_size=3))
def test_a_literal_input_resolves_to_itself(
    literal: str, leg: cabc.Mapping[str, object]
) -> None:
    """Pass a plain value through untouched, whatever the leg holds."""
    assert families._resolve(literal, leg, MESSAGE) == literal, (
        f"a value with no expression must pass through; {literal!r} did not"
    )


@SETTINGS
@given(name=MATRIX_NAMES, value=st.integers() | st.text(max_size=10))
def test_a_matrix_reference_resolves_from_the_leg(name: str, value: object) -> None:
    """Render `${{ matrix.x }}` from the leg that supplies `x`."""
    resolved = families._resolve("${{ matrix." + name + " }}", {name: value}, MESSAGE)
    assert resolved == str(value), (
        f"matrix.{name} must render from the leg, which holds {value!r}"
    )


@SETTINGS
@given(name=MATRIX_NAMES)
def test_a_matrix_reference_the_leg_lacks_is_a_contract_failure(name: str) -> None:
    """Refuse to guess. A key the leg never declares is a workflow bug."""
    with pytest.raises(AssertionError, match="matrix leg declares no"):
        families._resolve("${{ matrix." + name + " }}", {}, MESSAGE)


def test_a_non_matrix_expression_is_a_contract_failure() -> None:
    """Reject an expression this reader cannot evaluate rather than guess at it."""
    with pytest.raises(AssertionError, match="cannot resolve"):
        families._resolve("${{ env.SOMETHING }}", {}, MESSAGE)


@SETTINGS
@given(
    flags=st.dictionaries(MATRIX_NAMES, st.booleans(), min_size=1, max_size=4),
)
def test_a_condition_admits_a_leg_when_every_named_flag_is_true(
    flags: cabc.Mapping[str, bool],
) -> None:
    """Honour a save condition that names matrix values, and only those."""
    condition = " && ".join(f"matrix.{name}" for name in flags)
    step = typ.cast("Step", {"if": condition})
    admitted = families._writes_on_leg(step, flags)
    assert admitted == all(flags.values()), (
        f"condition {condition!r} against {dict(flags)} must admit the leg only "
        "when every flag it names is true"
    )


@pytest.mark.parametrize(
    "condition",
    [
        None,
        "github.event_name == 'push'",
        "github.event_name == 'push' && github.ref == 'refs/heads/main'",
    ],
)
def test_a_condition_naming_no_matrix_value_admits_every_leg(
    condition: str | None,
) -> None:
    """Model matrix references only; run-time values are not this test's job."""
    step = typ.cast("Step", {"if": condition} if condition is not None else {})
    assert families._writes_on_leg(step, {"python-suite": False}), (
        f"condition {condition!r} names no matrix value, so it must admit every leg"
    )


def test_a_job_without_a_matrix_expands_to_one_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Let callers iterate legs uniformly whether or not a matrix exists."""
    monkeypatch.setattr(families, "job", lambda *_: {"runs-on": "ubuntu-latest"})
    assert families.matrix_legs("w.yml", "j") == [{}], (
        "a job with no matrix must expand to exactly one empty leg"
    )


def test_a_matrix_without_include_is_a_contract_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail loudly on a matrix shape this reader cannot expand."""
    monkeypatch.setattr(
        families, "job", lambda *_: {"strategy": {"matrix": {"python": ["3.13"]}}}
    )
    with pytest.raises(AssertionError, match="only `include` lists are supported"):
        families.matrix_legs("w.yml", "j")


def test_an_unmapped_runner_label_is_a_contract_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse to guess a cache lane from a label nobody has declared."""
    renderer = {
        "uses": families.CACHE_KEYS_ACTION,
        "with": {"python-version": "3.13", "compiler-shape": "debug"},
    }
    monkeypatch.setattr(families, "job", lambda *_: {"runs-on": "some-new-shape"})
    monkeypatch.setattr(families, "steps", lambda *_: [renderer])
    monkeypatch.setattr(families, "save_steps", lambda *_: [])
    with pytest.raises(AssertionError, match="unmapped label"):
        families.writer_families("w.yml", "j")
