"""Named examples for executable bindings held on the execution context.

These examples pin the boundary between the two authorities the context
carries. The allowlist decides *which logical programs may run*; a binding
decides *which executable a permitted program runs*. A binding is additive
policy for an already-permitted program, never a way to permit one, so the
tests below assert both that resolution works and that it leaves
:meth:`CuprumContext.is_allowed` and :meth:`CuprumContext.check_allowed`
untouched.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest

from cuprum.catalogue import ECHO, LS
from cuprum.context import (
    ContextError,
    CuprumContext,
    ForbiddenProgramError,
    ScopeConfig,
    current_context,
    scoped,
)
from cuprum.context.registration import ExecutableBindingRegistration, bind_executable
from cuprum.executable_binding import (
    ExecutableBinding,
    InvalidExecutableBindingError,
    PathBindingRejection,
    executable_binding,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.program import Program

_ECHO_BINDING = executable_binding(ECHO, "/opt/tools/echo")
_LS_BINDING = executable_binding(LS, "/opt/tools/ls")


def _always_absent() -> str:
    """Return a path that is never reached inside the sandbox."""
    return "/opt/tools/resolved-echo.py"


# --------------------------------------------------------------------------
# ScopeConfig and CuprumContext fields
# --------------------------------------------------------------------------


def test_scope_config_defaults_to_no_bindings() -> None:
    """A scope that states no bindings inherits whatever is in effect."""
    assert ScopeConfig().executable_bindings is None


def test_context_defaults_to_no_bindings() -> None:
    """A bare context carries no bindings; every program is unbound."""
    assert CuprumContext().executable_bindings is None


def test_scope_config_snapshots_a_supplied_layer() -> None:
    """A supplied mapping is copied, not aliased, on construction."""
    layer: dict[Program, ExecutableBinding] = {ECHO: _ECHO_BINDING}
    config = ScopeConfig(executable_bindings=typ.cast("typ.Any", layer))
    layer.clear()
    assert config.executable_bindings == {ECHO: _ECHO_BINDING}, (
        "ScopeConfig must not alias a mutable mapping supplied by the caller"
    )


def test_context_rejects_item_assignment_on_its_layer() -> None:
    """The context's own layer is read-only."""
    context = CuprumContext().with_executable_binding(ECHO, _ECHO_BINDING)
    layer = typ.cast(
        "cabc.MutableMapping[Program, ExecutableBinding]",
        context.executable_bindings,
    )
    with pytest.raises(TypeError):
        layer[ECHO] = _LS_BINDING


# --------------------------------------------------------------------------
# with_executable_binding
# --------------------------------------------------------------------------


def test_with_executable_binding_adds_one_entry() -> None:
    """A context gains exactly the binding it was given."""
    context = CuprumContext().with_executable_binding(ECHO, _ECHO_BINDING)
    assert context.executable_bindings == {ECHO: _ECHO_BINDING}


def test_with_executable_binding_leaves_the_original_context_alone() -> None:
    """The update returns a new context; the receiver is unchanged."""
    original = CuprumContext()
    original.with_executable_binding(ECHO, _ECHO_BINDING)
    assert original.executable_bindings is None, (
        "with_executable_binding must not mutate the context it was called on"
    )


def test_with_executable_binding_replaces_an_existing_entry() -> None:
    """Rebinding a program overwrites the previous executable."""
    context = (
        CuprumContext()
        .with_executable_binding(ECHO, _ECHO_BINDING)
        .with_executable_binding(ECHO, _LS_BINDING)
    )
    assert context.executable_bindings == {ECHO: _LS_BINDING}, (
        "the most recent binding for a program must win"
    )


def test_with_executable_binding_keeps_other_entries() -> None:
    """Adding a binding for one program preserves bindings for others."""
    context = (
        CuprumContext()
        .with_executable_binding(ECHO, _ECHO_BINDING)
        .with_executable_binding(LS, _LS_BINDING)
    )
    assert context.executable_bindings == {ECHO: _ECHO_BINDING, LS: _LS_BINDING}


# --------------------------------------------------------------------------
# resolve_executable
# --------------------------------------------------------------------------


def test_resolve_executable_returns_none_for_an_unbound_program() -> None:
    """An unbound program resolves to ``None``, meaning *run it as named*."""
    assert CuprumContext().resolve_executable(ECHO, cwd=None) is None


