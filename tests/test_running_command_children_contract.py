"""Contract tests for the handle scenarios' shared child builders.

The builders in :mod:`tests.behaviour._running_command_children` write the
scripts every handle scenario drives, and the scenarios assert against the
statuses those scripts exit with. The two shared statuses are deliberately
distinct — neither is zero, and neither equals the other — so a scenario that
reports the wrong one cannot pass by coincidence.

That guarantee is only worth anything if the builder actually produces the
status its caller asked for. The output-writing variant is the one at risk,
because its script must emit a marker *and* choose a status, and the status was
once hard-coded into it: a caller naming one status received another, and every
scenario still passed, because the sole caller happened to name the hard-coded
value. These cases pin the parameter against a real child rather than against
the script text, so the substitution cannot silently stop working.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import ScopeConfig, scoped
from tests.behaviour._running_command_children import (
    _EARLY_EXIT_CODE,
    _OUTPUT_EXIT_CODE,
    _exit_code_command,
)
from tests.helpers.catalogue import python_catalogue
from tests.helpers.timeouts import CHILD_STDOUT

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.sh import CommandResult, SafeCmd

#: A status that is neither of the shared ones, so a builder that ignored its
#: argument cannot report it by falling back to a default.
_BESPOKE_EXIT_CODE = 21


def _run_sync(command: SafeCmd) -> CommandResult:
    """Run *command* inside a scope allowing the interpreter it drives."""
    _, python_program = python_catalogue()
    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        return command.run_sync()


@pytest.mark.parametrize("writes", [False, True])
def test_the_builder_honours_the_status_it_was_given(
    tmp_path: Path, *, writes: bool
) -> None:
    """A bespoke status reaches the caller, whether or not the child writes.

    Both variants are asserted because they take different script templates,
    and it is the output-writing template that once ignored its argument. The
    bespoke value is what makes the case meaningful: asserting the shared
    statuses would pass for a builder that ignored its argument entirely and
    hard-coded one of them.
    """
    command = _exit_code_command(tmp_path, _BESPOKE_EXIT_CODE, writes=writes)

    result = _run_sync(command)

    assert result.exit_code == _BESPOKE_EXIT_CODE, (
        "the child must exit with the status it was built for "
        f"({_BESPOKE_EXIT_CODE}), got {result.exit_code}"
    )
    if writes:
        assert CHILD_STDOUT in (result.stdout or ""), (
            "the output-writing variant must still write its marker, got "
            f"{result.stdout!r}"
        )


def test_the_two_shared_statuses_stay_distinct() -> None:
    """The shared statuses differ from each other and from a clean exit.

    A scenario asserts the status its own child produced, so the two have to be
    distinguishable: making them equal would let a child built for one scenario
    satisfy the other's assertion, and a zero would let any clean exit pass for
    a deliberately non-zero one.
    """
    assert _OUTPUT_EXIT_CODE != _EARLY_EXIT_CODE, (
        "the two shared statuses must differ, otherwise one scenario's "
        "assertion is satisfied by the other's child"
    )
    assert 0 not in {_OUTPUT_EXIT_CODE, _EARLY_EXIT_CODE}, (
        "a shared status must be non-zero, otherwise a clean exit passes for "
        "a deliberately non-zero one"
    )
