"""Shared subprocess support for duplication-gate workflow tests.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``
(``scripts/tests/duplication_gate_test_support.py``), the merged revision of
PR #276, under the ISC terms in ``LICENSE``. The gate modules are imported
through the ``scripts`` package, which the repository root satisfies.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - support invokes fixed test commands.
import sys
import typing as typ
from pathlib import Path

from scripts import duplication_allowlist as allowlist
from scripts import duplication_gate as gate
from scripts import nose_detector as detector
from scripts import nose_schema as schema

if typ.TYPE_CHECKING:
    from collections import abc as cabc

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: Report emitted by the stub detector: one two-member duplication family.
STUB_REPORT: dict[str, object] = {
    "schema_version": 9,
    "families": [
        {
            "id": "stub",
            "witness": "copy-paste",
            "surface": "default",
            "value": 22.1,
            "metrics": {"mean_score": 1.0},
            "locations": [
                {"file": "cuprum/a.py", "start": 1, "end": 20, "name": None},
                {"file": "cuprum/b.py", "start": 30, "end": 49, "name": None},
            ],
        }
    ],
}


def copied_gate_workspace(tmp_path: Path) -> Path:
    """Create a mutable workspace containing the gate and its helper modules.

    The workspace is a self-contained repository root: the copied modules are
    reached through the ``scripts`` package, which resolves from the
    workspace's own ``scripts`` directory once it is the working directory, so
    a subprocess never writes to the surrounding checkout.

    Returns
    -------
    Path
        The workspace root, to be passed to :func:`run_gate_command`.
    """
    workspace = tmp_path / "gate-workspace"
    scripts = workspace / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        "atomic_write.py",
        "duplication_allowlist.py",
        "duplication_gate.py",
        "nose_detector.py",
        "nose_schema.py",
    ):
        shutil.copy(REPOSITORY_ROOT / "scripts" / name, scripts / name)
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "gate-test"\nversion = "0"\n\n[tool.duplication_gate]\n',
        encoding="utf-8",
    )
    return workspace


def write_stub_nose(
    directory: Path,
    *,
    version: str = "nose 0.20.0",
    report: dict[str, object] | None = None,
) -> Path:
    """Write an executable stub standing in for the pinned nose binary.

    The stub answers ``--version`` and otherwise prints one canned JSON
    report, so gate tests exercise the real subprocess boundary without
    depending on a downloaded detector.

    Returns
    -------
    Path
        Path to the executable stub.
    """
    stub = directory / "nose"
    payload = STUB_REPORT if report is None else report
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "if '--version' in sys.argv:\n"
        f"    print({version!r})\n"
        "    raise SystemExit(0)\n"
        f"print(json.dumps({payload!r}))\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def gate_command(workspace: Path, *arguments: str) -> list[str]:
    """Build an isolated Python command for the workspace's copied gate.

    The script-path form mirrors the Makefile's ``DUPLICATION_GATE``, where
    ``uv run`` reads the gate's PEP 723 header and the ``scripts.<name>``
    sibling imports are satisfied by ``PYTHONPATH`` rather than by the working
    directory; see :func:`gate_environment`.

    Returns
    -------
    list[str]
        The interpreter, the workspace's gate script, and the given arguments.
    """
    return [
        sys.executable,
        str(workspace / "scripts" / "duplication_gate.py"),
        *arguments,
    ]


def gate_environment(workspace: Path, **overrides: str) -> dict[str, str]:
    """Build a deterministic environment whose imports resolve in ``workspace``.

    ``PYTHONPATH`` is set to the workspace root, so the copied gate's
    ``scripts.<name>`` imports resolve to the modules beside the workspace's
    own ``pyproject.toml`` instead of to the checkout running the tests. This
    matches the Makefile, which passes ``PYTHONPATH=.`` alongside the script.

    Returns
    -------
    dict[str, str]
        The ambient environment with ``PYTHONPATH`` and any overrides applied.
    """
    return {**os.environ, "PYTHONPATH": str(workspace), **overrides}


def run_gate_command(
    workspace: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a copied gate command and capture its completed result."""
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed test interpreter and copied workspace.
        gate_command(workspace, *arguments),
        cwd=workspace,
        env=gate_environment(workspace) if environment is None else environment,
        check=False,
        capture_output=True,
        text=True,
    )


def stub_settings() -> detector.NoseSettings:
    """Build the standard detector settings used by the gate tests.

    Vary individual fields at the call site with :func:`dataclasses.replace`
    rather than threading an override parameter per field through this factory.

    Returns
    -------
    detector.NoseSettings
        The pinned version, roots, channels, size floor, surface, ranking
        bound, and exclusions shared by the gate tests.
    """
    return detector.NoseSettings(
        version="0.20.0",
        roots=("cuprum",),
        mode="syntax,semantic,near",
        min_size=24,
        surface="all",
        top=30,
        exclude=(),
    )


def stub_runner(
    *, version: str = "nose 0.20.0", report: object = None
) -> detector.CommandRunner:
    """Build a command runner double answering version and query commands."""
    payload = STUB_REPORT if report is None else report

    def run(command: cabc.Sequence[str]) -> str:
        """Answer ``--version`` probes and return the canned JSON report."""
        if "--version" in command:
            return f"{version}\n"
        return json.dumps(payload)

    return run


__all__ = [
    "REPOSITORY_ROOT",
    "STUB_REPORT",
    "allowlist",
    "copied_gate_workspace",
    "detector",
    "gate",
    "gate_command",
    "gate_environment",
    "run_gate_command",
    "schema",
    "stub_runner",
    "stub_settings",
    "write_stub_nose",
]
