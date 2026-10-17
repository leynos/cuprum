"""What the spawn seam is handed as its argument vector.

``_spawn_subprocess`` composes the vector from two sources: the observation's
``argv0`` and the command's own ``argv``. The distinction between those and
``SafeCmd.argv_with_program`` is easy to lose, because both read like "the
command's argument vector" and only one of them carries an executable binding.
A rewrite that swapped one for the other left every resolution and every
allowlist test green, and surfaced only as end-to-end binding failures; this
module asserts the composition at the seam those failures passed through, so
the next such swap is named by the test that fails rather than inferred from a
child that ran the wrong file.

The assertions here are on the value handed to ``spawn_direct_process``, not on
the child's behaviour, for the same reason ``test_process_group_policy`` reads
its flag off the config: the spawn call is where the two candidate expressions
differ, and a binding that reached the wrong one would otherwise have to be
diagnosed from the outside.
"""

from __future__ import annotations

import asyncio
import sys
import typing as typ

import pytest

import cuprum._wait4_spawn as wait4_spawn
from cuprum import Program, ScopeConfig, scoped, sh
from cuprum.catalogue import ProgramCatalogue
from cuprum.context.registration import bind_executable
from cuprum.sh import RunOutputOptions

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum._wait4_spawn import DirectProcessConfig


def _catalogue_for(name: str, executable: str) -> tuple[ProgramCatalogue, Program]:
    """Build a catalogue of one logical *name* backed by *executable*."""
    program = Program(name)
    catalogue = ProgramCatalogue.from_programs(
        program,
        name="spawn-argv-tests",
        documentation_locations=(
            "docs/users-guide.md#bind-a-catalogued-program-to-a-specific-executable",
        ),
    )
    return catalogue, program


def _capture_spawn_configs(
    monkeypatch: pytest.MonkeyPatch,
    recorded: list[DirectProcessConfig],
) -> None:
    """Record every config handed to the spawn into *recorded*, then spawn.

    The caller owns *recorded* rather than receiving it back, because the
    replacement appends to it after the patch is installed: a helper that
    returned its own list would have to be asked after the run, and a caller
    that copied it at installation time would copy nothing.
    """
    real_spawn = wait4_spawn.spawn_direct_process

    async def recording_spawn(
        config: DirectProcessConfig,
    ) -> asyncio.subprocess.Process:
        """Record the config, then delegate to the real spawn."""
        recorded.append(config)
        return await real_spawn(config)

    monkeypatch.setattr(wait4_spawn, "spawn_direct_process", recording_spawn)


def _write_script(path: Path, marker: str) -> Path:
    """Write an executable script reporting *marker* and its own ``argv[0]``."""
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write('{marker}:' + sys.argv[0] + '\\n')\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o100)
    return path


def test_the_spawn_argv_starts_with_the_bound_executable(tmp_path: Path) -> None:
    """A binding is the vector's first element, not the catalogued name.

    ``argv_with_program`` always starts with ``str(program)`` — the allowlist's
    logical name. Substituting it for the observation's ``argv0`` therefore
    silently drops the binding, which is what the comment on the spawn used to
    claim it preserved. The two differ exactly here, so this is the assertion
    that distinguishes them.

    The run is driven for real, so a recorded call is the production call
    rather than a stub's; the script it spawns need not exist for the vector to
    be recorded, but it does, so the run also succeeds.
    """
    catalogued = _write_script(tmp_path / "catalogued.py", "IMPOSTOR")
    approved = _write_script(tmp_path / "approved.py", "APPROVED")
    catalogue, tool = _catalogue_for("spawn-argv-tool", str(catalogued))
    recorded: list[DirectProcessConfig] = []

    async def run_case() -> None:
        """Run the bound command under a recording spawn."""
        with (
            scoped(ScopeConfig(allowlist=frozenset([tool]))),
            bind_executable(tool, str(approved)),
            pytest.MonkeyPatch.context() as patch,
        ):
            _capture_spawn_configs(patch, recorded)
            command = sh.make(tool, catalogue=catalogue)("--flag")
            await command.run(output=RunOutputOptions(capture=True, echo=False))

    asyncio.run(run_case())

    assert recorded, "the run must have spawned through the recorded seam"
    assert recorded[0].argv[0] == str(approved), (
        "the spawn's argv[0] must be the bound executable, not the catalogued "
        f"name; got {recorded[0].argv[0]!r}"
    )
    assert recorded[0].argv[1:] == ("--flag",), (
        "binding changes which file runs, never the arguments it is given; got "
        f"{recorded[0].argv[1:]!r}"
    )


def test_the_spawn_argv_is_the_catalogued_name_when_unbound() -> None:
    """Without a binding ``argv[0]`` is the program's own catalogued name.

    The negative control for the case above: it separates "the binding changed
    the vector" from "the vector is not the command's at all". An unbound
    program has no resolved path, and the fallback that fills ``argv0`` is the
    only thing that produces the name.
    """
    program = Program(sys.executable)
    catalogue = ProgramCatalogue.from_programs(
        program,
        name="spawn-argv-tests",
        documentation_locations=("docs/users-guide.md",),
    )
    recorded: list[DirectProcessConfig] = []

    async def run_case() -> None:
        """Run an unbound command under a recording spawn."""
        with (
            scoped(ScopeConfig(allowlist=frozenset([program]))),
            pytest.MonkeyPatch.context() as patch,
        ):
            _capture_spawn_configs(patch, recorded)
            command = sh.make(program, catalogue=catalogue)("-c", "pass")
            await command.run(output=RunOutputOptions(capture=True, echo=False))

    asyncio.run(run_case())

    assert recorded, "the run must have spawned through the recorded seam"
    assert recorded[0].argv[0] == sys.executable, (
        "an unbound program must spawn under its catalogued name; got "
        f"{recorded[0].argv[0]!r}"
    )
    assert recorded[0].argv[1:] == ("-c", "pass"), (
        f"the command's own arguments must follow unchanged; got {recorded[0].argv!r}"
    )
