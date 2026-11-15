"""Fake shared tools and a Make runner for the install-target contracts.

The Make install targets delegate verifier binaries to the shared
`rust-prover-tools` dependency, so these helpers run the real Make recipes
against a fake CLI and a fake Cargo. That observes the exact command line, the
environment, and the ordering without network access, and it keeps the retired
local installers from creeping back. The same runner serves the boundary proof
target, whose recipe also resolves its own `cargo-kani` frontend.
"""

from __future__ import annotations

import dataclasses as dc
import json
import os
import shlex
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed tool argv.
import sys
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"

# The recipes must survive a checkout path that contains spaces, which is why
# every path argument is quoted and why the fixture runs make from one.
SPACED_ROOT_NAME = "work tree with spaces"

# Ambient names the shared CLI would otherwise treat as flag defaults.
AMBIENT_INSTALL_VARIABLES = (
    "INPUT_REPO_ROOT",
    "INPUT_VERSION",
    "INPUT_VERSION_FILE",
    "INPUT_KANI_VERSION_FILE",
    "INPUT_KANI_COMPILE",
    "KANI_INSTALL_DIR",
    "INPUT_KANI_INSTALL_DIR",
    "KANI_CACHE_DIR",
    "INPUT_KANI_CACHE_DIR",
    "Z3_INSTALL_DIR",
    "INPUT_Z3_INSTALL_DIR",
    "Z3_CACHE_DIR",
    "INPUT_Z3_CACHE_DIR",
    "VERUS_INSTALL_DIR",
    "INPUT_VERUS_INSTALL_DIR",
    "VERUS_TARGET",
    "INPUT_VERUS_TARGET",
    "INPUT_VERUS_VERSION_FILE",
    "INPUT_VERUS_BASE_URL",
    "INPUT_CHECKSUM_FILE",
    "INPUT_VERUS_CHECKSUM_FILE",
)

_FAKE_TOOL = """\
import json
import os
import sys
from pathlib import Path

record = {{"tool": {tool!r}, "argv": sys.argv[1:]}}
marker = os.environ.get("FAKE_MARKER")
if marker:
    record["marker_exists"] = Path(marker).exists()
watch = json.loads(os.environ.get("FAKE_WATCH", "[]"))
record["env"] = {{name: os.environ[name] for name in watch if name in os.environ}}
log = os.environ["FAKE_LOG"]
with open(log, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")
failure = os.environ.get("FAKE_FAIL_ARGS")
if failure and sys.argv[1:3] == json.loads(failure):
    print("fake prover-tools failure", file=sys.stderr)
    raise SystemExit(1)
"""

# The version check runs through `uv tool run`, so a fake `uv` on PATH can
# record which `cargo-kani` the environment it receives would select. That is
# the frontend the proof commands that follow would run.
FAKE_UV = """\
#!/bin/sh
{
  printf 'resolved=%s\\n' "$(command -v cargo-kani)"
  printf 'path=%s\\n' "$PATH"
} > "$FAKE_RESOLVE_LOG"
"""

# A competing frontend stays dormant unless something executes it, so its log
# only ever records an identity if it was actually selected.
FAKE_FRONTEND = """\
#!/bin/sh
printf '%s\\n' "{identity}" >> "$FAKE_FRONTEND_LOG"
"""


@typ.final
class FakeTools:
    """Fake `prover-tools` and Cargo executables that record their calls."""

    def __init__(self, directory: Path) -> None:
        """Write both fakes below `directory` and prepare their log path."""
        self.directory = directory
        self.directory.mkdir(parents=True)
        self.log = directory / "calls.jsonl"
        self.prover_tools = self._write("fake_prover_tools.py", "prover-tools")
        self.cargo = self._write("fake_cargo.py", "cargo")

    def _write(self, name: str, tool: str) -> Path:
        """Write one fake tool and return its path."""
        path = self.directory / name
        path.write_text(_FAKE_TOOL.format(tool=tool), encoding="utf-8")
        return path

    @staticmethod
    def command(script: Path) -> str:
        """Return a shell-safe command that runs `script` with this interpreter."""
        return shlex.join([sys.executable, str(script)])

    def records(self) -> list[dict[str, object]]:
        """Return every recorded call in execution order."""
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def environment(self, **overrides: str) -> dict[str, str]:
        """Return a child environment that points the fakes at their log."""
        env = os.environ.copy()
        env.update(
            FAKE_LOG=str(self.log),
            FAKE_WATCH=json.dumps(list(AMBIENT_INSTALL_VARIABLES)),
        )
        env.update(overrides)
        return env


