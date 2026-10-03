"""Contracts for the 3.13 typecheck that moved into `extension-tests`.

The interpreter matrix no longer has a 3.13 leg: the coverage job runs that
interpreter's suite, and `extension-tests` runs its typechecker after the gated
modules. A step that runs `make typecheck` proves nothing about the
interpreter, though. Changing the job's Python to 3.12 would keep every
presence check green while the 3.13 typecheck silently disappeared. These
contracts bind the typecheck to the interpreter the job sets up. They also
require the retired `python-suite` matrix key to stay gone, because a guard
reading a key no leg declares renders empty and switches its step off on every
leg.
"""

from __future__ import annotations

import typing as typ

from tests.helpers.ci_runners import job, steps

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from tests.helpers.workflow_types import Step

EXTENSION_JOB: typ.Final = ("ci.yml", "extension-tests")
#: The interpreter the coverage job runs and `extension-tests` typechecks.
COVERED_PYTHON_VERSION: typ.Final = "3.13"
SETUP_PYTHON: typ.Final = "actions/setup-python@"
TYPECHECK: typ.Final = "make typecheck"
EXTENSION_SUITE: typ.Final = "make test-extension"


def _run(step: Step) -> str:
    """Return a step's command with its whitespace collapsed."""
    return " ".join(str(step.get("run", "")).split())


def _one(
    job_steps: list[Step], what: str, predicate: cabc.Callable[[Step], bool]
) -> int:
    """Return the index of the one step matching ``predicate``."""
    matches = [index for index, step in enumerate(job_steps) if predicate(step)]
    assert len(matches) == 1, (
        f"extension-tests: expected one {what}, found {len(matches)}"
    )
    return matches[0]


def test_the_typecheck_runs_on_the_covered_interpreter() -> None:
    """One setup-python on 3.13 precedes the typecheck, which follows the suite.

    Pinning the version, not only the step's presence, is what makes the move
    safe: the typecheck is the only 3.13 typecheck left in CI.
    """
    job_steps = steps(*EXTENSION_JOB)
    setup = _one(
        job_steps,
        "setup-python step",
        lambda step: str(step.get("uses", "")).startswith(SETUP_PYTHON),
    )
    inputs = job_steps[setup].get("with")
    assert isinstance(inputs, dict), "extension-tests' setup-python must declare inputs"
    version = str(typ.cast("dict[str, object]", inputs).get("python-version"))
    assert version == COVERED_PYTHON_VERSION, (
        f"extension-tests must set up Python {COVERED_PYTHON_VERSION}, got {version!r}"
    )
    suite = _one(job_steps, EXTENSION_SUITE, lambda step: _run(step) == EXTENSION_SUITE)
    typecheck = _one(job_steps, TYPECHECK, lambda step: _run(step) == TYPECHECK)
    assert setup < suite < typecheck, (
        "extension-tests must set up Python, run the gated modules, then typecheck"
    )
    assert "if" not in job_steps[typecheck], "the 3.13 typecheck must not be guarded"


def test_no_matrix_leg_declares_the_retired_suite_flag() -> None:
    """Every leg runs its suite now, so the flag that switched one off is gone."""
    strategy = job("ci.yml", "typecheck-test").get("strategy")
    assert isinstance(strategy, dict), "typecheck-test must declare a strategy"
    matrix = strategy.get("matrix")
    assert isinstance(matrix, dict), "typecheck-test must declare a matrix"
    include = matrix.get("include")
    assert isinstance(include, list), "typecheck-test must list its legs"
    assert include, "typecheck-test must have legs"
    stale = [leg for leg in include if "python-suite" in leg]
    assert not stale, f"matrix legs still declare python-suite: {stale}"
