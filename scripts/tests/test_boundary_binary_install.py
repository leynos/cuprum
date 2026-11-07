"""The Make install targets must delegate verifier binaries safely.

Kani and Z3 installation is owned by the shared `rust-prover-tools` dependency,
so these tests run the real Make recipes with a fake `prover-tools` and a fake
Cargo. That observes the exact command line, environment, and ordering without
network access, and it keeps the retired local installers from creeping back.
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

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"

# The recipe must survive a checkout path that contains spaces, which is why
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

    def command(self, script: Path) -> str:
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


def _makefile_assignment(name: str) -> str:
    """Return the literal value assigned to `name` in the Makefile."""
    prefix = f"{name} ?="
    prefix_plain = f"{name} ="
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
        if line.startswith(prefix_plain):
            return line[len(prefix_plain) :].strip()
    msg = f"{name} is not assigned in the Makefile"
    raise AssertionError(msg)


def _argv(record: dict[str, object]) -> list[str]:
    """Return one recorded argv, failing the test if the fake logged another shape."""
    argv = record["argv"]
    assert isinstance(argv, list), f"the fake logged a non-list argv: {argv!r}"
    return [str(argument) for argument in argv]


@dc.dataclass(frozen=True, slots=True)
class MakeCall:
    """One Make target with the assignments and environment it must receive."""

    target: str
    assignments: tuple[str, ...] = ()
    environment: cabc.Mapping[str, str] = dc.field(default_factory=dict)


def _run_make(
    workdir: Path,
    call: MakeCall,
    fakes: FakeTools,
) -> subprocess.CompletedProcess[str]:
    """Run one Make target in `workdir` with the fake tools in place."""
    command = [
        shutil.which("make") or "make",
        "-C",
        str(workdir),
        "-f",
        str(MAKEFILE),
        call.target,
        f"PROVER_TOOLS={fakes.command(fakes.prover_tools)}",
        f"CARGO={fakes.command(fakes.cargo)}",
        *call.assignments,
    ]
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        command,
        capture_output=True,
        text=True,
        env=fakes.environment(**dict(call.environment)),
        timeout=120,
        check=False,
    )


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Create an isolated checkout path that contains spaces."""
    path = tmp_path / SPACED_ROOT_NAME
    path.mkdir()
    return path


@pytest.fixture
def fakes(tmp_path: Path) -> FakeTools:
    """Create the fake tool executables outside the spaced checkout path."""
    return FakeTools(tmp_path / "fakes")


def test_kani_install_delegates_download_setup_and_verification(
    workdir: Path, fakes: FakeTools
) -> None:
    """Kani installs through the shared CLI, then performs exactly one setup."""
    kani_version = _makefile_assignment("KANI_VERSION")
    pin = REPO_ROOT / "tools/kani/VERSION"
    assert pin.read_text(encoding="utf-8").strip() == kani_version, (
        "the setup bundle path must name the version the updater actually pins"
    )

    result = _run_make(workdir, MakeCall("install-boundary-kani"), fakes)
    assert result.returncode == 0, result.stderr

    records = fakes.records()
    assert [record["tool"] for record in records] == ["prover-tools", "cargo"], (
        "the download must precede setup, and setup must run exactly once"
    )
    cache = workdir / ".cache/boundary-kani"
    expected_install = [
        "kani",
        "install",
        "--repo-root",
        str(workdir),
        "--version-file",
        "tools/kani/VERSION",
        "--install-dir",
        str(cache / "bin"),
        "--cache-dir",
        str(cache),
        "--no-compile",
        "--no-setup",
        "--no-verify",
    ]
    assert records[0]["argv"] == expected_install, (
        "the delegated Kani install lost its explicit paths or safety flags"
    )
    expected_setup = [
        "kani",
        "setup",
        "--use-local-bundle",
        str(cache / f"kani-{kani_version}-x86_64-unknown-linux-gnu.tar.gz"),
    ]
    assert records[1]["argv"] == expected_setup, (
        "setup must use the verified bundle and the configured Cargo"
    )


