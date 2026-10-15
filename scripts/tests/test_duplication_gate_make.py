"""Make-target contract tests for recording duplication exceptions.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``
(``scripts/tests/test_duplication_gate_make.py``), the merged revision of
PR #276, under the ISC terms in ``LICENSE``.
"""

from __future__ import annotations

import dataclasses as dc
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - tests exercise the real Make target.
import sys
import typing as typ

import pytest

from scripts.tests.duplication_gate_test_support import (
    REPOSITORY_ROOT,
    allowlist,
    copied_gate_workspace,
    gate_environment,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


@dc.dataclass(frozen=True, slots=True)
class _MakeAllowRequest:
    """The `FIRST`, `MEMBERS`, and `REASON` inputs for one Make target run.

    ``members`` holds every location past the first, joined with spaces into
    the single ``MEMBERS`` value the target reads. GNU Make overwrites a
    repeated command-line variable with its last occurrence, so the separate
    `SECOND` variables the target forwards as `--second` cannot be supplied
    that way; one list-valued variable is what makes three-or-more-member
    families addressable from the command line.
    """

    first: str | None
    members: tuple[str, ...]
    reason: str | None


def _make_allow(
    workspace: Path,
    *,
    request: _MakeAllowRequest,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the real Make target against a copied, writable gate workspace.

    ``DUPLICATION_GATE`` is overridden on the command line with the same
    script-path form the Makefile uses, pointed at the workspace's copy, so the
    target writes to the workspace's own manifest rather than the checkout
    running the tests.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The completed Make invocation with its captured output.
    """
    make = shutil.which("make")
    assert make is not None, "Expected make to be available for contract tests."
    gate = f"{sys.executable} {workspace / 'scripts' / 'duplication_gate.py'}"
    command = [
        make,
        "--no-print-directory",
        "-f",
        str(REPOSITORY_ROOT / "Makefile"),
        "duplication-allow",
        f"DUPLICATION_GATE={gate}",
    ]
    if request.first is not None:
        command.append(f"FIRST={request.first}")
    if request.members:
        command.append(f"MEMBERS={' '.join(request.members)}")
    if request.reason is not None:
        command.append(f"REASON={request.reason}")
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make target and copied workspace.
        command,
        cwd=workspace,
        env=gate_environment(workspace) if environment is None else environment,
        check=False,
        capture_output=True,
        text=True,
    )


class TestMakeDuplicationAllow:
    """The `make duplication-allow` command-line contract."""

    @pytest.mark.parametrize(
        ("first", "reason", "expected_error"),
        [
            pytest.param(None, "reviewed exception", "FIRST is required", id="first"),
            pytest.param("cuprum/a.py", None, "REASON is required", id="reason"),
        ],
    )
    def test_make_duplication_allow_rejects_missing_and_ambient_values(
        self,
        tmp_path: Path,
        first: str | None,
        reason: str | None,
        expected_error: str,
    ) -> None:
        """Only command-line values satisfy the Make target's required inputs."""
        workspace = copied_gate_workspace(tmp_path)
        environment = {
            **gate_environment(workspace),
            "FIRST": "cuprum/ambient.py",
            "MEMBERS": "cuprum/ambient_member.py",
            "REASON": "ambient reason",
        }
        result = _make_allow(
            workspace,
            request=_MakeAllowRequest(first=first, members=(), reason=reason),
            environment=environment,
        )
        assert result.returncode == 2, result.stderr
        assert expected_error in result.stderr, (
            "Make must reject ambient values for required arguments."
        )

    def test_make_duplication_allow_never_writes_to_the_checkout(
        self, tmp_path: Path
    ) -> None:
        """The real Make target writes only inside the copied workspace.

        The recipe is expanded from the checkout's own ``Makefile``, so an
        unset or wrongly resolved ``DUPLICATION_GATE`` would let the target
        record its fixture entries in the repository's real ``pyproject.toml``.
        That
        happened during development: the committed manifest briefly carried
        ``cuprum/a.py`` entries and a quoted command-injection string as a
        reason. Compare the checkout's manifest byte-for-byte around a run so
        the leak cannot return unnoticed.
        """
        manifest = REPOSITORY_ROOT / "pyproject.toml"
        before = manifest.read_bytes()
        workspace = copied_gate_workspace(tmp_path)
        result = _make_allow(
            workspace,
            request=_MakeAllowRequest(
                first="cuprum/leak.py",
                members=(),
                reason="guard against writing to the checkout",
            ),
        )
        assert result.returncode == 0, result.stderr
        assert manifest.read_bytes() == before, (
            "make duplication-allow wrote to the checkout's pyproject.toml; "
            "the DUPLICATION_GATE override must keep it in the workspace."
        )
        entries = allowlist.load_allowlist(workspace / "pyproject.toml")
        assert entries[0].keys == ("cuprum/leak.py",), (
            "The entry must land in the copied workspace's manifest instead."
        )

    @pytest.mark.parametrize(
        ("members", "expected_keys"),
        [
            pytest.param((), ("cuprum/a.py",), id="unit"),
            pytest.param(
                ("cuprum/b.py::beta",),
                ("cuprum/a.py", "cuprum/b.py::beta"),
                id="members",
            ),
            pytest.param(
                ("cuprum/b.py::beta", "cuprum/c.py"),
                ("cuprum/a.py", "cuprum/b.py::beta", "cuprum/c.py"),
                id="three-members",
            ),
        ],
    )
    def test_make_duplication_allow_round_trips_quoted_values(
        self,
        tmp_path: Path,
        members: tuple[str, ...],
        expected_keys: tuple[str, ...],
    ) -> None:
        """The Make target forwards unit and member inputs as literal arguments."""
        workspace = copied_gate_workspace(tmp_path)
        marker = tmp_path / "injected-command"
        reason = f'kept literally: "$(touch {marker})"; $HOME'
        result = _make_allow(
            workspace,
            request=_MakeAllowRequest(
                first="cuprum/a.py", members=members, reason=reason
            ),
        )
        assert result.returncode == 0, result.stderr
        entries = allowlist.load_allowlist(workspace / "pyproject.toml")
        assert entries[0].keys == expected_keys, (
            "Make must forward the requested keys exactly."
        )
        assert entries[0].reason == reason, "Make must preserve quoted reasons."
        assert not marker.exists(), "Quoted values must not execute shell fragments."


def _make_dry_run(target: str) -> subprocess.CompletedProcess[str]:
    """Expand a Make target's recipe without running it."""
    make = shutil.which("make")
    assert make is not None, "Expected make to be available for contract tests."
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make target in the repository root.
        [
            make,
            "--dry-run",
            "--no-print-directory",
            "-f",
            str(REPOSITORY_ROOT / "Makefile"),
            target,
        ],
        cwd=REPOSITORY_ROOT,
        env=gate_environment(REPOSITORY_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )


class TestMakeGateWiring:
    """The Make targets that must run the blocking duplication gate.

    Mocked gate tests cannot show that `make lint` still reaches the gate, so
    these expand the real recipes and assert the gate command survives.
    """

    @pytest.mark.parametrize("target", ["lint", "duplication"])
    def test_target_runs_the_duplication_gate_check(self, target: str) -> None:
        """Both targets invoke the gate's `check` subcommand."""
        result = _make_dry_run(target)

        assert result.returncode == 0, result.stderr
        assert "scripts/duplication_gate.py check" in result.stdout, (
            f"`make {target}` must invoke the duplication gate's check command."
        )

    @pytest.mark.parametrize("target", ["lint", "duplication"])
    def test_target_pins_the_detector_binary(self, target: str) -> None:
        """Both targets pass the pinned detector location to the gate."""
        result = _make_dry_run(target)

        assert "NOSE_BIN=" in result.stdout, (
            f"`make {target}` must pin the detector binary for the gate."
        )

    def test_duplication_installs_the_pinned_detector_first(self) -> None:
        """The standalone target ensures the pinned detector before gating."""
        result = _make_dry_run("duplication")
        install = result.stdout.find("nose-cli@")
        check = result.stdout.find("scripts/duplication_gate.py check")

        assert install != -1, "`make duplication` must ensure the pinned detector."
        assert install < check, "The detector must be installed before the gate runs."
