"""Unit tests for context allowlist narrowing and hook merging."""

from __future__ import annotations

import typing as typ
from unittest import mock

import pytest
from hypothesis import given, settings

from cuprum.catalogue import ECHO, LS
from cuprum.context import (
    AfterHook,
    BeforeHook,
    CuprumContext,
    ForbiddenProgramError,
    ScopeConfig,
)
from cuprum.context._policy import (
    _is_narrowed_allowlist_restricted,
    _merge_hooks,
    _narrow_allowlist,
)
from cuprum.program import Program
from cuprum.unittests import strategies as cuprum_st

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_PROPERTY_SETTINGS = settings(derandomize=True, deadline=None, max_examples=50)

# Run the symbolic backend for these tests with:
#   uv run pytest cuprum/unittests/test_context.py -m crosshair \
#     --hypothesis-profile=crosshair

# =============================================================================
# CuprumContext Basics
# =============================================================================


def test_narrow_reduces_allowlist() -> None:
    """narrow() intersects with parent allowlist."""
    parent = CuprumContext(allowlist=frozenset([ECHO, LS]))
    narrowed = parent.narrow(ScopeConfig(allowlist=frozenset([ECHO])))
    assert narrowed.allowlist == frozenset([ECHO])


def test_narrow_cannot_widen_allowlist() -> None:
    """narrow() cannot add programs not in parent when parent is non-empty."""
    new_program = Program("cat")
    parent = CuprumContext(allowlist=frozenset([ECHO]))
    narrowed = parent.narrow(ScopeConfig(allowlist=frozenset([ECHO, new_program])))
    assert narrowed.allowlist == frozenset([ECHO])


def test_narrow_establishes_base_when_parent_empty() -> None:
    """narrow() uses provided allowlist when parent is empty."""
    parent = CuprumContext()  # Empty allowlist
    narrowed = parent.narrow(ScopeConfig(allowlist=frozenset([ECHO, LS])))
    assert narrowed.allowlist == frozenset([ECHO, LS])


def test_narrow_appends_before_hooks() -> None:
    """narrow() appends new before hooks after parent hooks."""
    parent_hook: BeforeHook = mock.Mock()
    child_hook: BeforeHook = mock.Mock()
    parent = CuprumContext(before_hooks=(parent_hook,))
    narrowed = parent.narrow(ScopeConfig(before_hooks=(child_hook,)))
    assert narrowed.before_hooks == (parent_hook, child_hook)


def test_narrow_prepends_after_hooks() -> None:
    """narrow() prepends new after hooks before parent hooks (LIFO)."""
    parent_hook: AfterHook = mock.Mock()
    child_hook: AfterHook = mock.Mock()
    parent = CuprumContext(after_hooks=(parent_hook,))
    narrowed = parent.narrow(ScopeConfig(after_hooks=(child_hook,)))
    # After hooks run inner-to-outer: child first, then parent
    assert narrowed.after_hooks == (child_hook, parent_hook)


def test_narrowed_empty_allowlist_remains_restricted() -> None:
    """narrow() preserves an empty restricted allowlist across nested scopes."""
    parent = CuprumContext(allowlist=frozenset([ECHO]))
    empty = parent.narrow(ScopeConfig(allowlist=frozenset()))
    narrowed = empty.narrow(ScopeConfig(allowlist=frozenset([ECHO])))

    assert narrowed.allowlist == frozenset()
    with pytest.raises(ForbiddenProgramError):
        narrowed.check_allowed(ECHO)


def test_with_allowlist_preserves_empty_restricted_allowlist() -> None:
    """with_allowlist() does not make a restricted empty allowlist permissive."""
    parent = CuprumContext(allowlist=frozenset([ECHO]))
    empty = parent.narrow(ScopeConfig(allowlist=frozenset()))
    replaced = empty.with_allowlist(frozenset())

    assert replaced.allowlist == frozenset()
    with pytest.raises(ForbiddenProgramError):
        replaced.check_allowed(ECHO)


def test_with_allowlist_empty_preserves_existing_non_empty_restriction() -> None:
    """with_allowlist() keeps prior non-empty allowlists restrictive."""
    parent = CuprumContext(allowlist=frozenset([ECHO]))
    replaced = parent.with_allowlist(frozenset())

    assert replaced.allowlist == frozenset()
    with pytest.raises(ForbiddenProgramError):
        replaced.check_allowed(ECHO)


def test_with_program_and_without_program_preserve_restriction() -> None:
    """Program mutation helpers maintain explicit allowlist policy."""
    ctx = CuprumContext().with_program(ECHO)
    emptied = ctx.without_program(ECHO)

    assert ctx.is_allowed(ECHO) is True
    assert emptied.allowlist == frozenset()
    with pytest.raises(ForbiddenProgramError):
        emptied.check_allowed(ECHO)