def test_resolve_executable_returns_the_bound_path() -> None:
    """A bound program resolves to its executable string."""
    context = CuprumContext().with_executable_binding(ECHO, _ECHO_BINDING)
    assert context.resolve_executable(ECHO, cwd=None) == "/opt/tools/echo"


def test_resolve_executable_evaluates_a_resolver() -> None:
    """A lazy binding is evaluated when the executable is resolved."""
    calls: list[int] = []

    def resolver() -> str:
        calls.append(len(calls) + 1)
        return "/opt/tools/late"

    context = CuprumContext().with_executable_binding(
        ECHO,
        executable_binding(ECHO, resolver),
    )
    assert context.resolve_executable(ECHO, cwd=None) == "/opt/tools/late"
    assert calls == [1], "resolution must evaluate the resolver exactly once"


def test_resolve_executable_anchors_a_relative_binding_at_cwd() -> None:
    """A relative binding is joined onto the working directory."""
    context = CuprumContext().with_executable_binding(
        ECHO,
        executable_binding(ECHO, "bin/echo", allow_relative=True),
    )
    assert context.resolve_executable(ECHO, cwd="/srv/project") == (
        "/srv/project/bin/echo"
    )


def test_resolve_executable_leaves_a_relative_binding_alone_without_cwd() -> None:
    """Without a working directory the platform resolves the spelling."""
    context = CuprumContext().with_executable_binding(
        ECHO,
        executable_binding(ECHO, "bin/echo", allow_relative=True),
    )
    assert context.resolve_executable(ECHO, cwd=None) == "bin/echo"


def test_resolve_executable_is_independent_of_the_allowlist() -> None:
    """Binding a program a context forbids still reports its executable.

    The allowlist is enforced before resolution, at the spawn site; this keeps
    resolution a pure lookup so an unpermitted program cannot become runnable
    by acquiring a binding, and so the refusal still names the logical
    program rather than a path.
    """
    context = CuprumContext(allowlist=frozenset([LS])).with_executable_binding(
        ECHO,
        _ECHO_BINDING,
    )
    assert context.resolve_executable(ECHO, cwd=None) == "/opt/tools/echo"
    assert context.is_allowed(ECHO) is False, "binding must not widen the allowlist"
    with pytest.raises(ForbiddenProgramError):
        context.check_allowed(ECHO)


def test_resolve_executable_accepts_a_string_program_name() -> None:
    """Keys are ``Program`` values, whose runtime type is ``str``."""
    context = CuprumContext().with_executable_binding(ECHO, _ECHO_BINDING)
    assert context.resolve_executable(typ.cast("typ.Any", "echo"), cwd=None) == (
        "/opt/tools/echo"
    )


# --------------------------------------------------------------------------
# narrow
# --------------------------------------------------------------------------


def test_narrow_inherits_parent_bindings_when_the_scope_states_none() -> None:
    """A scope that says nothing about bindings keeps the parent's."""
    parent = CuprumContext().with_executable_binding(ECHO, _ECHO_BINDING)
    narrowed = parent.narrow(ScopeConfig(allowlist=frozenset([ECHO, LS])))
    assert narrowed.executable_bindings == {ECHO: _ECHO_BINDING}


def test_narrow_layers_a_scoped_binding_over_the_parent() -> None:
    """A scoped binding for a program replaces the inherited one."""
    parent = CuprumContext().with_executable_binding(ECHO, _ECHO_BINDING)
    narrowed = parent.narrow(
        ScopeConfig(executable_bindings={ECHO: _LS_BINDING}),
    )
    assert narrowed.executable_bindings == {ECHO: _LS_BINDING}, (
        "the innermost binding for a program must win"
    )


def test_narrow_unions_bindings_for_distinct_programs() -> None:
    """A scoped binding for a new program joins the inherited layer."""
    parent = CuprumContext().with_executable_binding(ECHO, _ECHO_BINDING)
    narrowed = parent.narrow(ScopeConfig(executable_bindings={LS: _LS_BINDING}))
    assert narrowed.executable_bindings == {ECHO: _ECHO_BINDING, LS: _LS_BINDING}


def test_narrow_does_not_mutate_the_parent_bindings() -> None:
    """Narrowing produces a derived context, leaving the parent intact."""
    parent = CuprumContext().with_executable_binding(ECHO, _ECHO_BINDING)
    parent.narrow(ScopeConfig(executable_bindings={ECHO: _LS_BINDING}))
    assert parent.executable_bindings == {ECHO: _ECHO_BINDING}


