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

from scripts.nose_detector import load_settings

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPOSITORY_ROOT / "Makefile"

#: The detector pin, read from ``[tool.nose]`` rather than restated. The
#: scope-contract test owns pin-drift enforcement between the Makefile and the
#: manifest; this test *is* the manifest's claim, so restating the version here
#: would let the two disagree while both read as consistent.
NOSE_VERSION = load_settings(REPOSITORY_ROOT / "pyproject.toml").version

pytestmark = pytest.mark.skipif(
    shutil.which("make") is None,
    reason="`make` is required to exercise the installer target.",
)

#: The install directory is the test's own ``tmp_path``, so it changes every
#: run. The scanner that polices recorded snapshots reads an absolute path as a
#: leaked host path, and a snapshot that changed per run would be worthless
#: anyway, so the directory is replaced with a stable placeholder.
_INSTALL_DIRECTORY_KEY = "--install-path"


def _redact_install_path(arguments: list[str]) -> list[str]:
    """Replace the ``--install-path`` operand with a stable placeholder.

    Each element is redacted whole, so the recorded vector keeps one entry per
    argument. Substituting inside a joined rendering would lose the boundaries
    that make a reordered vector fail the comparison.

    Returns
    -------
    list[str]
        The argv with its install directory replaced.
    """
    return [
        "<install-path>"
        if index and arguments[index - 1] == _INSTALL_DIRECTORY_KEY
        else argument
        for index, argument in enumerate(arguments)
    ]


def _next_scratch_root(tmp_path: Path) -> Path:
    """Return an unused scratch root beneath *tmp_path*.

    A test that drives more than one install needs one root per call, and the
    caller should not have to know how many have already been made. Counting
    the roots already present keeps the allocation inside the test's own
    ``tmp_path``, so nothing is left behind for the next test to inherit.

    Returns
    -------
    Path
        A newly created directory.
    """
    taken = sum(1 for entry in tmp_path.iterdir() if entry.is_dir())
    scratch = tmp_path / f"case-{taken}"
    scratch.mkdir()
    return scratch


class InstallerInvocation(typ.NamedTuple):
    """The recorded effects of one `make install-nose` run."""

    result: subprocess.CompletedProcess[str]
    """The completed Make process."""

    binstall_arguments: list[str]
    """The argument vector the stub `cargo-binstall` received, if any."""

    install_directory: Path
    """The directory passed as ``NOSE_TOOLS_DIR`` for this run."""


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

    # A test that drives more than one case owns the allocation, so the second
    # call is given its own scratch root rather than colliding on this one; a
    # single-case test still gets the `tmp_path` it asked for, by the same rule.
    scratch = _next_scratch_root(tmp_path)

    tools = scratch / "tools"
    tools.mkdir()
    (scratch / "payload").mkdir()
    nose_bin = tools / "nose"
    if installed_version is not None:
        _stub_nose(tools, installed_version)

    binstall_log = scratch / "binstall-arguments"
    # The installer stub copies this template, so a successful reinstall
    # normally leaves a detector at the pinned version.
    detonator = _stub_nose(scratch / "payload", installed_after)
    binstall = _stub_binstall(
        scratch, binstall_log, detonator, exit_code=binstall_exit_code
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
    return InstallerInvocation(
        result=result,
        binstall_arguments=_redact_install_path(arguments),
        install_directory=tools,
    )


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

    def test_missing_or_mismatched_binary_invokes_the_pinned_installer(
        self,
        tmp_path: Path,
        snapshot: SnapshotAssertion,
    ) -> None:
        """A missing or stale detector triggers the same pinned reinstall.

        Both cases are driven into one recorded vector rather than asserted
        field by field. The whole argv is the contract — the flags that make
        the install non-interactive, the git source, the pinned crate, and the
        strategy restriction all have to reach ``cargo-binstall`` together — so
        pinning it in order catches a dropped or reordered flag that separate
        membership checks would read past.
        """
        missing = _run_install_nose(tmp_path, installed_version=None)
        stale = _run_install_nose(tmp_path, installed_version="0.19.0")

        for case, invocation in (("missing", missing), ("mismatched", stale)):
            assert invocation.result.returncode == 0, invocation.result.stderr
            assert f"Installing nose {NOSE_VERSION}" in invocation.result.stdout, (
                f"A {case} detector must trigger an install."
            )
        assert missing.binstall_arguments == snapshot, (
            "Every install flag must reach cargo-binstall, in the documented order."
        )
        assert stale.binstall_arguments == missing.binstall_arguments, (
            "A missing and a stale detector must be installed identically; only "
            "the decision to install may differ between them."
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
        installed = invocation.install_directory / "nose"
        assert installed.exists(), (
            "Installation must place the detector at NOSE_TOOLS_DIR."
        )
        verified = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - the stub written by this test.
            [str(installed), "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert verified.stdout.strip() == f"nose {NOSE_VERSION}", (
            "The installed detector must report the pinned version."
        )

    def test_an_install_directory_containing_spaces_is_reused(
        self, tmp_path: Path
    ) -> None:
        """A cached detector under a path with spaces is still found and run.

        The recipe's probes invoke the executable through the shell, so an
        unquoted ``NOSE_BIN`` splits a path containing a space into two words.
        The probe then runs a binary that does not exist, reads no version, and
        reports the cached detector as absent. A spaced ancestor is enough to
        provoke that: the recipe sees the whole absolute path either way.
        """
        spaced = tmp_path / "nose tools"
        spaced.mkdir()
        invocation = _run_install_nose(spaced, installed_version=NOSE_VERSION)

        assert invocation.result.returncode == 0, invocation.result.stderr
        assert "already installed" in invocation.result.stdout, (
            "A path containing spaces must not make a matching detector look "
            f"absent. stdout: {invocation.result.stdout!r}"
        )
        assert invocation.binstall_arguments == [], (
            "A matching cached detector must not invoke the installer."
        )

    def test_a_reinstall_into_a_directory_containing_spaces_verifies(
        self, tmp_path: Path
    ) -> None:
        """A reinstall into a path with spaces still verifies the pin."""
        spaced = tmp_path / "nose tools"
        spaced.mkdir()
        invocation = _run_install_nose(spaced, installed_version="0.19.0")

        assert invocation.result.returncode == 0, (
            "Installing into a path containing spaces must succeed. "
            f"stdout: {invocation.result.stdout!r} stderr: {invocation.result.stderr!r}"
        )
        assert (invocation.install_directory / "nose").exists(), (
            "Installation must place the detector at NOSE_TOOLS_DIR."
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
        assert f"expected 'nose {NOSE_VERSION}'" in invocation.result.stderr, (
            "The target must name the pin and the version it actually found."
        )
