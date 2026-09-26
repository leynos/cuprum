"""End-to-end blocking tests for the duplication gate.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``
(``scripts/tests/test_duplication_gate_commands.py``), the merged revision of
PR #276, under the ISC terms in ``LICENSE``.

These tests drive the real pinned detector through the real ``check`` command,
so they are separated from the stubbed command-surface tests: the stub tests
publish the interface contract, while these show a genuine duplicate still
reaches the gate and fails the build.
"""

from __future__ import annotations

import dataclasses as dc
import json
import subprocess  # ruff: ignore[suspicious-subprocess-import] - tests exercise the real repository gate command.
import sys
import textwrap
import typing as typ

import pytest

from scripts.tests.duplication_gate_test_support import (
    REPOSITORY_ROOT,
    copied_gate_workspace,
    detector,
    gate_environment,
    run_gate_command,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def _skip_without_the_detector(error: detector.GateExecutionError) -> typ.NoReturn:
    """Skip the test when the pinned detector is not provisioned.

    The ``NoReturn`` annotation is load-bearing for the callers, not decoration:
    without it the helpers below look like they can fall off the end of the
    ``except`` branch, so a correct ``return``-in-``try``/``skip``-in-``except``
    reads as an inconsistent return.
    """
    pytest.skip(str(error))


class TestEndToEndBlocking:
    """The real detector driving the real `check` command.

    Every other blocking test substitutes a stub report or calls the detector
    without the gate, so none of them shows that a genuine duplicate reaches
    `check` and fails the build. These do, using the pinned binary.
    """

    DUPLICATE_BODY = textwrap.dedent(
        """\
        def NAME(items):
            total = 0.0
            for item in items:
                price = item["price"] * item["quantity"]
                if item.get("taxable"):
                    price *= 1.2
                if item.get("discount"):
                    price -= item["discount"]
                total += price
            if total < 0:
                total = 0.0
            return round(total, 2)
        """
    )

    def _planted_workspace(self, tmp_path: Path, *, allow: str = "") -> Path:
        """Build a gate workspace whose package holds one verbatim duplicate."""
        workspace = copied_gate_workspace(tmp_path)
        package = workspace / "cuprum"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "mod.py").write_text(
            self.DUPLICATE_BODY.replace("NAME", "first_total")
            + "\n\n"
            + self.DUPLICATE_BODY.replace("NAME", "second_total"),
            encoding="utf-8",
        )
        (workspace / "pyproject.toml").write_text(
            textwrap.dedent(
                """\
                [project]
                name = "gate-test"
                version = "0"

                [tool.nose]
                version = "0.20.0"
                roots = ["cuprum"]
                mode = "syntax,semantic,near"
                min-size = 8
                surface = "all"
                top = 30
                """
            )
            + allow,
            encoding="utf-8",
        )
        return workspace

    def _run_check(self, workspace: Path) -> subprocess.CompletedProcess[str]:
        """Run the copied gate's real `check` against the pinned detector."""
        return run_gate_command(
            workspace,
            "check",
            environment=gate_environment(workspace, NOSE_BIN=self._resolve_binary()),
        )

    def _resolve_binary(self) -> str:
        """Return the pinned detector, skipping when it is not provisioned."""
        settings = detector.load_settings(REPOSITORY_ROOT / "pyproject.toml")
        try:
            return detector.resolve_binary(settings)
        except detector.GateExecutionError as error:  # pragma: no cover
            _skip_without_the_detector(error)

    def test_planted_duplicate_blocks_the_gate(self, tmp_path: Path) -> None:
        """A genuine duplicate fails `check` and names both copies."""
        result = self._run_check(self._planted_workspace(tmp_path))

        assert result.returncode == 1, (
            f"A planted duplicate must fail the gate.\n{result.stdout}{result.stderr}"
        )
        assert "cuprum/mod.py" in result.stdout, (
            "The report must locate the duplicated file."
        )
        assert "make duplication-allow" in result.stdout, (
            "A blocking report must show how to record a reasoned exception."
        )

    def test_reasoned_exception_unblocks_the_planted_duplicate(
        self, tmp_path: Path
    ) -> None:
        """The same duplicate passes once a reasoned allow entry covers it."""
        allow = textwrap.dedent(
            """
            [[tool.duplication_gate.allow]]
            unit = "cuprum/mod.py"
            reason = "Planted fixture proving the gate blocks and allows."
            """
        )
        result = self._run_check(self._planted_workspace(tmp_path, allow=allow))

        assert result.returncode == 0, (
            f"A covered duplicate must pass.\n{result.stdout}{result.stderr}"
        )
        assert "duplication gate passed" in result.stdout, (
            "The gate must report its successful result."
        )

    def test_real_check_cli_passes(self) -> None:
        """The checked-in gate runs successfully through its real CLI boundary."""
        binary = self._resolve_binary()
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed repository gate command.
            [
                sys.executable,
                str(REPOSITORY_ROOT / "scripts" / "duplication_gate.py"),
                "check",
            ],
            cwd=REPOSITORY_ROOT,
            env=gate_environment(REPOSITORY_ROOT, NOSE_BIN=binary),
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"The adjudicated cohort must leave the real gate passing.\n"
            f"{result.stdout}{result.stderr}"
        )
        assert "duplication gate passed" in result.stdout, (
            "Real check invocation must report its successful gate result."
        )

    def test_reports_a_planted_verbatim_copy(self, tmp_path: Path) -> None:
        """The pinned detector reports a planted copy through normalization."""
        settings = detector.load_settings(REPOSITORY_ROOT / "pyproject.toml")
        binary = self._resolve_binary()
        workspace = tmp_path
        (workspace / "mod.py").write_text(
            self.DUPLICATE_BODY.replace("NAME", "first_total")
            + "\n\n"
            + self.DUPLICATE_BODY.replace("NAME", "second_total"),
            encoding="utf-8",
        )
        command = detector.build_command(
            binary, dc.replace(settings, roots=(".",), min_size=8)
        )
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - pinned, repository-owned binary.
            command,
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        findings = detector.normalize_findings(json.loads(result.stdout))
        assert findings, "The planted copy must be reported."
        assert any(
            {location.name for location in finding.locations}
            == {"first_total", "second_total"}
            for finding in findings
        ), "The planted copy must name both duplicated functions."
