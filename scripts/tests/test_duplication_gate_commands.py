"""Command-line behaviour tests for the duplication gate.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``
(``scripts/tests/test_duplication_gate_commands.py``), the merged revision of
PR #276, under the ISC terms in ``LICENSE``.
"""

from __future__ import annotations

import textwrap
import typing as typ
from pathlib import Path

import pytest

from scripts.tests.duplication_gate_test_support import (
    allowlist,
    copied_gate_workspace,
    detector,
    gate,
    gate_environment,
    run_gate_command,
    write_stub_nose,
)

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion


def _finding() -> detector.Finding:
    """Build a representative blocking finding."""
    return detector.Finding(
        witness="copy-paste",
        value=22.1,
        locations=(
            detector.Location(file="cuprum/a.py", start=1, end=20, name=None),
            detector.Location(file="cuprum/b.py", start=30, end=49, name="beta"),
        ),
    )


def _grown_finding() -> detector.Finding:
    """Build a finding whose three locations outgrew a two-key entry."""
    return detector.Finding(
        witness="copy-paste",
        value=38.4,
        locations=(
            detector.Location(file="cuprum/one.py", start=1, end=12, name="first"),
            detector.Location(file="cuprum/two.py", start=1, end=12, name="second"),
            detector.Location(file="cuprum/three.py", start=1, end=12, name="third"),
        ),
    )


