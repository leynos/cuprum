"""Contract tests for the `make install-nose` detector installer.

The target decides between a cached binary and a fresh `cargo-binstall`
clone-and-install by comparing `nose --version` against the pinned
`NOSE_VERSION`. The full suite and `make duplication-test` both exercise it
here rather than through a downloaded detector: a stub `nose` reports whatever
version the test needs, and a stub `cargo-binstall` records how it was called.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``
(``scripts/tests/test_make_install_nose.py``), the merged revision of PR #276,
under the ISC terms in ``LICENSE``.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - tests exercise the real Make target.
import typing as typ
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPOSITORY_ROOT / "Makefile"

#: The detector pin these tests assume. The scope-contract test owns
#: pin-drift enforcement between the Makefile and ``[tool.nose]``; this value
#: only names the version the stubs report.
NOSE_VERSION = "0.20.0"

pytestmark = pytest.mark.skipif(
    shutil.which("make") is None,
    reason="`make` is required to exercise the installer target.",
)


class InstallerInvocation(typ.NamedTuple):
    """The recorded effects of one `make install-nose` run."""

    result: subprocess.CompletedProcess[str]
    """The completed Make process."""

    binstall_arguments: list[str]
    """The argument vector the stub `cargo-binstall` received, if any."""


def _write_executable(path: Path, body: str) -> Path:
    """Write an executable stub script.

    Returns
    -------
    Path
        The executable stub.
    """
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _stub_nose(directory: Path, version: str) -> Path:
    """Write a stub detector reporting ``version`` for ``--version``."""
    return _write_executable(
        directory / "nose",
        f"#!/bin/sh\nprintf 'nose %s\\n' '{version}'\n",
    )


def _stub_binstall(
    directory: Path,
    log: Path,
    detonator: Path,
    *,
    exit_code: int = 0,
) -> Path:
    """Write a stub installer that records its arguments and installs nose.

    cargo-binstall is invoked with ``--install-path <dir>`` before the crate
    spec, so the stub mirrors that shape: it logs its argv and copies the
    already-written detector into the requested install path. A non-zero
    ``exit_code`` models a failed install — the argv is still recorded, but
    nothing is placed at the install path.

    Returns
    -------
    Path
        The executable stub.
    """
    effect = (
        f'cp "{detonator}" "$install_path/nose"\nchmod +x "$install_path/nose"\n'
        if exit_code == 0
        else f"exit {exit_code}\n"
    )
    return _write_executable(
        directory / "cargo-binstall",
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" > "{log}"\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    --install-path) install_path="$2"; shift 2 ;;\n'
        "    *) shift ;;\n"
        "  esac\n"
        "done\n" + effect,
    )


def _run_install_nose(
    tmp_path: Path,
    *,
    installed_version: str | None,
    installed_after: str = NOSE_VERSION,
    binstall_exit_code: int = 0,
) -> InstallerInvocation:
    """Run the real target against stubbed detector and installer binaries.

    Parameters
    ----------
    tmp_path : Path
        Scratch directory the stub environment is built in.
    installed_version : str | None
        The version the cached detector reports, or ``None`` to simulate a
        binary that is absent altogether.
    installed_after : str
        The version the stub installer places at the install path. A value
        other than the pin models an installer that reported success but
        delivered the wrong detector.
    binstall_exit_code : int
        The stub installer's exit status. Non-zero models a failed install.

    Returns
    -------
    InstallerInvocation
        The Make result and the installer's recorded arguments.
    """
    make = shutil.which("make")
    assert make is not None, "Guarded by the module-level skip."

    tools = tmp_path / "tools"
    tools.mkdir()
    (tmp_path / "payload").mkdir()
    nose_bin = tools / "nose"
    if installed_version is not None:
        _stub_nose(tools, installed_version)

    binstall_log = tmp_path / "binstall-arguments"
    # The installer stub copies this template, so a successful reinstall
    # normally leaves a detector at the pinned version.
    detonator = _stub_nose(tmp_path / "payload", installed_after)
    binstall = _stub_binstall(
        tmp_path, binstall_log, detonator, exit_code=binstall_exit_code
    )

    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed target and stubbed tools.
        [
            make,
            "--no-print-directory",
            "-f",
            str(MAKEFILE),
            "install-nose",
            f"NOSE_BIN={nose_bin}",
            f"NOSE_TOOLS_DIR={tools}",
            f"CARGO_BINSTALL={binstall}",
        ],
        cwd=REPOSITORY_ROOT,
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        check=False,
        capture_output=True,
        text=True,
    )
    arguments = (
        binstall_log.read_text(encoding="utf-8").split()
        if binstall_log.exists()
        else []
    )
    return InstallerInvocation(result=result, binstall_arguments=arguments)


class TestMakeInstallNose:
    """The cached-versus-install decision in `make install-nose`."""

    def test_matching_cached_binary_skips_installation(self, tmp_path: Path) -> None:
        """A detector already at the pinned version is reused untouched."""
        invocation = _run_install_nose(tmp_path, installed_version=NOSE_VERSION)

        assert invocation.result.returncode == 0, invocation.result.stderr
        assert "already installed" in invocation.result.stdout, (
            "A matching detector must be reported as already installed."
        )
        assert invocation.binstall_arguments == [], (
            "A matching cached detector must not invoke the installer."
        )

    @pytest.mark.parametrize(
        ("installed_version", "case"),
        [
            pytest.param(None, "missing", id="missing-binary"),
            pytest.param("0.19.0", "mismatched", id="mismatched-version"),
        ],
    )
    def test_missing_or_mismatched_binary_invokes_the_pinned_installer(
        self,
        tmp_path: Path,
        installed_version: str | None,
        case: str,
    ) -> None:
        """A missing or stale detector triggers a pinned reinstall."""
        invocation = _run_install_nose(tmp_path, installed_version=installed_version)

        assert invocation.result.returncode == 0, invocation.result.stderr
        assert f"Installing nose {NOSE_VERSION}" in invocation.result.stdout, (
            f"A {case} detector must trigger an install."
        )
        assert "--no-confirm" in invocation.binstall_arguments, (
            "Installation must run non-interactively."
        )
        assert "--git" in invocation.binstall_arguments, (
            "The crate is not on crates.io, so the install must use git mode."
        )
        assert f"nose-cli@{NOSE_VERSION}" in invocation.binstall_arguments, (
            "Installation must request the pinned detector version."
        )
        assert "--disable-strategies" in invocation.binstall_arguments, (
            "The install must refuse the compile and quick-install strategies."
        )
        assert "compile,quick-install" in invocation.binstall_arguments, (
            "Only the pre-built download strategies may be used; compiling the "
            "detector from source defeats the point of the pin."
        )

    def test_reinstall_leaves_a_detector_at_the_pinned_version(
        self, tmp_path: Path
    ) -> None:
        """After a stale reinstall the detector verifies against the pin."""
        invocation = _run_install_nose(tmp_path, installed_version="0.19.0")

        assert invocation.result.returncode == 0, invocation.result.stderr
        assert ".tools/nose/nose --version" not in invocation.result.stdout, (
            "The recipe must verify through the configured NOSE_BIN."
        )
        assert (tmp_path / "tools" / "nose").exists(), (
            "Installation must place the detector at NOSE_TOOLS_DIR."
        )
        verified = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - the stub written by this test.
            [str(tmp_path / "tools" / "nose"), "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert verified.stdout.strip() == f"nose {NOSE_VERSION}", (
            "The installed detector must report the pinned version."
        )

    def test_a_failed_install_fails_the_target(self, tmp_path: Path) -> None:
        """An installer failure propagates instead of leaving a stale detector.

        Without the explicit status check the recipe's last command decides the
        shell's status. A failed install over a detector that is still present
        at the old version would then report success while the gate goes on
        running the stale binary.
        """
        invocation = _run_install_nose(
            tmp_path, installed_version="0.19.0", binstall_exit_code=42
        )

        assert invocation.result.returncode != 0, (
            "A failed install must fail `make install-nose`, not be reported as "
            f"a successful reinstall. stdout: {invocation.result.stdout!r}"
        )

    def test_an_install_that_delivers_the_wrong_version_fails_the_target(
        self, tmp_path: Path
    ) -> None:
        """A successful installer that placed the wrong detector is a failure.

        cargo-binstall can exit zero having resolved a different revision than
        the requested tag. Re-checking the installed binary is what binds the
        version the gate will actually run to the pin the manifest declares.
        """
        invocation = _run_install_nose(
            tmp_path, installed_version="0.19.0", installed_after="0.19.0"
        )

        assert invocation.result.returncode != 0, (
            "An install that left the detector at the old version must fail the "
            f"target. stdout: {invocation.result.stdout!r}"
        )
        assert "expected 'nose 0.20.0'" in invocation.result.stderr, (
            "The target must name the pin and the version it actually found."
        )
