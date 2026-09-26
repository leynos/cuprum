"""Injected-seam tests for the duplication gate's command inputs.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``
(``scripts/tests/test_duplication_gate_commands.py``), the merged revision of
PR #276, under the ISC terms in ``LICENSE``.

``check`` reaches its allowlist reader and its detector through two injectable
seams, so a failure in either can be raised without a real file, a real binary,
or a real report. These tests pin that translation layer: which failures are
named, which are wrapped, and which must propagate untouched.
"""

from __future__ import annotations

import tomllib

import pytest

from scripts.tests.duplication_gate_test_support import (
    allowlist,
    detector,
    gate,
)


class TestCheckInputSeams:
    """The two inputs `check` reads through, and their failure translation."""

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(OSError("unreadable configuration"), id="allowlist-io"),
            pytest.param(OSError("detector executable unavailable"), id="detector-io"),
        ],
    )
    def test_check_inputs_wrap_environment_failures(self, error: Exception) -> None:
        """Injected reader and detector failures become explicit gate errors."""
        if str(error).startswith("unreadable"):

            def reader(_path: object) -> tuple[allowlist.AllowEntry, ...]:
                """Fail every allowlist read with the injected error."""
                raise error

            def detect() -> list[detector.Finding]:
                """Report no findings, so only the reader can fail."""
                return []

        else:

            def reader(_path: object) -> tuple[allowlist.AllowEntry, ...]:
                """Report no entries, so only the detector can fail."""
                return ()

            def detect() -> list[detector.Finding]:
                """Fail every detection with the injected error."""
                raise error

        with pytest.raises(gate.GateExecutionError, match=str(error)):
            gate._check_inputs(allowlist_reader=reader, detector=detect)

    def test_read_allowlist_returns_the_reader_result(self) -> None:
        """A successful reader result reaches the caller unchanged."""
        entry = allowlist.AllowEntry(keys=("cuprum/a.py",), reason="reviewed")

        def reader(_path: object) -> tuple[allowlist.AllowEntry, ...]:
            """Return the single reviewed entry under test."""
            return (entry,)

        assert gate._read_allowlist(reader) == (entry,), (
            "The reader's entries must pass through unchanged."
        )

    def test_detect_findings_returns_the_detector_result(self) -> None:
        """A successful detector result reaches the caller unchanged."""
        finding = detector.Finding(
            witness="copy-paste",
            value=9.0,
            locations=(
                detector.Location(file="cuprum/a.py", start=1, end=2, name=None),
                detector.Location(file="cuprum/b.py", start=1, end=2, name=None),
            ),
        )

        def detect() -> list[detector.Finding]:
            """Return the single planted finding under test."""
            return [finding]

        assert gate._detect_findings(detect) == [finding], (
            "The detector's findings must pass through unchanged."
        )

    @pytest.mark.parametrize(
        ("error", "expected_type", "expected_message"),
        [
            pytest.param(
                OSError("unreadable configuration"),
                gate.GateExecutionError,
                "cannot load duplication allowlist: unreadable configuration",
                id="os-error",
            ),
            pytest.param(
                tomllib.TOMLDecodeError("bad table", "", 0),
                gate.GateExecutionError,
                "cannot load duplication allowlist: bad table (at end of document)",
                id="toml-error",
            ),
        ],
    )
    def test_read_allowlist_translates_environment_failures(
        self,
        error: Exception,
        expected_type: type[Exception],
        expected_message: str,
    ) -> None:
        """Unreadable configuration becomes an explicit execution error."""

        def reader(_path: object) -> tuple[allowlist.AllowEntry, ...]:
            """Fail every allowlist read with the injected error."""
            raise error

        with pytest.raises(expected_type) as raised:
            gate._read_allowlist(reader)

        assert str(raised.value) == expected_message, "Diagnostic must name the cause."
        assert raised.value.__cause__ is error, "The original error must be the cause."

    @pytest.mark.parametrize(
        ("error", "expected_type", "expected_message"),
        [
            pytest.param(
                OSError("detector executable unavailable"),
                gate.GateExecutionError,
                "nose detector failed: detector executable unavailable",
                id="os-error",
            ),
            pytest.param(
                TypeError("families must be an array"),
                gate.GateConfigError,
                "families must be an array",
                id="type-error",
            ),
            pytest.param(
                ValueError("value must be a number"),
                gate.GateConfigError,
                "value must be a number",
                id="value-error",
            ),
        ],
    )
    def test_detect_findings_translates_detector_failures(
        self,
        error: Exception,
        expected_type: type[Exception],
        expected_message: str,
    ) -> None:
        """Execution failures and schema violations use distinct gate errors."""

        def detect() -> list[detector.Finding]:
            """Fail every detection with the injected error."""
            raise error

        with pytest.raises(expected_type) as raised:
            gate._detect_findings(detect)

        assert str(raised.value) == expected_message, "Diagnostic must name the cause."
        assert raised.value.__cause__ is error, "The original error must be the cause."

    def test_detect_findings_does_not_wrap_runtime_errors(self) -> None:
        """Programming faults from a detector are not reclassified as I/O failures."""
        error = RuntimeError("detector runtime failed")

        def detect() -> list[detector.Finding]:
            """Raise a bare runtime error, which must not be wrapped."""
            raise error

        with pytest.raises(RuntimeError) as raised:
            gate._detect_findings(detect)

        assert raised.value is error, "Runtime errors must propagate unchanged."

    def test_read_allowlist_passes_configuration_errors_through(self) -> None:
        """An allowlist configuration error is not rewrapped."""
        error = gate.GateConfigError("duplication_gate.allow[0] requires a reason")

        def reader(_path: object) -> tuple[allowlist.AllowEntry, ...]:
            """Fail every allowlist read with a configuration error."""
            raise error

        with pytest.raises(gate.GateConfigError) as raised:
            gate._read_allowlist(reader)

        assert raised.value is error, "The original configuration error must propagate."

    def test_detect_findings_passes_configuration_errors_through(self) -> None:
        """A detector configuration error is not rewrapped."""
        error = gate.GateConfigError("nose 0.19.0 is installed but 0.20.0 is pinned")

        def detect() -> list[detector.Finding]:
            """Fail every detection with a configuration error."""
            raise error

        with pytest.raises(gate.GateConfigError) as raised:
            gate._detect_findings(detect)

        assert raised.value is error, "The original configuration error must propagate."