class TestGateCommands:
    """CLI orchestration and real workflow contracts."""

    def test_check_reports_blocking_findings(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        snapshot: SnapshotAssertion,
    ) -> None:
        """The check command emits the blocking report and status one."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(gate, "load_allowlist", lambda _path: ())
        monkeypatch.setattr(gate, "detect_findings", lambda: [_finding()])
        with pytest.raises(SystemExit) as error:
            gate.check()
        assert error.value.code == 1, "Blocking findings must return status one."
        assert capsys.readouterr().out == snapshot, (
            "Blocking report must remain actionable and deterministic."
        )

    def test_check_reports_stale_entries(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Allow entries covering nothing are reported for removal."""
        monkeypatch.chdir(tmp_path)
        entry = allowlist.AllowEntry(keys=("cuprum/gone.py",), reason="resolved")
        monkeypatch.setattr(gate, "load_allowlist", lambda _path: (entry,))
        monkeypatch.setattr(gate, "detect_findings", lambda: [])
        gate.check()
        assert capsys.readouterr().out == (
            "stale allow entry (cuprum/gone.py): remove it; no family in this "
            "scan reports any of its locations\n"
            "duplication gate passed\n"
        ), "Stale entries must be reported alongside a passing gate."

    def test_check_does_not_call_a_grown_family_gone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An entry its family outgrew is reported as needing widening.

        The entry matches nothing once a third copy joins the family, so it is
        stale by the matching rule — but the duplication is more present than
        ever. Reporting "the duplication is gone" here would invite deleting an
        entry that the very same run is reporting as blocking.
        """
        monkeypatch.chdir(tmp_path)
        entry = allowlist.AllowEntry(
            keys=("cuprum/one.py::first", "cuprum/two.py::second"), reason="two copies"
        )
        monkeypatch.setattr(gate, "load_allowlist", lambda _path: (entry,))
        monkeypatch.setattr(gate, "detect_findings", lambda: [_grown_finding()])
        with pytest.raises(SystemExit) as error:
            gate.check()
        assert error.value.code == 1, "A grown family must still block."
        output = capsys.readouterr().out
        assert "the duplication is gone" not in output, (
            "A family that grew a location is still duplicated."
        )
        assert "has grown to 3 locations" in output, (
            "The report must name why the entry stopped covering the family."
        )
        assert "widen it to match" in output, (
            "The report must direct the maintainer to widen, not delete."
        )

    def test_check_does_not_call_a_coincidental_match_growth(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A single-key entry still touching another family is not "grown".

        A one-key entry offers no evidence that the family it covered is the
        family still being reported: its key is a path glob, and a glob is
        exactly what makes a coincidental overlap possible. Telling the
        maintainer to widen the entry here would instruct them to re-authorise
        duplication the entry never described.
        """
        monkeypatch.chdir(tmp_path)
        entry = allowlist.AllowEntry(keys=("cuprum/one.py",), reason="a variant")
        monkeypatch.setattr(gate, "load_allowlist", lambda _path: (entry,))
        monkeypatch.setattr(gate, "detect_findings", lambda: [_grown_finding()])
        with pytest.raises(SystemExit) as error:
            gate.check()
        assert error.value.code == 1, "The un-covered family must still block."
        output = capsys.readouterr().out
        assert "widen it to match" not in output, (
            "A single-key entry cannot show that its own family grew."
        )
        assert "path-glob coincidence" in output, (
            "The overlap must be named as a coincidence, not as growth."
        )
        assert "remove it" in output, (
            "A coincidental overlap still leaves the entry covering nothing."
        )

    def test_check_reports_detector_schema_errors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Malformed detector reports exit cleanly instead of showing a traceback."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(gate, "load_allowlist", lambda _path: ())

        def raise_schema_error() -> list[detector.Finding]:
            """Raise the schema error a malformed report would produce."""
            msg = "nose report families must be an array"
            raise TypeError(msg)

        monkeypatch.setattr(gate, "detect_findings", raise_schema_error)
        with pytest.raises(SystemExit) as error:
            gate.check()

        assert error.value.code == 2, "Malformed detector reports must return two."
        assert capsys.readouterr().err == (
            "configuration error: nose report families must be an array\n"
        ), "Schema errors must use the configuration diagnostic."
        assert Path.cwd() == tmp_path, (
            "The check command must not change its caller's working directory."
        )

    def test_check_reports_a_version_mismatch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An unpinned detector fails the gate with a remediation message."""
        workspace = tmp_path
        stub = write_stub_nose(workspace, version="nose 0.19.0")
        monkeypatch.setenv("NOSE_BIN", str(stub))
        monkeypatch.chdir(workspace)
        monkeypatch.setattr(gate, "load_allowlist", lambda _path: ())
        with pytest.raises(SystemExit) as error:
            gate.check()
        assert error.value.code == 2, "A version mismatch must return two."
        assert "make install-nose" in capsys.readouterr().err, (
            "The mismatch diagnostic must name the install remediation."
        )

    def test_allow_reports_malformed_existing_entries(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Malformed existing allows exit cleanly instead of showing a traceback."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[[tool.duplication_gate.allow]]\nunit = "cuprum/a.py"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(gate, "PYPROJECT", pyproject)

        with pytest.raises(SystemExit) as error:
            gate.allow(first="cuprum/b.py", reason="reviewed exception")

        assert error.value.code == 2, "Malformed existing allows must return two."
        assert capsys.readouterr().err == (
            "configuration error: duplication_gate.allow[0] "
            "requires a non-empty reason\n"
        ), "Malformed allows must use the configuration diagnostic."

    def test_allow_reports_write_failures(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A filesystem failure while recording an allow exits cleanly."""
        write_error = OSError("read-only filesystem")

        def fail_write(*_args: object, **_kwargs: object) -> None:
            """Fail the manifest write with the injected error."""
            raise write_error

        monkeypatch.setattr(gate, "append_allow_entry", fail_write)

        with pytest.raises(SystemExit) as exit_error:
            gate.allow(first="cuprum/a.py", reason="reviewed exception")

        assert exit_error.value.code == 2, "Write failures must return two."
        assert capsys.readouterr().err == (
            "configuration error: read-only filesystem\n"
        ), "Write failures must use the configuration diagnostic."

    def test_allow_rejects_malformed_keys(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An absolute key is refused before anything is written."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'x'\n", encoding="utf-8")
        monkeypatch.setattr(gate, "PYPROJECT", pyproject)

        with pytest.raises(SystemExit) as error:
            gate.allow(first="/cuprum/a.py", reason="reviewed exception")

        assert error.value.code == 2, "Malformed keys must return two."
        assert "repository-relative" in capsys.readouterr().err, (
            "The diagnostic must explain the key requirement."
        )
        assert "duplication_gate" not in pyproject.read_text(encoding="utf-8"), (
            "A rejected key must not be recorded."
        )

    @pytest.mark.parametrize(
        ("second", "expected_keys"),
        [
            pytest.param(None, ("cuprum/a.py",), id="unit"),
            pytest.param(
                ["cuprum/b.py::beta"],
                ("cuprum/a.py", "cuprum/b.py::beta"),
                id="members",
            ),
        ],
    )
    def test_allow_cli_round_trips_unit_and_members(
        self,
        tmp_path: Path,
        second: list[str] | None,
        expected_keys: tuple[str, ...],
    ) -> None:
        """The real allow CLI records both supported exception forms."""
        workspace = copied_gate_workspace(tmp_path)
        arguments = ["allow", "--first", "cuprum/a.py"]
        for key in second or ():
            arguments.extend(("--second", key))
        arguments.extend(("--reason", "reviewed exception"))

        result = run_gate_command(workspace, *arguments)
        assert result.returncode == 0, result.stderr
        entries = allowlist.load_allowlist(workspace / "pyproject.toml")
        assert entries[0].keys == expected_keys, "CLI must retain its requested keys."
        assert entries[0].reason == "reviewed exception", (
            "CLI must retain the supplied reason."
        )

    def test_check_cli_passes_with_a_stub_detector(self, tmp_path: Path) -> None:
        """The gate exits zero through its real CLI when every family is allowed."""
        workspace = copied_gate_workspace(tmp_path)
        stub = write_stub_nose(tmp_path)
        (workspace / "pyproject.toml").write_text(
            textwrap.dedent(
                """\
                [tool.nose]
                version = "0.20.0"
                roots = ["cuprum"]
                mode = "syntax"
                min-size = 24

                [[tool.duplication_gate.allow]]
                members = ["cuprum/a.py", "cuprum/b.py"]
                reason = "parallel wire contracts"
                """
            ),
            encoding="utf-8",
        )
        result = run_gate_command(
            workspace,
            "check",
            environment=gate_environment(workspace, NOSE_BIN=str(stub)),
        )
        assert result.returncode == 0, result.stderr
        assert "duplication gate passed" in result.stdout, (
            "An allowed family must leave the gate passing."
        )
