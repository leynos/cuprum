"""Contract tests pinning the mutmut test selection and sandbox profile.

The mutation run collects both suite roots and omits the test modules that
cannot run against a mutated ``cuprum``: two build an environment of their own,
two read the scheduled coroutine through mutmut's async trampoline, and three
are the ``act`` scenarios that ``make test`` runs only from ``test-act``. The
exclusion is pinned here so it cannot widen silently, and each excluded module
is checked to still exist inside the selected suite, so the exclusion cannot
hide a deletion.

The run also loads a Hypothesis profile that relaxes one harness-specific
health check: mutmut reruns the suite in one process, so a class-based
``@given`` test is seen from several executors even though it is correct. The
profile is asserted to inherit the suite settings and suppress exactly that
check, so the relaxation cannot widen into the ordinary suite.

The root ``conftest.py`` must also import without importing ``cuprum``. mutmut
verifies its setup by arming the mutated package to raise from the first
trampolined call, and an exception escaping the run is that probe's expected
outcome. An import in the conftest body would instead be reported as a pytest
usage error, which mutmut refuses and aborts the whole run on.

The copied-input inventory these selection tests do not touch is pinned by
``test_mutmut_config_contract.py``.
"""

from __future__ import annotations

import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell
import sys
from pathlib import PurePosixPath

import pytest
from hypothesis import HealthCheck, settings

from cuprum.unittests._mutation_config_support import mutmut_table, string_list
from tests.helpers.docs import repo_root

#: Suite roots the mutation run collects.
_SELECTED_ROOTS: tuple[str, ...] = ("tests/", "cuprum/unittests/")

#: Test modules the mutation run must not collect.
#:
#: Two of them construct an environment inside the test: the nextest-route
#: tests drive ``uv run --frozen``, and the typing contract points ty at
#: ``sys.prefix``. mutmut rewrites every sandboxed module to import ``mutmut``
#: and installs itself only in an ephemeral ``uv run --with`` overlay, so
#: neither constructed environment can import the mutated ``cuprum``.
#:
#: Two more compare what the observe-hook dispatcher scheduled by reading the
#: task's coroutine code object and a frame local. mutmut's async trampoline
#: creates its own coroutine, so the oracle sees the trampoline rather than the
#: dispatcher and cannot read the original frame at all. The named examples
#: beside them stay selected: their attribution predicate recognizes the
#: trampoline's renamed copies.
#:
#: The three ``act`` scenarios need a container runtime. ``make test`` already
#: runs them only from ``test-act``, so collecting them here would make the
#: mutation baseline depend on whether the host has ``act`` installed, and on
#: scenario expectations that mutate no ``cuprum`` source. The recorded-output
#: parser beside them is plain string handling and stays selected.
#:
#: The assertions stay in the suite; only the mutation selection skips them.
_SANDBOX_EXCLUDED_MODULES: tuple[str, ...] = (
    "cuprum/unittests/test_nextest_route_floor.py",
    "cuprum/unittests/test_sh_typing_contract.py",
    "cuprum/unittests/test_observe_hook_dispatch_properties.py",
    "cuprum/unittests/test_observe_hook_dispatch_seeded_faults.py",
    "tests/integration/test_workflow_integration.py",
    "tests/integration/test_experimental_leg_integration.py",
    "tests/integration/test_extension_typecheck_integration.py",
)

#: Hypothesis profile every mutmut pytest invocation loads.
#:
#: mutmut reruns the suite in a single process, so Hypothesis raises its
#: differing-executors health check for class-based property tests. The profile
#: is registered in the root ``conftest.py`` and must relax that one check only.
_SANDBOX_HYPOTHESIS_PROFILE: str = "mutmut-sandbox"


def _test_selection() -> list[str]:
    """Return the mutation run's pytest selection arguments."""
    return string_list(mutmut_table(), "pytest_add_cli_args_test_selection")


def _ignored_paths(selection: list[str]) -> set[str]:
    """Return the paths named by ``--ignore=`` entries in ``selection``."""
    return {
        entry.removeprefix("--ignore=")
        for entry in selection
        if entry.startswith("--ignore=")
    }


def test_test_selection_still_collects_both_suite_roots() -> None:
    """Every suite root is collected, so an exclusion cannot shrink the run."""
    selection = _test_selection()
    missing = [root for root in _SELECTED_ROOTS if root not in selection]
    assert not missing, f"the mutation run must still collect {missing}"


def test_mutation_run_ignores_exactly_the_pinned_modules() -> None:
    """The sandbox exclusions are the pinned modules and nothing else."""
    ignored = _ignored_paths(_test_selection())
    unexpected = sorted(ignored - set(_SANDBOX_EXCLUDED_MODULES))
    missing = sorted(set(_SANDBOX_EXCLUDED_MODULES) - ignored)
    assert not unexpected, (
        "the mutation run must not ignore modules beyond the ones that cannot "
        f"run against a mutated cuprum; unexpected: {unexpected}"
    )
    assert not missing, (
        "every module that cannot run against a mutated cuprum must stay "
        f"excluded; missing: {missing}"
    )


def test_mutation_run_loads_the_sandbox_hypothesis_profile() -> None:
    """Every mutmut pytest invocation selects the sandbox profile."""
    args = string_list(mutmut_table(), "pytest_add_cli_args")
    assert args == [f"--hypothesis-profile={_SANDBOX_HYPOTHESIS_PROFILE}"], (
        "mutmut must load the sandbox Hypothesis profile on every run; "
        f"expected exactly the profile flag, got {args}"
    )


def test_sandbox_hypothesis_profile_relaxes_only_the_harness_check() -> None:
    """The profile inherits the suite settings and suppresses one check."""
    profile = settings.get_profile(_SANDBOX_HYPOTHESIS_PROFILE)
    assert profile.suppress_health_check == (HealthCheck.differing_executors,), (
        "the sandbox profile must suppress only the differing-executors "
        f"check, got {profile.suppress_health_check}"
    )
    assert profile.deadline is None, (
        "the sandbox profile must inherit the suite profile's unlimited "
        "deadline; registering it before the suite profile would silently "
        "restore Hypothesis's default deadline"
    )


def test_root_conftest_imports_without_the_package_under_test() -> None:
    """Importing the root conftest must not import ``cuprum``."""
    probe = (
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "import conftest\n"
        "print('cuprum' in sys.modules)\n"
    )
    # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv: the
    # running interpreter and a literal probe script, with no shell.
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"importing the root conftest must succeed, got exit "
        f"{result.returncode}: {result.stderr}"
    )
    assert result.stdout.strip() == "False", (
        "the root conftest must not import cuprum: mutmut's forced-fail probe "
        "runs with the mutated package armed to raise, and a conftest import "
        "failure is a pytest usage error that mutmut refuses; move the import "
        "into the fixture that needs it or declare its module in "
        "pytest_plugins"
    )


@pytest.mark.parametrize("module", _SANDBOX_EXCLUDED_MODULES)
def test_ignored_module_is_a_selected_test_module(module: str) -> None:
    """An ignored module still exists under a collected suite root."""
    path = repo_root() / module
    assert path.is_file(), (
        f"{module} is ignored by the mutation run but no longer exists; the "
        "exclusion must not outlive the module or hide its deletion"
    )
    is_under_selected_root = any(
        PurePosixPath(module).is_relative_to(root) for root in _SELECTED_ROOTS
    )
    assert is_under_selected_root, f"{module} is not under a collected suite root"
