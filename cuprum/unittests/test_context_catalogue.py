"""Scoped catalogue resolution on ``CuprumContext``.

Catalogue-specific context behaviour lives here: how a scope exposes its
catalogue, how nested and allowlist-only scopes nest and restore, how a
builder resolves against the innermost one, and where the two enforcement
points fire. The context mechanics that are not catalogue-specific stay in
``test_context.py``.

The property test at the end states the nesting invariant over arbitrary
scope sequences rather than fixed examples: after any interleaving of
catalogue scopes, allowlist-only scopes, and ``allow()`` registrations, the
active catalogue is the one named by the innermost catalogue scope, and
``None`` outside every catalogue scope.
"""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum import sh
from cuprum.catalogue import (
    ECHO,
    LS,
    Program,
    ProgramCatalogue,
    UnknownProgramError,
)
from cuprum.context import (
    ForbiddenProgramError,
    ScopeConfig,
    allow,
    current_context,
    scoped,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import contextlib


def test_scoped_catalogue_narrows_to_its_allowlist() -> None:
    """scoped(catalogue=...) derives permissions from the catalogue."""
    catalogue = ProgramCatalogue.from_programs(ECHO)
    original = current_context()

    with scoped(catalogue=catalogue) as ctx:
        assert ctx.allowlist == catalogue.allowlist, (
            "catalogue scope should copy the catalogue allowlist"
        )
        assert ctx.is_allowed(LS) is False, (
            "catalogue scope should exclude programs outside its allowlist"
        )
        assert current_context() is ctx, (
            "catalogue scope should activate its narrowed context"
        )
        ctx.check_allowed(ECHO)

    assert current_context() is original, (
        "catalogue scope should restore the previous context after normal exit"
    )


def test_scoped_catalogue_blocks_unrelated_builder_construction() -> None:
    """A catalogue scope resolves builders, so unknown programs fail early."""
    catalogue = ProgramCatalogue.from_programs(ECHO)

    with (
        scoped(catalogue=catalogue),
        pytest.raises(UnknownProgramError, match="ls"),
    ):
        sh.make(LS)


def test_scoped_catalogue_denies_a_foreign_catalogue_at_run_time() -> None:
    """A builder from another catalogue constructs, then the scope denies it."""
    scoped_catalogue = ProgramCatalogue.from_programs(ECHO)
    foreign_catalogue = ProgramCatalogue.from_programs(LS)
    original = current_context()

    with scoped(catalogue=scoped_catalogue) as ctx:
        # Construction consults the named catalogue, not the active scope.
        command = sh.make(LS, catalogue=foreign_catalogue)("--version")
        assert ctx.is_allowed(LS) is False, (
            "the scope allowlist should exclude the foreign catalogue's program"
        )
        with pytest.raises(ForbiddenProgramError, match="ls"):
            command.run_sync()

    assert current_context() is original, (
        "catalogue scope should restore the previous context after normal exit"
    )


def test_scoped_catalogue_is_exposed_on_the_active_context() -> None:
    """A catalogue scope records the catalogue sh.make resolves against."""
    catalogue = ProgramCatalogue.from_programs(ECHO)

    assert current_context().catalogue is None, (
        "the default context must not carry a catalogue"
    )

    with scoped(catalogue=catalogue) as ctx:
        assert ctx.catalogue is catalogue, (
            "catalogue scope should expose its catalogue on the derived context"
        )
        assert current_context().catalogue is catalogue, (
            "the active context should carry the scope's catalogue"
        )

    assert current_context().catalogue is None, (
        "leaving the scope should drop the catalogue"
    )


def test_scope_without_catalogue_inherits_the_active_one() -> None:
    """An allowlist-only scope leaves the inherited catalogue in place."""
    catalogue = ProgramCatalogue.from_programs(ECHO, LS)

    with (
        scoped(catalogue=catalogue),
        scoped(ScopeConfig(allowlist=frozenset([ECHO]))) as inner,
    ):
        assert inner.catalogue is catalogue, (
            "a scope that names no catalogue should inherit the active one"
        )


def test_nested_catalogue_scope_replaces_and_then_restores() -> None:
    """The innermost catalogue wins, and the outer one returns on exit."""
    outer_catalogue = ProgramCatalogue.from_programs(ECHO)
    inner_catalogue = ProgramCatalogue.from_programs(LS)

    with scoped(catalogue=outer_catalogue):
        assert current_context().catalogue is outer_catalogue, (
            "the outer catalogue should be active before the inner scope opens"
        )
        with scoped(catalogue=inner_catalogue):
            assert current_context().catalogue is inner_catalogue, (
                "the innermost catalogue scope should replace the outer one"
            )
        assert current_context().catalogue is outer_catalogue, (
            "exiting the inner scope should restore the outer catalogue"
        )


def test_allow_registration_preserves_the_scoped_catalogue() -> None:
    """Registering extra programs does not disturb the active catalogue."""
    catalogue = ProgramCatalogue.from_programs(ECHO)

    with scoped(catalogue=catalogue):
        registration = allow(LS)
        assert current_context().catalogue is catalogue, (
            "allow() should derive a context that keeps the scoped catalogue"
        )
        registration.detach()
        assert current_context().catalogue is catalogue, (
            "detaching an allow registration should keep the scoped catalogue"
        )


def test_scoped_catalogue_restores_context_after_exception() -> None:
    """scoped(catalogue=...) restores the previous context on exceptions."""
    catalogue = ProgramCatalogue.from_programs(ECHO)
    original = current_context()
    message = "catalogue scope failure"

    with (
        pytest.raises(ValueError, match=message),
        scoped(catalogue=catalogue),
    ):
        raise ValueError(message)

    assert current_context() is original, (
        "catalogue scope should restore the previous context after exceptions"
    )


# =============================================================================
# Property: scope nesting resolves to the innermost named catalogue
# =============================================================================


class _Step(typ.NamedTuple):
    """One generated scope step: what to open, and for which program."""

    kind: str
    program: Program


# A small program universe keeps generated scopes overlapping, so the
# nesting assertions observe real replacement rather than disjoint
# catalogues that would satisfy the invariant trivially. One catalogue
# instance per program is built once: ``ProgramCatalogue`` has no ``__eq__``,
# so the invariant is checked with ``is``, and comparing against a freshly
# built instance would fail on identity whatever the scopes did.
_PROGRAMS = (ECHO, LS)
_CATALOGUES = {
    program: ProgramCatalogue.from_programs(program) for program in _PROGRAMS
}

# One generated scope step. ``catalogue`` names a catalogue, ``plain`` opens
# an allowlist-only scope, and ``allow`` extends the active allowlist. All
# three derive a new context, so all three are places the catalogue could be
# dropped by mistake.
_SCOPE_STEPS = st.lists(
    st.builds(
        _Step,
        st.sampled_from(["catalogue", "plain", "allow"]),
        st.sampled_from(_PROGRAMS),
    ),
    max_size=8,
)

_PROPERTY_SETTINGS = settings(derandomize=True, deadline=None, max_examples=50)


def _expected_catalogue(steps: cabc.Sequence[_Step]) -> ProgramCatalogue | None:
    """Return the catalogue active after replaying ``steps``."""
    active: ProgramCatalogue | None = None
    for step in steps:
        if step.kind == "catalogue":
            active = _CATALOGUES[step.program]
    return active


def _open(step: _Step) -> contextlib.AbstractContextManager[object]:
    """Return the context manager that opens one generated scope step."""
    if step.kind == "catalogue":
        return scoped(catalogue=_CATALOGUES[step.program])
    if step.kind == "plain":
        return scoped(ScopeConfig(allowlist=frozenset([step.program])))
    return allow(step.program)


def _replay(
    steps: cabc.Sequence[_Step],
    remaining: cabc.Sequence[_Step] = (),
) -> None:
    """Recursively replay ``steps``, asserting the invariant at each depth."""
    if not steps:
        # The whole sequence is open: the innermost named catalogue wins.
        assert current_context().catalogue is _expected_catalogue(remaining), (
            "the active catalogue should be the innermost catalogue scope"
        )
        return
    step, rest = steps[0], steps[1:]
    with _open(step):
        _replay(rest, [*remaining, step])


@_PROPERTY_SETTINGS
@given(steps=_SCOPE_STEPS)
def test_catalogue_scope_nesting_resolves_to_the_innermost_named_catalogue(
    steps: list[_Step],
) -> None:
    """The active catalogue is the innermost scope that named one."""
    assert current_context().catalogue is None, "the default context is bare"
    _replay(steps)
    assert current_context().catalogue is None, (
        "every catalogue scope should be unwound after the sequence exits"
    )
