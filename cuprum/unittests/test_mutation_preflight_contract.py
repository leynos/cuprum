"""Contract tests pinning the shared mutation environment preflight.

The caller workflow passes ``preflight: true``, so the shared mutation workflow
reads ``[tool.mutation-preflight]`` before mutmut enumerates a mutant. The
declaration repeats the audited ``also_copy`` inventory as ``sandbox_paths``,
which the shared helper checks against the copy contract of the mutmut release
the caller pins — a path the run would not materialize fails there as a named
finding rather than inside the sandbox.

The declaration also carries the one check that proves the Makefile's
root-relative pins reached a fresh sandbox copy, and the command the baseline
invokes. Both are pinned here so the early failure cannot be dropped silently.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from cuprum.unittests._mutation_config_support import (
    COPIED_INPUTS,
    preflight_table,
    string_list,
)


def test_preflight_declares_exactly_the_sandbox_inventory() -> None:
    """The shared preflight checks the same paths the sandbox must carry."""
    declared = {
        PurePosixPath(entry).as_posix()
        for entry in string_list(preflight_table(), "sandbox_paths")
    }
    inventoried = {PurePosixPath(copied.path).as_posix() for copied in COPIED_INPUTS}
    assert declared == inventoried, (
        "sandbox_paths must be the audited also_copy inventory, so the shared "
        f"preflight checks every copied input; missing: "
        f"{sorted(inventoried - declared)}; unexplained: "
        f"{sorted(declared - inventoried)}"
    )


def test_preflight_requires_the_make_executable() -> None:
    """The baseline dry-runs the Makefile, so make itself must resolve."""
    commands = preflight_table().get("require_commands")
    assert isinstance(commands, list), (
        f"require_commands must be a list of tables, got {commands!r}"
    )
    names = {command.get("name") for command in commands if isinstance(command, dict)}
    assert "make" in names, (
        "test_dev_fast_contract dry-runs the Makefile, so the preflight must "
        f"require make; declared commands: {sorted(str(name) for name in names)}"
    )


def test_preflight_dry_runs_the_makefile_before_enumeration() -> None:
    """The declared check is the dry run that reads every root-relative pin."""
    checks = preflight_table().get("checks")
    assert isinstance(checks, list), (
        f"the preflight checks must be a list, got {checks!r}"
    )
    assert len(checks) == 1, (
        "the preflight must declare exactly one check, so a failing baseline "
        f"has one bounded log to read; got {checks!r}"
    )
    check = checks[0]
    assert isinstance(check, dict), f"the check must be a table, got {check!r}"
    assert check.get("name") == "makefile-dry-run", (
        f"the declared check must be makefile-dry-run, got {check.get('name')!r}"
    )
    command = check.get("command")
    assert isinstance(command, list), f"the check needs a command list, got {command!r}"
    assert command[:2] == ["make", "--dry-run"], (
        "the check must dry-run the Makefile, which parses its root-relative "
        f"pins while loading; got {command!r}"
    )
