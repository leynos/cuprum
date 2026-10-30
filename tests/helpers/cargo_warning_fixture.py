"""Build dependency-free crates against the selected modern-stable toolchain.

The warning-policy contract needs real Cargo invocations: only a live build
shows that changing `build.warnings` replays a cached artefact instead of
recompiling it. These helpers own the awkward parts of that fixture — probing
the toolchain, writing a locked crate, keeping each pytest run's target
directory under the Rust workspace, and constructing the environment — so the
test module can assert what each Cargo transcript proves.

Scope: the Cargo warning-policy contracts. Nothing here installs a toolchain or
alters ambient configuration; every command runs with an explicit
`RUSTUP_TOOLCHAIN`, an explicit target directory, `--locked`, and `--offline`.
"""

from __future__ import annotations

import dataclasses as dc
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed Cargo argv.
import typing as typ

import pytest

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import pathlib as pth

__all__ = (
    "BASE_RUSTFLAGS",
    "CARGO_COMMAND_TIMEOUT_SECONDS",
    "MODERN_STABLE_TOOLCHAIN",
    "TARGET_DIR",
    "CargoFixture",
    "diagnostics",
    "host_target",
    "make_crate",
    "require_toolchain",
    "target_dir",
    "toolchain_available",
)

#: The compiler the modern-stable route selects, pinned independently here so
#: a Makefile bump that forgets this contract fails rather than silently
#: testing the previous compiler.
MODERN_STABLE_TOOLCHAIN = "1.99.1"

#: Test artefacts stay in the shared Rust workspace, never under `/tmp`.
TARGET_DIR = repo_root() / "rust/target/cargo-build-warnings-contract"

#: The only compiler flag the modern route retains. It is not a warning flag,
#: so changing `build.warnings` must not invalidate a build that keeps it.
BASE_RUSTFLAGS = "-C codegen-units=1"

#: A cold Cargo build of a tiny crate fits comfortably inside this bound; it
#: exists to turn a wedged compilation into a named test failure.
CARGO_COMMAND_TIMEOUT_SECONDS = 1800


