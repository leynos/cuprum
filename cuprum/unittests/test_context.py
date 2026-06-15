"""Unit tests for CuprumContext and hooks."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import typing as typ
from unittest import mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum.catalogue import ECHO, LS
from cuprum.context import (
    AfterHook,
    BeforeHook,
    CuprumContext,
    ForbiddenProgramError,
    ScopeConfig,
    _merge_after_hooks,
    _merge_before_hooks,
    _merge_observe_hooks,
    _narrow_allowlist,
    _resolve_narrowed_timeout,
    _validate_timeout,
    after,
    allow,
    before,
    current_context,
    get_context,
    scoped,
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


def test_empty_context_has_no_allowlist() -> None:
    """A context without explicit allowlist has an empty frozenset."""
    ctx = CuprumContext()
    assert ctx.allowlist == frozenset()


def test_check_allowed_permits_all_with_empty_allowlist() -> None:
    """check_allowed permits all programs when allowlist is empty."""
    ctx = CuprumContext()  # Empty allowlist permits all (permissive default)
    ctx.check_allowed(ECHO)  # Must not raise
    ctx.check_allowed(LS)  # Must not raise


def test_context_with_allowlist() -> None:
    """Context retains provided allowlist."""
    programs = frozenset([ECHO, LS])
    ctx = CuprumContext(allowlist=programs)
    assert ctx.allowlist == programs


def test_is_allowed_returns_true_for_allowed_program() -> None:
    """is_allowed returns True when program is in allowlist."""
    ctx = CuprumContext(allowlist=frozenset([ECHO]))
    assert ctx.is_allowed(ECHO) is True


def test_is_allowed_returns_false_for_disallowed_program() -> None:
    """is_allowed returns False when program is not in allowlist."""
    ctx = CuprumContext(allowlist=frozenset([ECHO]))
    assert ctx.is_allowed(LS) is False


def test_empty_hooks_by_default() -> None:
    """Context has empty hooks by default."""
    ctx = CuprumContext()
    assert ctx.before_hooks == ()
    assert ctx.after_hooks == ()


def test_context_with_hooks() -> None:
    """Context retains provided hooks."""
    before_hook: BeforeHook = mock.Mock()
    after_hook: AfterHook = mock.Mock()
    ctx = CuprumContext(before_hooks=(before_hook,), after_hooks=(after_hook,))
    assert ctx.before_hooks == (before_hook,)
    assert ctx.after_hooks == (after_hook,)


# =============================================================================
# Context Narrowing
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


@pytest.mark.crosshair
@_PROPERTY_SETTINGS
@given(parent=cuprum_st.hook_tuples(), config=cuprum_st.hook_tuples())
def test_merge_before_hooks_preserves_fifo_order(
    parent: tuple[cabc.Callable[..., None], ...],
    config: tuple[cabc.Callable[..., None], ...],
) -> None:
    """Property: before hooks append child hooks after parent hooks."""
    result = _merge_before_hooks(
        typ.cast("tuple[BeforeHook, ...]", parent),
        typ.cast("tuple[BeforeHook, ...]", config),
    )

    assert len(result) == len(parent) + len(config)
    assert result[: len(parent)] == parent
    assert result[len(parent) :] == config
    assert set(result) == {*parent, *config}


@pytest.mark.crosshair
@_PROPERTY_SETTINGS
@given(parent=cuprum_st.hook_tuples(), config=cuprum_st.hook_tuples())
def test_merge_after_hooks_preserves_lifo_order(
    parent: tuple[cabc.Callable[..., None], ...],
    config: tuple[cabc.Callable[..., None], ...],
) -> None:
    """Property: after hooks prepend child hooks before parent hooks."""
    result = _merge_after_hooks(
        typ.cast("tuple[AfterHook, ...]", parent),
        typ.cast("tuple[AfterHook, ...]", config),
    )

    assert len(result) == len(parent) + len(config)
    assert result[: len(config)] == config
    assert result[len(config) :] == parent
    assert set(result) == {*parent, *config}


@pytest.mark.crosshair
@_PROPERTY_SETTINGS
@given(parent=cuprum_st.hook_tuples(), config=cuprum_st.hook_tuples())
def test_merge_observe_hooks_preserves_fifo_order(
    parent: tuple[cabc.Callable[..., None], ...],
    config: tuple[cabc.Callable[..., None], ...],
) -> None:
    """Property: observe hooks append child hooks after parent hooks."""
    result = _merge_observe_hooks(
        typ.cast("tuple[typ.Any, ...]", parent),
        typ.cast("tuple[typ.Any, ...]", config),
    )

    assert len(result) == len(parent) + len(config)
    assert result[: len(parent)] == parent
    assert result[len(parent) :] == config
    assert set(result) == {*parent, *config}


def test_current_context_returns_context() -> None:
    """current_context() returns the current context."""
    ctx = current_context()
    assert isinstance(ctx, CuprumContext)


def test_get_context_returns_same_as_current() -> None:
    """get_context() is an alias for current_context()."""
    assert get_context() is current_context()


# =============================================================================
# Scoped Context Manager
# =============================================================================


def test_scoped_narrows_allowlist_in_block() -> None:
    """scoped(ScopeConfig()) narrows allowlist within the context block."""
    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))) as ctx:
        assert ctx.is_allowed(ECHO) is True
        assert current_context() is ctx


def test_scoped_restores_context_after_block() -> None:
    """scoped(ScopeConfig()) restores previous context after exiting block."""
    original = current_context()
    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
        pass
    assert current_context() is original


def test_scoped_restores_on_exception() -> None:
    """scoped(ScopeConfig()) restores context even when exception is raised."""
    original = current_context()
    with (
        pytest.raises(ValueError, match=r"test"),
        scoped(ScopeConfig(allowlist=frozenset([ECHO]))),
    ):
        raise ValueError("test")
    assert current_context() is original


def test_nested_scopes_stack_correctly() -> None:
    """Nested scoped(ScopeConfig()) calls narrow progressively."""
    with scoped(ScopeConfig(allowlist=frozenset([ECHO, LS]))) as outer:
        assert outer.is_allowed(ECHO) is True
        assert outer.is_allowed(LS) is True
        with scoped(ScopeConfig(allowlist=frozenset([ECHO]))) as inner:
            assert inner.is_allowed(ECHO) is True
            assert inner.is_allowed(LS) is False
        # Back to outer scope
        assert current_context().is_allowed(LS) is True


# =============================================================================
# AllowRegistration
# =============================================================================


def test_allow_adds_programs_to_context() -> None:
    """AllowRegistration adds programs to current context allowlist."""
    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
        reg = allow(LS)
        assert current_context().is_allowed(LS) is True
        reg.detach()
        # After detach, LS should no longer be allowed in current scope
        assert current_context().is_allowed(LS) is False


def test_allow_as_context_manager() -> None:
    """AllowRegistration can be used as a context manager."""
    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
        with allow(LS):
            assert current_context().is_allowed(LS) is True
        assert current_context().is_allowed(LS) is False


# =============================================================================
# HookRegistration
# =============================================================================


def test_before_hook_registration_and_detach() -> None:
    """before() registers a hook that can be detached."""
    hook: BeforeHook = mock.Mock()
    with scoped(ScopeConfig()):
        reg = before(hook)
        assert hook in current_context().before_hooks
        reg.detach()
        assert hook not in current_context().before_hooks


def test_after_hook_registration_and_detach() -> None:
    """after() registers a hook that can be detached."""
    hook: AfterHook = mock.Mock()
    with scoped(ScopeConfig()):
        reg = after(hook)
        assert hook in current_context().after_hooks
        reg.detach()
        assert hook not in current_context().after_hooks


def test_before_hook_as_context_manager() -> None:
    """before() can be used as a context manager."""
    hook: BeforeHook = mock.Mock()
    with scoped(ScopeConfig()):
        with before(hook):
            assert hook in current_context().before_hooks
        assert hook not in current_context().before_hooks


def test_after_hook_as_context_manager() -> None:
    """after() can be used as a context manager."""
    hook: AfterHook = mock.Mock()
    with scoped(ScopeConfig()):
        with after(hook):
            assert hook in current_context().after_hooks
        assert hook not in current_context().after_hooks


# =============================================================================
# Hook Ordering
# =============================================================================


def test_before_hooks_execute_in_registration_order() -> None:
    """Before hooks execute in registration order (FIFO)."""
    call_order: list[int] = []

    def hook1(cmd: object) -> None:
        """Record this before hook as the first to run."""
        _ = cmd  # Unused
        call_order.append(1)

    def hook2(cmd: object) -> None:
        """Record this before hook as the second to run."""
        _ = cmd  # Unused
        call_order.append(2)

    def hook3(cmd: object) -> None:
        """Record this before hook as the third to run."""
        _ = cmd  # Unused
        call_order.append(3)

    ctx = CuprumContext(
        before_hooks=(
            typ.cast("BeforeHook", hook1),
            typ.cast("BeforeHook", hook2),
            typ.cast("BeforeHook", hook3),
        ),
    )

    # Execute hooks manually to verify order
    for hook in ctx.before_hooks:
        hook(typ.cast("typ.Any", None))

    assert call_order == [1, 2, 3]


def test_after_hooks_execute_in_reverse_registration_order() -> None:
    """After hooks execute inner-to-outer (LIFO within a level)."""
    call_order: list[int] = []

    def hook1(cmd: object, result: object) -> None:
        """Record this after hook as the first registered."""
        _, _ = cmd, result  # Unused
        call_order.append(1)

    def hook2(cmd: object, result: object) -> None:
        """Record this after hook as the second registered."""
        _, _ = cmd, result  # Unused
        call_order.append(2)

    def hook3(cmd: object, result: object) -> None:
        """Record this after hook as the third registered."""
        _, _ = cmd, result  # Unused
        call_order.append(3)

    # In after_hooks, prepended hooks run first
    ctx = CuprumContext(
        after_hooks=(
            typ.cast("AfterHook", hook3),
            typ.cast("AfterHook", hook2),
            typ.cast("AfterHook", hook1),
        ),
    )

    for hook in ctx.after_hooks:
        hook(typ.cast("typ.Any", None), typ.cast("typ.Any", None))

    assert call_order == [3, 2, 1]


# =============================================================================
# Context Isolation (Threads)
# =============================================================================


def test_context_is_isolated_per_thread() -> None:
    """Each thread has its own context."""
    results: dict[str, bool] = {}

    def thread_worker(name: str, programs: frozenset[Program]) -> None:
        """Capture the allowlist decision observed inside the worker thread."""
        with scoped(ScopeConfig(allowlist=programs)):
            results[name] = current_context().is_allowed(ECHO)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(thread_worker, "thread1", frozenset([ECHO]))
        f2 = executor.submit(thread_worker, "thread2", frozenset([LS]))
        f1.result()
        f2.result()

    assert results["thread1"] is True
    assert results["thread2"] is False


# =============================================================================
# Context Isolation (Async Tasks)
# =============================================================================


def test_context_is_isolated_per_async_task() -> None:
    """Each async task has its own context."""
    results: dict[str, bool] = {}

    async def task_worker(name: str, programs: frozenset[Program]) -> None:
        """Capture the allowlist decision observed inside the async task."""
        with scoped(ScopeConfig(allowlist=programs)):
            await asyncio.sleep(0.01)  # Yield to allow interleaving
            results[name] = current_context().is_allowed(ECHO)

    async def run_tasks() -> None:
        """Run both task workers concurrently to interleave their scopes."""
        await asyncio.gather(
            task_worker("task1", frozenset([ECHO])),
            task_worker("task2", frozenset([LS])),
        )

    asyncio.run(run_tasks())

    assert results["task1"] is True
    assert results["task2"] is False


# =============================================================================
# ForbiddenProgramError
# =============================================================================


def test_forbidden_program_error_raised_for_disallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """check_allowed raises and logs denied programs."""
    ctx = CuprumContext().narrow(ScopeConfig(allowlist=frozenset([ECHO])))

    caplog.set_level(logging.WARNING, logger="cuprum.context")

    with pytest.raises(ForbiddenProgramError) as exc_info:
        ctx.check_allowed(LS)

    assert "ls" in str(exc_info.value).lower()
    records = [
        record
        for record in caplog.records
        if record.name == "cuprum.context" and record.levelno == logging.WARNING
    ]
    assert len(records) == 1
    record = typ.cast("typ.Any", records[0])
    assert "ls" in record.getMessage()
    assert "restricted_state=True" in record.getMessage()
    assert record.operation == LS
    assert record.restricted_state is True


def test_check_allowed_passes_for_allowed_program() -> None:
    """check_allowed does not raise for allowed programs."""
    ctx = CuprumContext(allowlist=frozenset([ECHO]))
    ctx.check_allowed(ECHO)  # Should not raise


# =============================================================================
# Timeout Validation
# =============================================================================

_TIMEOUT_VALIDATION_CASES = [
    pytest.param(None, None, None, id="none-is-valid"),
    pytest.param(0.0, 0.0, None, id="zero-is-valid"),
    pytest.param(5.0, 5.0, None, id="positive-float-is-valid"),
    pytest.param(
        typ.cast("float", 5),
        5.0,
        None,
        id="positive-int-coerced-to-float",
    ),
    pytest.param(-1.0, None, r"timeout must be non-negative.*-1\.0", id="negative"),
    pytest.param(
        typ.cast("float", -5),
        None,
        r"timeout must be non-negative.*-5\.0",
        id="negative-int",
    ),
]


@pytest.mark.parametrize(
    ("timeout_input", "expected_result", "error_pattern"),
    _TIMEOUT_VALIDATION_CASES,
)
def test_scope_config_timeout_validation(
    timeout_input: float | None,
    expected_result: float | None,
    error_pattern: str | None,
) -> None:
    """ScopeConfig validates and coerces timeout values correctly."""
    if error_pattern is not None:
        with pytest.raises(ValueError, match=error_pattern):
            ScopeConfig(timeout=timeout_input)
    else:
        config = ScopeConfig(timeout=timeout_input)
        assert config.timeout == expected_result
        if expected_result is not None:
            assert isinstance(config.timeout, float)


@pytest.mark.parametrize(
    ("timeout_input", "expected_result", "error_pattern"),
    _TIMEOUT_VALIDATION_CASES,
)
def test_cuprum_context_timeout_validation(
    timeout_input: float | None,
    expected_result: float | None,
    error_pattern: str | None,
) -> None:
    """CuprumContext validates and coerces timeout values correctly."""
    if error_pattern is not None:
        with pytest.raises(ValueError, match=error_pattern):
            CuprumContext(timeout=timeout_input)
    else:
        ctx = CuprumContext(timeout=timeout_input)
        assert ctx.timeout == expected_result
        if expected_result is not None:
            assert isinstance(ctx.timeout, float)


@pytest.mark.crosshair
@_PROPERTY_SETTINGS
@given(timeout=st.none())
def test_validate_timeout_preserves_none(timeout: None) -> None:
    """Property: None timeout values are preserved."""
    assert _validate_timeout(timeout, "Test") is None


@pytest.mark.crosshair
@_PROPERTY_SETTINGS
@given(timeout=st.floats(min_value=0.0, max_value=3600.0, allow_nan=False))
def test_validate_timeout_preserves_non_negative_floats(timeout: float) -> None:
    """Property: non-negative float timeouts are accepted unchanged."""
    result = _validate_timeout(timeout, "Test")

    assert result is not None
    assert result >= timeout
    assert result <= timeout


@pytest.mark.crosshair
@_PROPERTY_SETTINGS
@given(timeout=st.integers(min_value=0, max_value=10**18))
def test_validate_timeout_coerces_non_negative_integers(timeout: int) -> None:
    """Property: non-negative integer timeouts are coerced to float."""
    result = _validate_timeout(typ.cast("float", timeout), "Test")

    assert result is not None
    assert isinstance(result, float)
    assert result.hex() == float(timeout).hex()


@pytest.mark.crosshair
@_PROPERTY_SETTINGS
@given(timeout=st.floats(max_value=-0.000001, allow_nan=False))
def test_validate_timeout_rejects_negative_floats(timeout: float) -> None:
    """Property: negative float timeouts are rejected."""
    with pytest.raises(ValueError, match="timeout must be non-negative"):
        _validate_timeout(timeout, "Test")


@pytest.mark.crosshair
@_PROPERTY_SETTINGS
@given(timeout=st.integers(max_value=-1))
def test_validate_timeout_rejects_negative_integers(timeout: int) -> None:
    """Property: negative integer timeouts are rejected."""
    with pytest.raises(ValueError, match="timeout must be non-negative"):
        _validate_timeout(typ.cast("float", timeout), "Test")


@pytest.mark.crosshair
@_PROPERTY_SETTINGS
@given(parent=cuprum_st.timeouts())
def test_resolve_narrowed_timeout_inherits_without_config(
    parent: float | None,
) -> None:
    """Property: absent config timeout inherits the parent timeout."""
    assert _resolve_narrowed_timeout(parent, None) == parent


@pytest.mark.crosshair
@_PROPERTY_SETTINGS
@given(
    parent=cuprum_st.timeouts(),
    config=st.floats(min_value=0.0, max_value=3600.0, allow_nan=False),
)
def test_resolve_narrowed_timeout_config_overrides_parent(
    parent: float | None,
    config: float,
) -> None:
    """Property: explicit config timeout overrides any parent timeout."""
    assert _resolve_narrowed_timeout(parent, config) == config
