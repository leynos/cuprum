"""How a resolver that breaks its contract is reported to the caller.

``resolve_binding`` is the pure half of the binding feature and
``CuprumContext.resolve_executable`` is the half that knows which logical
program is being resolved. Both run caller-supplied resolver code, so both have
to decide what a resolver that fails reports. These examples pin that decision
for the three ways a resolver can fail: it returns something that is not a
``str``, it raises, or it raises the binding error itself.

The first case is the one with teeth. ``None`` is the sentinel the execution
layer reads as *unbound*, so a resolver returning it would send the child to
the catalogued name — a different executable. A resolver that returned ``None``
by accident would then be indistinguishable from one that deliberately
declined to bind, and the child would run a program the caller never chose.
Every other substitution in this feature is deliberate; this one must not be
reachable by accident.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import ScopeConfig, bind_executable, scoped, sh
from cuprum.catalogue import ECHO, ProgramCatalogue
from cuprum.context import CuprumContext
from cuprum.executable_binding import (
    ExecutableResolutionError,
    ExecutableResolver,
    executable_binding,
    resolve_binding,
)

if typ.TYPE_CHECKING:
    from cuprum.program import Program


def _returns(value: object) -> ExecutableResolver:
    """Wrap *value* as a resolver that produces it, contract notwithstanding."""
    return typ.cast("ExecutableResolver", lambda: value)


def _bound(
    resolver: ExecutableResolver,
    *,
    program: Program = ECHO,
) -> CuprumContext:
    """Return a context binding *program* to *resolver*."""
    return CuprumContext().with_executable_binding(
        program,
        executable_binding(program, resolver),
    )


# --------------------------------------------------------------------------
# A resolver that returns a non-string
# --------------------------------------------------------------------------


def test_a_none_result_is_rejected_rather_than_read_as_unbound() -> None:
    """``None`` may only ever mean *unbound*, never *this resolver's answer*.

    The failure mode this guards is silent and severe: the execution layer
    treats a ``None`` from resolution as *run the catalogued name*, so a
    resolver returning it would send the child to a different executable than
    the one the caller configured, with nothing to distinguish the accident
    from a deliberate fallback.
    """
    binding = executable_binding(ECHO, _returns(None))

    with pytest.raises(TypeError, match=r"must return str; got NoneType$"):
        resolve_binding(binding, cwd=None)


@pytest.mark.parametrize(
    ("value", "type_name"),
    [
        pytest.param(42, "int", id="int"),
        pytest.param(b"/bin/sh", "bytes", id="bytes"),
        pytest.param(["/bin/sh"], "list", id="list"),
    ],
)
def test_a_resolver_must_return_a_string(value: object, type_name: str) -> None:
    """Any other result is a contract violation, reported by its own type."""
    binding = executable_binding(ECHO, _returns(value))

    with pytest.raises(TypeError, match=f"got {type_name}$"):
        resolve_binding(binding, cwd=None)


def test_the_contract_violation_survives_the_context_entry_point() -> None:
    """The context reports the same violation, wrapped as a binding failure.

    ``None`` must not reach the caller as an executable by either route, so the
    check cannot live only in the pure half. Chaining keeps the original
    ``TypeError`` reachable through ``__cause__``.
    """
    with pytest.raises(ExecutableResolutionError) as excinfo:
        _bound(_returns(None)).resolve_executable(ECHO, cwd=None)

    assert isinstance(excinfo.value.__cause__, TypeError), (
        "the contract violation must be chained, not replaced, "
        f"found {excinfo.value.__cause__!r}"
    )


# --------------------------------------------------------------------------
# A resolver that raises
# --------------------------------------------------------------------------


def _missing_venv() -> str:
    """Fail the way a resolver does when the state it reads is absent."""
    raise FileNotFoundError("/opt/venvs/tools/bin/tool")


def test_a_raising_resolver_is_reported_as_a_binding_failure() -> None:
    """The caller gets a domain error naming the logical program."""
    with pytest.raises(ExecutableResolutionError) as excinfo:
        _bound(_missing_venv).resolve_executable(ECHO, cwd=None)

    assert excinfo.value.program == ECHO, (
        f"the error must name the program, found {excinfo.value.program!r}"
    )
    assert type(excinfo.value.cause).__name__ == "FileNotFoundError", (
        f"the error must carry the resolver's own failure, "
        f"found {excinfo.value.cause!r}"
    )


def test_the_error_message_names_the_program_not_the_absent_path() -> None:
    """A refusal stays in catalogue terms, not filesystem terms.

    The resolver's ``FileNotFoundError`` names a path the caller never
    configured; quoting it back would present an implementation detail as if it
    were part of the contract.
    """
    with pytest.raises(ExecutableResolutionError) as excinfo:
        _bound(_missing_venv).resolve_executable(ECHO, cwd=None)

    assert str(excinfo.value) == (
        "Program 'echo' cannot resolve its executable: FileNotFoundError"
    ), f"unexpected message: {str(excinfo.value)!r}"


def test_a_contract_violation_reaches_the_execution_boundary_wrapped() -> None:
    """A run reports a wrong-typed resolver the same way it reports a raising one.

    The documented contract is split by boundary, and only the outer half is
    reachable from a command a caller actually runs. ``resolve_binding`` raises
    ``TypeError`` for a non-``str``, but the execution boundary must not let
    that distinction through: a caller catching resolver failures around a run
    would otherwise need two handlers for one contract, and ``TypeError`` is
    exactly what unrelated programming errors in the surrounding code raise.
    """
    tool = ECHO
    catalogue = ProgramCatalogue.from_programs(
        tool,
        name="execution-boundary-failures",
        documentation_locations=("docs/users-guide.md",),
    )

    with (
        scoped(ScopeConfig(allowlist=frozenset([tool]))),
        bind_executable(tool, _returns(None)),
        pytest.raises(ExecutableResolutionError) as excinfo,
    ):
        sh.make(tool, catalogue=catalogue)().run_sync()

    assert not isinstance(excinfo.value, TypeError), (
        "the execution boundary must not surface the raw contract violation"
    )
    assert isinstance(excinfo.value.__cause__, TypeError), (
        f"the violation must be chained, found {excinfo.value.__cause__!r}"
    )


def test_the_original_exception_is_chained() -> None:
    """``__cause__`` preserves the traceback and the raising style."""
    with pytest.raises(ExecutableResolutionError) as excinfo:
        _bound(_missing_venv).resolve_executable(ECHO, cwd=None)

    assert isinstance(excinfo.value.__cause__, FileNotFoundError), (
        f"the resolver's exception must be chained, found {excinfo.value.__cause__!r}"
    )


def test_a_resolution_error_from_a_resolver_is_not_wrapped_twice() -> None:
    """A resolver that already reports a binding failure is taken at its word.

    Wrapping it again would replace an accurate message with
    ``cannot resolve its executable: ExecutableResolutionError``, which says
    nothing the caller did not already know.
    """
    raised = ExecutableResolutionError(ECHO, OSError("boom"))

    def resolver() -> str:
        """Fail with an error that is already a binding failure."""
        raise raised

    with pytest.raises(ExecutableResolutionError) as excinfo:
        _bound(resolver).resolve_executable(ECHO, cwd=None)

    assert excinfo.value is raised, (
        "the resolver's own binding error must propagate unchanged, "
        f"found {excinfo.value!r}"
    )