def test_scoped_context_manager_activates_bindings() -> None:
    """A scoped binding is visible through the current context."""
    with scoped(ScopeConfig(executable_bindings={ECHO: _ECHO_BINDING})):
        assert current_context().resolve_executable(ECHO, cwd=None) == (
            "/opt/tools/echo"
        )


# --------------------------------------------------------------------------
# bind_executable
# --------------------------------------------------------------------------


def test_bind_executable_installs_a_binding_in_the_current_context() -> None:
    """The factory registers the binding and reports the derived context."""
    with bind_executable(ECHO, "/opt/tools/echo") as registration:
        assert isinstance(registration, ExecutableBindingRegistration)
        assert current_context().resolve_executable(ECHO, cwd=None) == (
            "/opt/tools/echo"
        )


def test_bind_executable_restores_the_previous_context_on_exit() -> None:
    """Leaving the scope removes the binding again."""
    before = current_context()
    with bind_executable(ECHO, "/opt/tools/echo"):
        assert current_context() is not before
    assert current_context() is before, "scope exit must restore the prior context"


def test_bind_executable_detach_is_idempotent() -> None:
    """Detaching twice leaves the restored context in place."""
    before = current_context()
    handle = bind_executable(ECHO, "/opt/tools/echo")
    handle.detach()
    after_first = current_context()
    handle.detach()
    assert current_context() is after_first, "a second detach must be a no-op"
    assert after_first is before, "the first detach must restore the prior context"


def test_bind_executable_accepts_a_resolver() -> None:
    """A callable is stored as a resolver rather than validated as a path."""
    with bind_executable(ECHO, _always_absent):
        assert current_context().resolve_executable(ECHO, cwd=None) == (
            "/opt/tools/resolved-echo.py"
        )


def test_bind_executable_nests_innermost_binding_wins() -> None:
    """A nested registration rebinds a program for the inner scope only."""
    with (
        bind_executable(ECHO, "/opt/tools/echo"),
        bind_executable(ECHO, "/opt/tools/other-echo"),
    ):
        assert current_context().resolve_executable(ECHO, cwd=None) == (
            "/opt/tools/other-echo"
        )


def test_bind_executable_validates_the_path_against_the_program() -> None:
    """A rejected path names the logical program it was being bound to."""
    with pytest.raises(InvalidExecutableBindingError) as exc_info:
        bind_executable(ECHO, "bin/echo")
    error = exc_info.value
    assert error.program == ECHO, "the error must name the logical program"
    assert error.path == "bin/echo", "the error must quote the rejected path"
    assert error.reason is PathBindingRejection.NOT_ABSOLUTE


def test_bind_executable_honours_allow_relative() -> None:
    """A caller may opt into a relative path explicitly."""
    with bind_executable(ECHO, "bin/echo", allow_relative=True):
        assert current_context().resolve_executable(ECHO, cwd="/srv") == (
            "/srv/bin/echo"
        )


def test_bind_executable_does_not_release_an_existing_hold() -> None:
    """The handle keeps the derived context alive without weakening the scope."""
    context = CuprumContext(allowlist=frozenset([LS]))
    handle = bind_executable(ECHO, "/opt/tools/echo")
    try:
        assert handle is not None
        assert context.is_allowed(ECHO) is False
    finally:
        handle.detach()


def test_registration_is_a_context_manager_returning_a_context_error_base() -> None:
    """The handle participates in the context-error hierarchy's package."""
    handle = bind_executable(ECHO, "/opt/tools/echo")
    try:
        assert hasattr(handle, "__enter__"), "the handle must be a context manager"
    finally:
        handle.detach()
    assert issubclass(ForbiddenProgramError, ContextError)


def test_binding_layer_participates_in_context_equality() -> None:
    """Equality distinguishes contexts that differ only in their bindings."""
    bound = CuprumContext().with_executable_binding(ECHO, _ECHO_BINDING)
    other = CuprumContext().with_executable_binding(ECHO, _LS_BINDING)
    assert bound != other, "bindings must participate in context equality"

    rebound = CuprumContext().with_executable_binding(ECHO, _ECHO_BINDING)
    assert rebound == bound, "the same binding must compare equal"

    replaced = dc.replace(bound, allowlist=frozenset([ECHO]))
    assert replaced.executable_binding(ECHO) is _ECHO_BINDING, (
        "an unrelated field change must not disturb the binding layer"
    )