def _probe(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run one fixed toolchain probe, or return ``None`` if it cannot run."""
    try:
        return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed toolchain probe.
            argv,
            capture_output=True,
            check=False,
            timeout=10,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _reports_version(probe: subprocess.CompletedProcess[str] | None, tool: str) -> bool:
    """Return whether a probe succeeded and named the selected toolchain."""
    return (
        probe is not None
        and probe.returncode == 0
        and probe.stdout.startswith(f"{tool} {MODERN_STABLE_TOOLCHAIN} ")
    )


def _reports_clippy(probe: subprocess.CompletedProcess[str] | None) -> bool:
    """Return whether a component-listing probe found Clippy installed."""
    return (
        probe is not None
        and probe.returncode == 0
        and any(line.startswith("clippy") for line in probe.stdout.splitlines())
    )


def toolchain_available() -> bool:
    """Return whether the selected compiler, Cargo, and Clippy are installed."""
    rustup = shutil.which("rustup")
    cargo = shutil.which("cargo")
    if rustup is None or cargo is None:
        return False
    rustc = _probe([rustup, "run", MODERN_STABLE_TOOLCHAIN, "rustc", "-V"])
    cargo_version = _probe([cargo, f"+{MODERN_STABLE_TOOLCHAIN}", "-V"])
    components = _probe([
        rustup,
        "component",
        "list",
        "--installed",
        "--toolchain",
        MODERN_STABLE_TOOLCHAIN,
    ])
    return (
        _reports_version(rustc, "rustc")
        and _reports_version(cargo_version, "cargo")
        and _reports_clippy(components)
    )


def require_toolchain() -> str:
    """Return Cargo or skip locally, while making CI absence a failure.

    Returns
    -------
    str
        The Cargo executable that honours the modern toolchain selection.

    Raises
    ------
    AssertionError
        If Cargo disappears between the availability probe and the return.
    """
    cargo = shutil.which("cargo")
    if cargo is None or not toolchain_available():
        message = (
            f"Rust and Cargo {MODERN_STABLE_TOOLCHAIN} with Clippy are unavailable"
        )
        if os.environ.get("CUPRUM_REQUIRE_MODERN_STABLE") == "1":
            pytest.fail(message)
        pytest.skip(message)
    if cargo is None:
        message = "Cargo must remain available after the toolchain check"
        raise AssertionError(message)
    return cargo


def host_target() -> str:
    """Return the exact host triple reported by the selected rustc.

    Returns
    -------
    str
        The host triple, such as ``x86_64-unknown-linux-gnu``.

    Raises
    ------
    AssertionError
        If rustup is unavailable or `rustc -vV` omits its host triple.
    """
    rustup = shutil.which("rustup")
    if rustup is None:
        message = "rustup must be available with the pinned toolchain"
        raise AssertionError(message)
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed toolchain probe.
        [rustup, "run", MODERN_STABLE_TOOLCHAIN, "rustc", "-vV"],
        capture_output=True,
        check=True,
        timeout=10,
        text=True,
    )
    target = next(
        (
            line.removeprefix("host: ")
            for line in result.stdout.splitlines()
            if line.startswith("host: ")
        ),
        None,
    )
    if target is None:
        message = "rustc -vV must report its host target"
        raise AssertionError(message)
    return target


def make_crate(directory: pth.Path, name: str, source: str) -> pth.Path:
    """Create one dependency-free library with a fixed lockfile.

    Parameters
    ----------
    directory : pathlib.Path
        Temporary directory that will hold the crate.
    name : str
        Crate and package name; also used in Cargo's freshness messages.
    source : str
        Contents of ``src/lib.rs``.

    Returns
    -------
    pathlib.Path
        The crate root, ready for a locked Cargo command.
    """
    crate = directory / name
    source_path = crate / "src/lib.rs"
    source_path.parent.mkdir(parents=True)
    (crate / "Cargo.toml").write_text(
        f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2024"\n',
        encoding="utf-8",
    )
    (crate / "Cargo.lock").write_text(
        f'version = 4\n\n[[package]]\nname = "{name}"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    source_path.write_text(source, encoding="utf-8")
    return crate


def target_dir(crate: pth.Path) -> pth.Path:
    """Keep each pytest run isolated while sharing its policy-toggle cache."""
    return TARGET_DIR / crate.parent.name


def diagnostics(result: subprocess.CompletedProcess[str]) -> str:
    """Return Cargo's combined output for a focused assertion message."""
    return result.stdout + result.stderr


@dc.dataclass(frozen=True, slots=True)
class CargoFixture:
    """One crate bound to the selected Cargo and host target.

    Attributes
    ----------
    cargo : str
        Cargo executable honouring the modern toolchain selection.
    target : str
        Host triple reported by the selected rustc.
    crate : pathlib.Path
        Crate root produced by :func:`make_crate`.
    """

    cargo: str
    target: str
    crate: pth.Path

    def run(
        self,
        *arguments: str,
        warnings: str = "deny",
        rustflags: str = BASE_RUSTFLAGS,
    ) -> subprocess.CompletedProcess[str]:
        """Run one locked command through the selected stable toolchain.

        Parameters
        ----------
        *arguments : str
            Cargo subcommand and its arguments.
        warnings : str
            Value for ``CARGO_BUILD_WARNINGS``; ``"warn"`` or ``"deny"``.
        rustflags : str
            Value for ``RUSTFLAGS``, excluding any warning flag.

        Returns
        -------
        subprocess.CompletedProcess[str]
            The finished Cargo invocation, never raised on a non-zero exit.
        """
        environment = os.environ.copy()
        for variable in ("RUSTC", "CARGO_ENCODED_RUSTFLAGS", "RUSTDOCFLAGS"):
            environment.pop(variable, None)
        environment.update({
            "CARGO_BUILD_WARNINGS": warnings,
            "CARGO_TERM_COLOR": "never",
            "CARGO_TARGET_DIR": str(target_dir(self.crate)),
            "RUSTFLAGS": rustflags,
            "RUSTUP_TOOLCHAIN": MODERN_STABLE_TOOLCHAIN,
        })
        return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Cargo argv.
            [
                self.cargo,
                *arguments,
                "--locked",
                "--offline",
                "--verbose",
                "--target",
                self.target,
                "--profile",
                "dev",
            ],
            capture_output=True,
            check=False,
            cwd=self.crate,
            env=environment,
            timeout=CARGO_COMMAND_TIMEOUT_SECONDS,
            text=True,
        )
