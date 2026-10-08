"""Read how a workflow job provisions makeutil.

CI installs makeutil through the shared prebuilt `install-makeutil` action and
verifies it in the next step. The helpers here hold both halves, so the
contract in `tests/test_ci_makeutil_install.py` stays short and every job is
held to the same shape.

The install step carries no `run` or `continue-on-error` and a `with` of
`bin-dir` alone, so the version is the action's own default and a from-source
install cannot creep back. `bin-dir` sits under the runner's temporary
directory because the action's default, `~/.local/bin`, is archived by the
tool cache. The verify step compares the binary's version with the one the
action reports and requires a complete parse of the repository `Makefile`; it
never names a version. In `typecheck-test` both steps end with the leg flag, which
:func:`tests.helpers.ci_leg_gate.ungated` strips exactly, so no other guard can
hide behind it.

This repository runs no Python module doctests, so the examples are
documentation rather than executed assertions.
"""

from __future__ import annotations

import re
import typing as typ

from tests.helpers.ci_leg_gate import ungated

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Step

#: The shared action, pinned by commit. It defaults to the version it ships.
INSTALL_ACTION: typ.Final = (
    "leynos/shared-actions/.github/actions/install-makeutil"
    "@ebe2f3105334283441f4c915eb979a0f4b9f2c04"
)
INSTALL_STEP: typ.Final = "Install makeutil"
VERIFY_STEP: typ.Final = "Verify makeutil"
#: What the verify step reads from the install step, which carries this id.
INSTALL_ID: typ.Final = "makeutil"
#: Where the action puts the binary: outside ``~/.local/bin``, which the tool
#: cache archives, so the tool archive never carries makeutil.
BIN_DIR: typ.Final = "${{ runner.temp }}/makeutil/bin"
VERSION_OUTPUT: typ.Final = "${{ steps.makeutil.outputs.version }}"

#: The verify script, as the shell would run it: strict mode, the version
#: comparison, and a complete parse of the repository Makefile.
EXPECTED_SCRIPT: typ.Final = (
    "set -euo pipefail",
    'test "$(makeutil --version)" = "makeutil ${INSTALLED_VERSION}"',
    (
        "makeutil parse Makefile | python3 -c "
        "'import json, sys; "
        'assert json.load(sys.stdin)["parse"]["status"] == "complete"\''
    ),
)

_VERSION_LITERAL: typ.Final = re.compile(r"\d+\.\d+\.\d+")
#: Keys that could skip a step or swallow its failure.
_DISABLING_KEYS: typ.Final = frozenset({"continue-on-error"})


def executable_lines(script: str) -> tuple[str, ...]:
    r"""Return a script's commands, joining continuations and dropping comments.

    A check that is commented out, or sits inside a conditional, must not count
    as a check, so the script is reduced to the lines the shell would run.

    Parameters
    ----------
    script : str
        The step's `run` text.

    Returns
    -------
    tuple[str, ...]
        One whitespace-normalized string per command line.

    Examples
    --------
    >>> executable_lines("# note\nset -e\n\nfoo \\\n  bar")
    ('set -e', 'foo bar')
    """
    joined = script.replace("\\\n", " ")
    lines = (" ".join(line.split()) for line in joined.splitlines())
    return tuple(line for line in lines if line and not line.startswith("#"))


def _require(condition: object, message: str) -> None:
    """Fail with ``message`` unless ``condition`` holds."""
    if not condition:
        raise AssertionError(message)


def _guard(workflow_name: str, job_name: str, step: Step) -> str:
    """Return the step's guard without the leg flag."""
    return ungated(workflow_name, job_name, step.get("if"))


