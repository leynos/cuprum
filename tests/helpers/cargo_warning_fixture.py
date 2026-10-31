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

import collections.abc as cabc
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
    "Locator",
    "Probe",
    "ToolchainProbeError",
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

#: A toolchain probe answers in well under a second on an idle host. The bound
#: is generous because a loaded host can take tens of seconds to schedule a
#: process; it exists only to keep a wedged child from hanging the suite.
PROBE_TIMEOUT_SECONDS = 60


#: Runs one fixed toolchain probe under optional extra environment entries,
#: returning ``None`` when it gave no answer.
type Probe = cabc.Callable[
    [list[str], cabc.Mapping[str, str] | None],
    subprocess.CompletedProcess[str] | None,
]
#: Resolves an executable name to its path, or to ``None`` when it is absent.
type Locator = cabc.Callable[[str], str | None]


class ToolchainProbeError(RuntimeError):
    """Raised when a toolchain probe could not be started at all."""


def _probe(
    argv: list[str], env: cabc.Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str] | None:
    """Run one fixed toolchain probe, or report that it gave no answer.

    A probe that outlived `PROBE_TIMEOUT_SECONDS` is reported as no answer,
    because a loaded host can delay a working toolchain past any fixed bound;
    counting that as absence keeps the contract skipping instead of erroring.
    `env` adds entries to this child's environment. A probe that cannot be
    started is a different fault: the executable was located, so the
    environment, not the toolchain, is broken.

    Returns
    -------
    subprocess.CompletedProcess[str] | None
        The finished probe, or ``None`` when it did not answer in time.

    Raises
    ------
    ToolchainProbeError
        If the probe could not be started.
    """
    child_env = None if env is None else {**os.environ, **env}
    try:
        return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed toolchain probe.
            argv,
            capture_output=True,
            check=False,
            env=child_env,
            timeout=PROBE_TIMEOUT_SECONDS,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError as error:
        message = f"toolchain probe could not start: {' '.join(argv)}"
        raise ToolchainProbeError(message) from error


def _reports_version(
    probe: subprocess.CompletedProcess[str] | None, tool: str, toolchain: str
) -> bool:
    """Return whether a probe answered and named the requested toolchain."""
    return (
        probe is not None
        and probe.returncode == 0
        and probe.stdout.startswith(f"{tool} {toolchain} ")
    )


def _reports_clippy(probe: subprocess.CompletedProcess[str] | None) -> bool:
    """Return whether a component-listing probe found Clippy installed."""
    return (
        probe is not None
        and probe.returncode == 0
        and any(line.startswith("clippy") for line in probe.stdout.splitlines())
    )


def toolchain_available(
    *,
    toolchain: str = MODERN_STABLE_TOOLCHAIN,
    probe: Probe = _probe,
    locate: Locator = shutil.which,
) -> bool:
    """Return whether the named compiler, Cargo, and Clippy are installed.

    Parameters
    ----------
    toolchain : str
        Toolchain the probe must reach, passed explicitly so the query never
        reads a selection from the ambient environment.
    probe : Probe
        Adapter running one fixed argument vector under optional environment
        entries; it reports no answer as ``None`` and raises only on start failure.
    locate : collections.abc.Callable[[str], str | None]
        Resolver for executable names, injected so the query does not have to
        read `PATH` itself.

    Returns
    -------
    bool
        Whether rustc, Cargo, and Clippy all reported the named toolchain.

    Notes
    -----
    Cargo is selected through ``RUSTUP_TOOLCHAIN``, not a ``+toolchain``
    argument, because the ``+`` form is a rustup proxy feature that a direct
    Cargo binary ignores; the Make route uses the same mechanism.

    A probe that gave no answer counts as unavailable, so a loaded host skips
    the contract rather than failing it. A probe that could not be started
    propagates :class:`ToolchainProbeError`; that fault is the environment's,
    not the toolchain's.
    """
    rustup = locate("rustup")
    cargo = locate("cargo")
    if rustup is None or cargo is None:
        return False
    rustc_query = [rustup, "run", toolchain, "rustc", "-V"]
    clippy_query = [
        rustup,
        "component",
        "list",
        "--installed",
        "--toolchain",
        toolchain,
    ]
    return (
        _reports_version(probe(rustc_query, None), "rustc", toolchain)
        and _reports_version(
            probe([cargo, "-V"], {"RUSTUP_TOOLCHAIN": toolchain}),
            "cargo",
            toolchain,
        )
        and _reports_clippy(probe(clippy_query, None))
    )


def require_toolchain(
    *,
    toolchain: str = MODERN_STABLE_TOOLCHAIN,
    probe: Probe = _probe,
    locate: Locator = shutil.which,
) -> str:
    """Return Cargo or skip locally, while making CI absence a failure.

    Parameters
    ----------
    toolchain : str
        Toolchain the caller needs.
    probe : Probe
        Adapter used for the availability probe.
    locate : collections.abc.Callable[[str], str | None]
        Resolver for executable names.

    Returns
    -------
    str
        The Cargo executable that honours the modern toolchain selection.

    Raises
    ------
    AssertionError
        If the locator resolved no Cargo while the availability query still
        reported the toolchain present.

    Notes
    -----
    A probe that could not be started propagates :class:`ToolchainProbeError`,
    because that is an environment fault and must not be reported as a skipped
    test. A probe that gave no answer counts as unavailable, so it skips here
    and fails under ``CUPRUM_REQUIRE_MODERN_STABLE=1``.
    """
    cargo = locate("cargo")
    if cargo is None or not toolchain_available(
        toolchain=toolchain, probe=probe, locate=locate
    ):
        message = f"Rust and Cargo {toolchain} with Clippy are unavailable"
        if os.environ.get("CUPRUM_REQUIRE_MODERN_STABLE") == "1":
            pytest.fail(message)
        pytest.skip(message)
    if cargo is None:
        message = "Cargo must be locatable whenever the toolchain is available"
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