@pytest.mark.crosshair
@_PROPERTY_SETTINGS
@given(parent=cuprum_st.allowlists(), config=cuprum_st.optional_allowlists())
def test_narrow_allowlist_can_only_shrink_non_empty_parent(
    parent: frozenset[Program],
    config: frozenset[Program] | None,
) -> None:
    """Property: narrowing cannot broaden a populated parent allowlist."""
    result = _narrow_allowlist(parent, config)

    if parent:
        assert result <= parent


@pytest.mark.crosshair
@_PROPERTY_SETTINGS
@given(parent=cuprum_st.allowlists(), config=cuprum_st.allowlists())
def test_narrow_allowlist_result_subset_of_config(
    parent: frozenset[Program],
    config: frozenset[Program],
) -> None:
    """Property: explicit config allowlists constrain the result."""
    assert _narrow_allowlist(parent, config) <= config


@pytest.mark.crosshair
@_PROPERTY_SETTINGS
@given(parent=cuprum_st.allowlists())
def test_narrow_allowlist_none_config_preserves_parent(
    parent: frozenset[Program],
) -> None:
    """Property: absent config allowlist inherits the parent exactly."""
    assert _narrow_allowlist(parent, None) == parent


@pytest.mark.crosshair
@_PROPERTY_SETTINGS
@given(
    parent=cuprum_st.allowlists().filter(bool),
    first=cuprum_st.allowlists(),
    second=cuprum_st.allowlists(),
)
def test_narrow_allowlist_two_steps_match_single_intersection(
    parent: frozenset[Program],
    first: frozenset[Program],
    second: frozenset[Program],
) -> None:
    """Property: repeated narrowing matches one equivalent intersection."""
    first_step = _narrow_allowlist(parent, first, parent_is_restricted=True)
    two_step = _narrow_allowlist(first_step, second, parent_is_restricted=True)
    single_step = _narrow_allowlist(parent, first & second)

    assert two_step == single_step


@pytest.mark.parametrize(
    ("config", "parent_is_restricted", "expected"),
    [
        pytest.param(None, False, False, id="unrestricted-parent-no-config"),
        pytest.param(None, True, True, id="restricted-parent-sticky"),
        pytest.param(frozenset(), False, True, id="empty-config-restricts"),
        pytest.param(frozenset({ECHO}), False, True, id="config-restricts"),
        pytest.param(frozenset({ECHO}), True, True, id="config-and-restricted"),
    ],
)
def test_is_narrowed_allowlist_restricted(
    config: frozenset[Program] | None,
    *,
    parent_is_restricted: bool,
    expected: bool,
) -> None:
    """A narrowed context is restricted when the parent was or a config is set.

    The restriction is sticky (a restricted parent stays restricted) and any
    supplied config allowlist — even an empty one — establishes a restriction.
    """
    result = _is_narrowed_allowlist_restricted(
        config,
        parent_is_restricted=parent_is_restricted,
    )
    assert result is expected, (
        "narrowing restriction must follow the parent flag and supplied config"
    )


@pytest.mark.crosshair
@pytest.mark.parametrize("scoped_first", [False, True])
@_PROPERTY_SETTINGS
@given(parent=cuprum_st.hook_tuples(), config=cuprum_st.hook_tuples())
def test_merge_hooks_preserves_scope_ordering(
    parent: tuple[cabc.Callable[..., None], ...],
    config: tuple[cabc.Callable[..., None], ...],
    *,
    scoped_first: bool,
) -> None:
    """Property: ``_merge_hooks`` orders scoped hooks per ``scoped_first``.

    ``scoped_first=False`` keeps ``parent + config`` (FIFO order used for
    before and observe hooks); ``scoped_first=True`` keeps ``config + parent``
    (LIFO order used for after hooks). Length and membership are preserved in
    both modes. The before-, after-, and observe-hook casts are all exercised
    so their tuple compatibility with the generic helper stays type-checked.
    """
    before_result = _merge_hooks(
        typ.cast("tuple[BeforeHook, ...]", parent),
        typ.cast("tuple[BeforeHook, ...]", config),
        scoped_first=scoped_first,
    )
    after_result = _merge_hooks(
        typ.cast("tuple[AfterHook, ...]", parent),
        typ.cast("tuple[AfterHook, ...]", config),
        scoped_first=scoped_first,
    )
    observe_result = _merge_hooks(
        typ.cast("tuple[cabc.Callable[..., None], ...]", parent),
        typ.cast("tuple[cabc.Callable[..., None], ...]", config),
        scoped_first=scoped_first,
    )

    expected = config + parent if scoped_first else parent + config
    for result in (before_result, after_result, observe_result):
        assert result == expected, (
            "merged hooks must follow the requested scope ordering"
        )
        assert len(result) == len(parent) + len(config), (
            "merging must preserve the total hook count"
        )
        assert set(result) == {*parent, *config}, (
            "merging must preserve hook membership"
        )
