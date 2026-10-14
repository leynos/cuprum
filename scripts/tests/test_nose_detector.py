"""Report parsing and finding normalization for the nose detector wrapper.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``
(``scripts/tests/test_nose_detector.py``), the merged revision of PR #276,
under the ISC terms in ``LICENSE``.

Everything downstream of the detector's exit status is validated here: a
malformed report, an unreadable payload, a timeout and a non-zero exit must all
fail closed with an actionable diagnostic rather than an empty findings list.
"""

from __future__ import annotations

import copy
import re
import typing as typ

import pytest

from scripts.tests.duplication_gate_test_support import (
    STUB_REPORT,
    detector,
    stub_runner,
    stub_settings,
)

if typ.TYPE_CHECKING:
    from collections import abc as cabc


class TestRunDetector:
    """Report parsing and finding normalization."""

    def test_normalizes_a_stub_report(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stub report becomes one ordered finding with both locations."""
        monkeypatch.setenv("NOSE_BIN", "/stub/nose")
        findings = detector.run_detector(stub_settings(), runner=stub_runner())
        assert len(findings) == 1, "The stub report contains one family."
        assert findings[0].label == "cuprum/a.py:1-20 ~ cuprum/b.py:30-49", (
            "Findings must report both spans."
        )

    @pytest.mark.parametrize(
        ("families", "expected_values", "expected_labels"),
        [
            pytest.param(
                [
                    {
                        "witness": "copy-paste",
                        "value": 5.0,
                        "locations": [
                            {
                                "file": "cuprum/z.py",
                                "start": 1,
                                "end": 2,
                                "name": None,
                            },
                            {
                                "file": "cuprum/y.py",
                                "start": 1,
                                "end": 2,
                                "name": None,
                            },
                        ],
                    },
                    {
                        "witness": "exact",
                        "value": 9.0,
                        "locations": [
                            {
                                "file": "cuprum/a.py",
                                "start": 1,
                                "end": 2,
                                "name": "run",
                            },
                            {
                                "file": "cuprum/b.py",
                                "start": 1,
                                "end": 2,
                                "name": "run",
                            },
                        ],
                    },
                ],
                [9.0, 5.0],
                [
                    "cuprum/a.py:1-2 run ~ cuprum/b.py:1-2 run",
                    "cuprum/z.py:1-2 ~ cuprum/y.py:1-2",
                ],
                id="descending-value",
            ),
            pytest.param(
                [
                    {
                        "witness": "copy-paste",
                        "value": 5.0,
                        "locations": [
                            {
                                "file": "cuprum/z.py",
                                "start": 1,
                                "end": 2,
                                "name": None,
                            },
                            {
                                "file": "cuprum/y.py",
                                "start": 1,
                                "end": 2,
                                "name": None,
                            },
                        ],
                    },
                    {
                        "witness": "copy-paste",
                        "value": 5.0,
                        "locations": [
                            {
                                "file": "cuprum/b.py",
                                "start": 1,
                                "end": 2,
                                "name": None,
                            },
                            {
                                "file": "cuprum/a.py",
                                "start": 1,
                                "end": 2,
                                "name": None,
                            },
                        ],
                    },
                ],
                [5.0, 5.0],
                [
                    "cuprum/b.py:1-2 ~ cuprum/a.py:1-2",
                    "cuprum/z.py:1-2 ~ cuprum/y.py:1-2",
                ],
                id="location-label-tie-break",
            ),
        ],
    )
    def test_orders_findings(
        self,
        families: list[dict[str, object]],
        expected_values: list[float],
        expected_labels: list[str],
    ) -> None:
        """Findings sort by descending value, then by normalized location label."""
        findings = detector.normalize_findings({"families": families})

        assert [finding.value for finding in findings] == expected_values, (
            "Higher-value families must sort first."
        )
        assert [finding.label for finding in findings] == expected_labels, (
            "Equal values must order by normalized location label."
        )

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(7, 7.0, id="integer"),
            pytest.param(7.5, 7.5, id="float"),
        ],
    )
    def test_normalizes_numeric_values_to_float(
        self,
        value: float,
        expected: float,
    ) -> None:
        """Integer and floating-point family values both normalize to float."""
        report = copy.deepcopy(STUB_REPORT)
        typ.cast("dict[str, typ.Any]", report)["families"][0]["value"] = value
        findings = detector.normalize_findings(report)
        assert isinstance(findings[0].value, float), (
            "Normalization must coerce family values to float."
        )
        assert findings[0].value == expected, "Normalization must preserve the value."

    @pytest.mark.parametrize(
        ("mutate", "diagnostic"),
        [
            pytest.param(
                lambda report: report.__setitem__("families", {}),
                "families must be an array",
                id="families-object",
            ),
            pytest.param(
                lambda report: report["families"][0].__setitem__("value", "high"),
                "value must be a number",
                id="string-value",
            ),
            pytest.param(
                lambda report: report["families"][0].update({"value": True}),
                "value must be a number",
                id="boolean-value",
            ),
            pytest.param(
                lambda report: report["families"][0].pop("value"),
                "value must be a number",
                id="missing-value",
            ),
            pytest.param(
                lambda report: report["families"][0].__setitem__(
                    "locations", "cuprum/a.py"
                ),
                "locations must be an array",
                id="string-locations",
            ),
            pytest.param(
                lambda report: report["families"][0].__setitem__(
                    "locations", b"cuprum/a.py"
                ),
                "locations must be an array",
                id="bytes-locations",
            ),
            pytest.param(
                lambda report: report["families"][0].__setitem__("locations", []),
                "locations must not be empty",
                id="empty-locations",
            ),
            pytest.param(
                lambda report: report["families"][0]["locations"].__setitem__(
                    0, "cuprum/a.py"
                ),
                "families[0].locations[0] must be a table",
                id="malformed-first-location",
            ),
            pytest.param(
                lambda report: report["families"][0]["locations"][0].__setitem__(
                    "start", 0
                ),
                "start must be a positive integer",
                id="zero-start",
            ),
            pytest.param(
                lambda report: report["families"][0]["locations"][0].__setitem__(
                    "end", 0
                ),
                "end must not precede start",
                id="inverted-span",
            ),
            pytest.param(
                lambda report: report["families"][0]["locations"][0].__setitem__(
                    "name", ""
                ),
                "name must be a non-empty string or null",
                id="empty-name",
            ),
        ],
    )
    def test_rejects_malformed_reports(
        self,
        mutate: cabc.Callable[[dict[str, typ.Any]], None],
        diagnostic: str,
    ) -> None:
        """Schema violations fail at the detector boundary."""
        report = copy.deepcopy(STUB_REPORT)
        mutate(typ.cast("dict[str, typ.Any]", report))
        with pytest.raises(detector.GateConfigError, match=re.escape(diagnostic)):
            detector.normalize_findings(report)

    def test_rejects_unreadable_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-JSON detector output fails with an execution error."""
        monkeypatch.setenv("NOSE_BIN", "/stub/nose")

        def runner(command: cabc.Sequence[str]) -> str:
            """Answer the version probe, then emit non-JSON output."""
            return "nose 0.20.0\n" if "--version" in command else "not json"

        with pytest.raises(detector.GateExecutionError, match="not valid JSON"):
            detector.run_detector(stub_settings(), runner=runner)

    def test_run_command_reports_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A slow detector becomes an actionable execution error."""

        def timeout(*_args: object, **_kwargs: object) -> typ.NoReturn:
            """Simulate the detector exceeding its bounded timeout."""
            raise detector.subprocess.TimeoutExpired(["nose", "query"], 120)

        monkeypatch.setattr(detector.subprocess, "run", timeout)

        with pytest.raises(
            detector.GateExecutionError, match="timed out after 120 seconds"
        ):
            detector._run_command(("nose", "query"))

    def test_run_command_reports_a_non_zero_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A detector that fails carries its status and diagnostic out."""

        def failing(
            *_args: object, **_kwargs: object
        ) -> detector.subprocess.CompletedProcess[str]:
            """Return the non-zero exit a rejected query produces."""
            return detector.subprocess.CompletedProcess(
                args=["nose", "query"], returncode=2, stdout="", stderr="bad query\n"
            )

        monkeypatch.setattr(detector.subprocess, "run", failing)

        with pytest.raises(
            detector.GateExecutionError,
            match=r"nose exited with status 2: bad query",
        ):
            detector._run_command(("nose", "query"))

    def test_run_command_reports_an_execution_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrunnable detector points at the install remediation."""

        def unrunnable(*_args: object, **_kwargs: object) -> typ.NoReturn:
            """Simulate an unusable detector binary."""
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(detector.subprocess, "run", unrunnable)

        with pytest.raises(
            detector.GateExecutionError,
            match=r"cannot run nose: .*Permission denied.*make install-nose",
        ):
            detector._run_command(("nose", "query"))

    def test_run_command_rejects_an_empty_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A root holding no source files is a provisioning error, not a pass.

        nose answers an empty scope with exit 0 and a summary that matches a
        clean tree, so without this the gate would report success for a
        mistyped or empty ``roots`` entry.
        """

        def empty_scope(
            *_args: object, **_kwargs: object
        ) -> detector.subprocess.CompletedProcess[str]:
            """Return nose's successful-but-empty answer to an empty scope."""
            return detector.subprocess.CompletedProcess(
                args=["nose", "query"],
                returncode=0,
                stdout='{"summary": {"families": 0, "shown": 0}}',
                stderr="warning: no supported source files found under: docs\n",
            )

        monkeypatch.setattr(detector.subprocess, "run", empty_scope)

        with pytest.raises(
            detector.GateExecutionError,
            match=r"proves nothing about the tree.*no supported source files",
        ):
            detector._run_command(("nose", "query"))
