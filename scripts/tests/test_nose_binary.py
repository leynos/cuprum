"""Protocol tests for the nose detector command boundary.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``
(``scripts/tests/test_nose_detector.py``), the merged revision of PR #276,
under the ISC terms in ``LICENSE``.

These cover discovery and version verification of the pinned binary, and the
exact argument vector the gate hands it. Both are contracts the repository's
own pin agreement depends on.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest

from scripts.tests.duplication_gate_test_support import (
    detector,
    stub_runner,
    stub_settings,
    write_stub_nose,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from syrupy.assertion import SnapshotAssertion


class TestResolveBinary:
    """Discovery and version verification of the pinned binary."""

    def test_accepts_the_pinned_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A binary reporting the pinned version is accepted."""
        stub = write_stub_nose(tmp_path)
        monkeypatch.setenv("NOSE_BIN", str(stub))
        assert detector.resolve_binary(stub_settings(), runner=stub_runner()) == str(
            stub
        ), "The pinned binary must be returned unchanged."

    def test_rejects_a_version_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A different installed version fails with a remediation hint."""
        stub = write_stub_nose(tmp_path, version="nose 0.19.0")
        monkeypatch.setenv("NOSE_BIN", str(stub))
        with pytest.raises(
            detector.GateExecutionError,
            match=r"reports 'nose 0\.19\.0'.*make install-nose",
        ):
            detector.resolve_binary(
                stub_settings(), runner=stub_runner(version="nose 0.19.0")
            )

    def test_reports_a_missing_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing detector fails with the install remediation."""
        monkeypatch.delenv("NOSE_BIN", raising=False)
        monkeypatch.setattr(detector, "_discover_binary", lambda: None)
        with pytest.raises(detector.GateExecutionError, match="make install-nose"):
            detector.resolve_binary(stub_settings(), runner=stub_runner())


class TestBuildCommand:
    """Translation of gate settings into a nose query command."""

    def test_pins_every_configured_setting(self, snapshot: SnapshotAssertion) -> None:
        """The whole argument vector is pinned, in order, from the settings."""
        settings = dc.replace(
            stub_settings(),
            roots=("cuprum", "openai_test_types.py"),
            mode="semantic",
            min_size=40,
            surface="all",
            top=30,
            exclude=("**/generated/**", "**/_vendor/**"),
        )

        command = detector.build_command("nose", settings)

        assert command == snapshot, (
            "Every configured setting must reach nose, in the documented order."
        )

    def test_default_surface_omits_the_all_term(
        self, snapshot: SnapshotAssertion
    ) -> None:
        """The default surface leaves nose on its ranked dashboard."""
        settings = dc.replace(
            stub_settings(),
            roots=("cuprum",),
            mode="syntax",
            min_size=24,
            surface="default",
            top=None,
            exclude=(),
        )

        command = detector.build_command("nose", settings)

        assert command == snapshot, (
            "The default surface must not widen the view or pass a ranking bound."
        )