def makefile_assignment(name: str) -> str:
    """Return the literal value assigned to `name` in the Makefile.

    Returns
    -------
    str
        The value after the assignment operator, with surrounding space
        removed.

    Raises
    ------
    AssertionError
        If the Makefile assigns no such name, which means the recipe this
        helper reads has been renamed or removed.
    """
    prefix = f"{name} ?="
    prefix_plain = f"{name} ="
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
        if line.startswith(prefix_plain):
            return line[len(prefix_plain) :].strip()
    msg = f"{name} is not assigned in the Makefile"
    raise AssertionError(msg)


def recorded_argv(record: dict[str, object]) -> list[str]:
    """Return one recorded argv, failing the test if the fake logged another shape.

    Returns
    -------
    list[str]
        The recorded argument vector as text.

    Raises
    ------
    TypeError
        If the fake logged something other than a list of arguments, so a
        malformed record fails the case rather than being read as no call.
    """
    argv = record["argv"]
    if not isinstance(argv, list):
        msg = f"the fake logged a non-list argv: {argv!r}"
        raise TypeError(msg)
    return [str(argument) for argument in argv]


@dc.dataclass(frozen=True, slots=True)
class MakeCall:
    """One Make target with the assignments and environment it must receive."""

    target: str
    assignments: tuple[str, ...] = ()
    environment: cabc.Mapping[str, str] = dc.field(default_factory=dict)


def run_make(
    workdir: Path,
    call: MakeCall,
    fakes: FakeTools,
    *,
    delegate: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one Make target in `workdir` with the fake tools in place.

    `delegate` supplies the fake `prover-tools`. A test that observes how the
    recipe itself resolves the tool passes `delegate=False` instead, because a
    `PROVER_TOOLS` assignment would replace the command whose environment is
    under test.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The finished Make process with its captured output.
    """
    command = [
        shutil.which("make") or "make",
        "-C",
        str(workdir),
        "-f",
        str(MAKEFILE),
        call.target,
        f"CARGO={fakes.command(fakes.cargo)}",
    ]
    if delegate:
        command.append(f"PROVER_TOOLS={fakes.command(fakes.prover_tools)}")
    command.extend(call.assignments)
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        command,
        capture_output=True,
        text=True,
        env=fakes.environment(**dict(call.environment)),
        timeout=120,
        check=False,
    )


def ambient_guard_names() -> list[str]:
    """Return the names `PROVER_TOOLS_AMBIENT_VARS` clears before delegation.

    Returns
    -------
    list[str]
        Every variable name the Makefile guard names, in declaration order.
    """
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    index = next(
        position
        for position, line in enumerate(lines)
        if line.startswith("PROVER_TOOLS_AMBIENT_VARS =")
    )
    value = lines[index].partition("=")[2]
    while value.rstrip().endswith("\\"):
        index += 1
        value = f"{value.rstrip()[:-1]} {lines[index]}"
    return value.split()


def write_executable(path: Path, text: str) -> Path:
    """Write `text` to `path` and mark the result executable.

    Returns
    -------
    Path
        The path just written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def recorded_values(log: Path) -> dict[str, str]:
    """Parse the fake `uv` record into its keyed values.

    Returns
    -------
    dict[str, str]
        One entry per `key=value` line the fake wrote.
    """
    lines = log.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)