def assert_installation(
    workflow_name: str, job_name: str, step: Step, *, contract: str
) -> None:
    """Assert that ``step`` runs the pinned install action, defaults only.

    Raises ``AssertionError`` if the step does not use the pinned action, has
    no `id`, carries a `run` key, sets anything but `bin-dir` or puts it
    elsewhere, can fail without failing the job, or has any guard beyond the
    job's leg flag.

    Parameters
    ----------
    workflow_name : str
        The workflow file name, such as ``"ci.yml"``.
    job_name : str
        The job the step belongs to.
    step : Step
        The parsed step named "Install makeutil".
    contract : str
        Names the workflow and job, so a failure says where it happened.

    Examples
    --------
    >>> assert_installation("coverage-main.yml", "coverage-upload", {}, contract="c")
    Traceback (most recent call last):
    ...
    AssertionError: c must use the pinned install-makeutil action
    """
    _require(
        step.get("uses") == INSTALL_ACTION,
        f"{contract} must use the pinned install-makeutil action",
    )
    _require(step.get("id") == INSTALL_ID, f"{contract} install step needs an id")
    _require("run" not in step, f"{contract} must not also run an install command")
    _require(
        step.get("with") == {"bin-dir": BIN_DIR},
        f"{contract} must set only bin-dir, outside the tool cache, and so "
        "take the action's default version",
    )
    _require(
        not _DISABLING_KEYS & step.keys(),
        f"{contract} install must fail the job when it fails",
    )
    _require(
        not _guard(workflow_name, job_name, step),
        f"{contract} install must run on every leg the job runs, with no guard",
    )


def assert_verification(
    workflow_name: str, job_name: str, step: Step, *, contract: str
) -> None:
    """Assert the step that proves the installed binary is usable.

    Raises ``AssertionError`` if the step is guarded beyond the leg flag, can
    fail without failing the job, does not read the version the action reports,
    runs anything but the expected commands, or names a literal version.

    Parameters
    ----------
    workflow_name : str
        The workflow file name, such as ``"ci.yml"``.
    job_name : str
        The job the step belongs to.
    step : Step
        The parsed step named "Verify makeutil".
    contract : str
        Names the workflow and job, so a failure says where it happened.

    Examples
    --------
    >>> assert_verification("coverage-main.yml", "coverage-upload", {}, contract="c")
    Traceback (most recent call last):
    ...
    AssertionError: c verify step needs an env
    """
    _require(
        not _guard(workflow_name, job_name, step),
        f"{contract} verify must run on every leg the job runs, with no guard",
    )
    _require(
        not _DISABLING_KEYS & step.keys(),
        f"{contract} verify must fail the job when it fails",
    )
    environment = step.get("env")
    _require(isinstance(environment, dict), f"{contract} verify step needs an env")
    _require(
        typ.cast("dict[str, object]", environment).get("INSTALLED_VERSION")
        == VERSION_OUTPUT,
        f"{contract} must read the version the install action reports",
    )
    script = step.get("run")
    _require(isinstance(script, str), f"{contract} must run a verification script")
    text = typ.cast("str", script)
    _require(
        executable_lines(text) == EXPECTED_SCRIPT,
        (
            f"{contract} must run exactly the checks it names, "
            "uncommented and unconditional"
        ),
    )
    _require(
        not _VERSION_LITERAL.search(text),
        f"{contract} must compare versions, never name one",
    )


def assert_verification_follows_install(
    job_steps: list[Step], *, contract: str
) -> None:
    """Assert the verify step directly follows the install step.

    Raises ``AssertionError`` if there is no install step, or the step after it
    is not the verify step.

    Parameters
    ----------
    job_steps : list[Step]
        The job's parsed steps, in order.
    contract : str
        Names the workflow and job, so a failure says where it happened.

    Examples
    --------
    >>> assert_verification_follows_install(
    ...     [{"name": "Install makeutil"}, {"name": "Verify makeutil"}], contract="c"
    ... )
    """
    names = [step.get("name") for step in job_steps]
    _require(INSTALL_STEP in names, f"{contract} must have an install step")
    following = names[names.index(INSTALL_STEP) + 1 :][:1]
    _require(
        following == [VERIFY_STEP],
        f"{contract} must verify makeutil right after installing it",
    )