def test_verus_install_delegates_verus_and_z3(
    workdir: Path, fakes: FakeTools, tmp_path: Path
) -> None:
    """Verus and Z3 both install through the shared CLI with explicit paths."""
    verus_dir = tmp_path / "verus install dir"
    result = _run_make(
        workdir,
        MakeCall("install-verus", assignments=(f"VERUS_INSTALL_DIR={verus_dir}",)),
        fakes,
    )
    assert result.returncode == 0, result.stderr

    records = fakes.records()
    assert [record["tool"] for record in records] == ["prover-tools", "prover-tools"], (
        "Verus installation must delegate both the verifier and the solver"
    )
    expected_verus = [
        "verus",
        "install",
        "--repo-root",
        str(workdir),
        "--install-dir",
        str(verus_dir),
    ]
    assert records[0]["argv"] == expected_verus, (
        "a spaced install path must reach the verifier as one argument"
    )
    z3_dir = workdir / ".cache/boundary-z3"
    expected_z3 = [
        "z3",
        "install",
        "--repo-root",
        str(workdir),
        "--install-dir",
        str(z3_dir),
        "--cache-dir",
        str(z3_dir),
    ]
    assert records[1]["argv"] == expected_z3, (
        "the solver must install into the repository cache as a local file"
    )


def test_verus_install_clears_the_z3_marker_first(
    workdir: Path, fakes: FakeTools
) -> None:
    """A stale install marker must not hide the newly verified solver archive."""
    marker = workdir / ".cache/boundary-z3/.z3-version"
    marker.parent.mkdir(parents=True)
    marker.write_text("4.16.0\n", encoding="utf-8")

    result = _run_make(
        workdir,
        MakeCall("install-verus", environment={"FAKE_MARKER": str(marker)}),
        fakes,
    )
    assert result.returncode == 0, result.stderr

    records = fakes.records()
    assert records[0]["marker_exists"] is True, (
        "the verifier install must not disturb the existing solver cache"
    )
    assert records[1]["marker_exists"] is False, (
        "the marker must be cleared before the delegated solver install"
    )
    assert not marker.exists(), "the retired marker must not survive the reinstall"


def _ambient_guard_names() -> list[str]:
    """Return the names `PROVER_TOOLS_AMBIENT_VARS` clears before delegation."""
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


def test_the_watched_environment_matches_the_makefile_guard() -> None:
    """The fake must watch every name the Makefile claims to clear."""
    assert set(_ambient_guard_names()) == set(AMBIENT_INSTALL_VARIABLES), (
        "the ambient-name tuple must match PROVER_TOOLS_AMBIENT_VARS"
    )


@pytest.mark.parametrize("target", ["install-boundary-kani", "install-verus"])
def test_ambient_install_options_cannot_redirect_the_targets(
    workdir: Path, fakes: FakeTools, target: str
) -> None:
    """Stray CI variables must not override the explicit repository paths."""
    ambient = dict.fromkeys(AMBIENT_INSTALL_VARIABLES, "/ambient/wrong")
    result = _run_make(workdir, MakeCall(target, environment=ambient), fakes)
    assert result.returncode == 0, result.stderr

    delegated = [
        record for record in fakes.records() if record["tool"] == "prover-tools"
    ]
    assert delegated, "the fake prover-tools was never invoked"
    leaked = [record["env"] for record in delegated if record["env"]]
    assert not leaked, f"ambient installer options reached the shared CLI: {leaked}"
    roots = [_argv(record) for record in delegated]
    assert all(argv[argv.index("--repo-root") + 1] == str(workdir) for argv in roots), (
        f"{target} must resolve every delegated call against the checkout"
    )


def test_a_failed_z3_install_fails_the_make_target(
    workdir: Path, fakes: FakeTools
) -> None:
    """A delegated solver failure must stop the target and stay visible."""
    result = _run_make(
        workdir,
        MakeCall(
            "install-verus",
            environment={"FAKE_FAIL_ARGS": json.dumps(["z3", "install"])},
        ),
        fakes,
    )
    assert result.returncode != 0, "a failed solver install must fail the target"
    assert "fake prover-tools failure" in result.stderr, (
        "the delegated failure must reach the caller's log"
    )
    assert [_argv(record)[:2] for record in fakes.records()] == [
        ["verus", "install"],
        ["z3", "install"],
    ], "the verifier must install before the solver it drives"


def test_the_retired_local_installers_are_gone() -> None:
    """A leftover entry point would reintroduce the unbounded disk write."""
    assert not (REPO_ROOT / "scripts/install_boundary_kani.py").exists(), (
        "the retired Kani installer must not return"
    )
    assert not (REPO_ROOT / "scripts/install_boundary_z3.py").exists(), (
        "the retired Z3 installer must not return"
    )
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "install_boundary_kani" not in text, (
        "the Makefile must not call the retired Kani installer"
    )
    assert "install_boundary_z3" not in text, (
        "the Makefile must not call the retired Z3 installer"
    )
