"""Scoped executable-binding steps for the catalogue behaviour suite.

``test_catalogue_behaviour`` declares the scenarios; the steps that exercise
executable bindings live here so that module stays inside the repository's
module-length budget. The steps register themselves through their own
decorators at import time and are re-bound by the importing module.
"""

from __future__ import annotations

import sys
import typing as typ

import pytest
from pytest_bdd import parsers, then, when

import cuprum as c
from cuprum import ScopeConfig, scoped, sh
from cuprum.catalogue import ProgramCatalogue
from cuprum.context.registration import bind_executable
from cuprum.program import Program

if typ.TYPE_CHECKING:
    from cuprum.sh import CommandResult

# The scenario's own catalogue, distinct from the default one so the steps can
# allowlist exactly the program under test. The entry carries the documentation
# location the feature's sibling scenarios advertise, which keeps the fixture
# honest about being a real catalogue rather than a test double.
_SCENARIO_DOCS = "docs/users-guide.md#executable-bindings"

# Each binding step enters and leaves its own ``scoped`` block around the whole
# execution. A scenario has no teardown hook a step can register into, so a
# scope opened in one step could not be reliably unwound in a later one; doing
# the work inside a single step keeps the narrowing's lifetime the step's own,
# and keeps the binding from outliving the scenario that installed it.


def _require(*, condition: bool, message: str) -> None:
    """Fail a behaviour step when its required condition is false.

    This module is not named ``test_*``, so ruff's ``assert`` exemption for
    test modules does not reach it; ``pytest.fail`` carries the same failure
    with the same message.
    """
    if not condition:
        pytest.fail(message)


def _scenario_catalogue(program: Program) -> ProgramCatalogue:
    """Build a catalogue whose only entry is ``program``.

    Returns
    -------
    ProgramCatalogue
        A catalogue admitting ``program`` under a name of its own.
    """
    return ProgramCatalogue.from_programs(
        program,
        name="catalogue-behaviour-bindings",
        documentation_locations=(_SCENARIO_DOCS,),
    )


@when(
    parsers.parse('I bind the program "{program_name}" to the running interpreter'),
    target_fixture="binding_outcome",
)
def when_bind_program_to_executable(
    program_name: str,
) -> dict[str, object]:
    """Run ``program_name`` inside a scope that binds it to another executable.

    The bound executable is the running interpreter: a real, executable file
    whose path differs from the catalogued program's name, so a regression that
    ran the catalogued name instead would be visible in the child's own report.
    The scenario names the interpreter rather than a literal path because the
    executable has to exist on whatever machine runs the suite, and the
    interpreter is the one executable guaranteed to.

    Both halves of the claim are returned: ``resolved_path`` is what the
    library says ran, and the child's own report is what actually did. Checking
    only the first could not tell the two apart.

    Parameters
    ----------
    program_name : str
        The logical program name captured from the scenario step text.

    Returns
    -------
    dict[str, object]
        The run's ``result`` and the ``executable`` it was bound to.
    """
    program = Program(program_name)
    bound = sys.executable
    with (
        scoped(ScopeConfig(allowlist=frozenset([program]))),
        bind_executable(program, bound),
    ):
        # ``sh.make`` returns a builder; calling it with no arguments is what
        # produces the command, matching how the unit tests drive it.
        cmd = sh.make(program, catalogue=_scenario_catalogue(program))(
            "-c", "import sys; sys.stdout.write(sys.executable)"
        )
        result = cmd.run_sync(output=c.RunOutputOptions(capture=True, echo=False))
    return {"result": result, "executable": bound}


@when(
    parsers.parse(
        'I bind the unlisted program "{program_name}" to the executable "{path}"',
    ),
    target_fixture="refusal_outcome",
)
def when_bind_unlisted_program(
    program_name: str,
    path: str,
    curated_program: Program,
) -> dict[str, object]:
    """Attempt to run an unlisted program that carries a binding.

    ``curated_program`` is allowlisted and ``program_name`` is not, so the
    refusal under test is the allowlist's rather than the catalogue's. The
    binding is a counting resolver, because the ordering claim is not only
    *that* the run is refused but that resolution never happened: a resolver
    that ran would have started the executable before the decision was made.

    Parameters
    ----------
    program_name : str
        The unlisted program name captured from the scenario step text.
    path : str
        The path the counting resolver would return if it were called.
    curated_program : Program
        The allowlisted program that must keep its authority.

    Returns
    -------
    dict[str, object]
        The raised ``error``, if any, and the number of ``calls`` made.
    """
    calls: list[int] = []

    def resolver() -> str:
        """Record the call and return a path that must never be executed."""
        calls.append(len(calls))
        return path

    outcome: dict[str, object] = {"calls": 0}
    with (
        scoped(ScopeConfig(allowlist=frozenset([curated_program]))),
        bind_executable(Program(program_name), resolver),
    ):
        try:
            cmd = sh.make(
                Program(program_name),
                catalogue=_scenario_catalogue(Program(program_name)),
            )()
            cmd.run_sync(output=c.RunOutputOptions(capture=True, echo=False))
        except c.ForbiddenProgramError as exc:
            outcome["error"] = exc
    outcome["calls"] = len(calls)
    return outcome


@then("the bound executable runs and reports itself")
def then_bound_executable_runs(binding_outcome: dict[str, object]) -> None:
    """Assert the child ran the bound file and reported that exact path."""
    result = typ.cast("CommandResult", binding_outcome["result"])
    executable = typ.cast("str", binding_outcome["executable"])

    _require(
        condition=result.stdout is not None,
        message=(
            f"the bound script must report its interpreter, got "
            f"stdout={result.stdout!r}"
        ),
    )
    reported = typ.cast("str", result.stdout).strip()
    _require(
        condition=reported == executable,
        message=(
            f"the child must have been started as the bound file, reported "
            f"{reported!r} but bound {executable!r}"
        ),
    )
    _require(
        condition=result.resolved_path == executable,
        message=(
            f"the run must report the bound executable, got {result.resolved_path!r}"
        ),
    )


@then(parsers.parse('the logical program remains "{program_name}"'))
def then_logical_program_is_preserved(
    binding_outcome: dict[str, object],
    program_name: str,
) -> None:
    """Assert the binding changed the executable, not the identity."""
    result = typ.cast("CommandResult", binding_outcome["result"])
    _require(
        condition=result.program == Program(program_name),
        message=(
            f"the result must still carry the logical program, got {result.program!r}"
        ),
    )


@then("execution is refused as a forbidden program")
def then_execution_is_refused(refusal_outcome: dict[str, object]) -> None:
    """Assert the allowlist still decides first."""
    error = refusal_outcome.get("error")
    _require(
        condition=isinstance(error, c.ForbiddenProgramError),
        message=f"an unlisted program must be refused, got {error!r}",
    )


@then("the binding's executable was never resolved")
def then_binding_was_never_resolved(refusal_outcome: dict[str, object]) -> None:
    """Assert resolution never preceded the refusal.

    The count is read as a number rather than as truthiness, so the failure
    message names how many times the resolver ran instead of merely calling the
    result empty.
    """
    calls = typ.cast("int", refusal_outcome["calls"])
    _require(
        condition=calls == 0,
        message=(
            f"a resolver must not run for a program the allowlist refuses, ran "
            f"{calls} times"
        ),
    )
