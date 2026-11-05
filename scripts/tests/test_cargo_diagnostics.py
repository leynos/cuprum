"""Exercise redaction and retention of Cargo preparation diagnostics."""

from __future__ import annotations

import json
import typing as typ

import pytest

from scripts.cargo_diagnostics import (
    CargoDiagnostic,
    is_secret_environment_name,
    record_cargo_diagnostic,
    redact_environment_values,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("GH_TOKEN", True, id="token"),
        pytest.param("CARGO_REGISTRIES_CRATES_IO_TOKEN", True, id="registry-token"),
        pytest.param("AWS_SECRET_ACCESS_KEY", True, id="secret"),
        pytest.param("DB_PASSWORD", True, id="password"),
        pytest.param("SIGNING_CREDENTIAL", True, id="credential"),
        pytest.param("GITHUB_AUTH", True, id="auth"),
        pytest.param("gh_token", True, id="lower-case"),
        pytest.param("CARGO_HOME", False, id="cargo-home"),
        pytest.param("RUSTUP_HOME", False, id="rustup-home"),
        pytest.param("PATH", False, id="path"),
    ],
)
def test_only_secret_shaped_names_are_treated_as_secret(
    name: str, expected: bool
) -> None:
    """Only names carrying a secret marker classify their value as secret."""
    assert is_secret_environment_name(name) is expected, (
        f"{name} must be classified as {'secret' if expected else 'non-secret'}"
    )


def test_redaction_replaces_every_occurrence_of_a_secret_value() -> None:
    """A secret echoed twice is redacted in both places."""
    environment = {"GH_TOKEN": "ghs-super-secret", "PATH": "/usr/bin"}

    redacted = redact_environment_values(
        "token=ghs-super-secret; retry ghs-super-secret",
        environment,
    )

    assert redacted == "token=<redacted>; retry <redacted>", (
        "a value echoed twice must be redacted at every occurrence"
    )


def test_redaction_leaves_short_and_non_secret_values_alone() -> None:
    """Short values and ordinary variables survive so the diagnostic stays legible."""
    environment = {"GH_TOKEN": "abc", "PATH": "/opt/tools"}

    redacted = redact_environment_values("abc /opt/tools", environment)

    assert redacted == "abc /opt/tools", (
        "short values collide with ordinary words, and PATH is not secret"
    )


def test_redaction_leaves_unrelated_text_unchanged() -> None:
    """Text that contains no secret value is returned untouched."""
    assert redact_environment_values("registry index unavailable", {}) == (
        "registry index unavailable"
    ), "text with no secret value must be returned untouched"


def test_a_record_keeps_the_invocation_context_and_streams(tmp_path: Path) -> None:
    """One record carries the operation, exit status, timing, and both streams."""
    diagnostics = tmp_path / "cargo-diagnostics.jsonl"

    record_cargo_diagnostic(
        diagnostics,
        CargoDiagnostic(
            operation="cargo fetch --locked",
            working_directory="/repo/rust",
            cargo_home="/run/cargo-home",
            exit_code=101,
            elapsed_seconds=1.5,
            stdout="standard output",
            stderr="standard error",
        ),
    )

    [record] = [
        json.loads(line)
        for line in diagnostics.read_text(encoding="utf-8").splitlines()
    ]
    assert record["operation"] == "cargo fetch --locked", (
        "the record must name the operation it observed"
    )
    assert record["working_directory"] == "/repo/rust", (
        "the record must name the directory Cargo ran in"
    )
    assert record["cargo_home"] == "/run/cargo-home", (
        "the record must name the Cargo home it used"
    )
    assert record["exit_code"] == 101, "the record must retain the exit status"
    assert record["elapsed_seconds"] == pytest.approx(1.5), (
        "the record must retain the measured duration"
    )
    assert record["stdout"] == "standard output", (
        "the record must retain standard output"
    )
    assert record["stderr"] == "standard error", "the record must retain standard error"


def test_records_append_so_a_failure_keeps_its_preparation_context(
    tmp_path: Path,
) -> None:
    """Earlier successful steps remain beside the invocation that failed."""
    diagnostics = tmp_path / "cargo-diagnostics.jsonl"

    for operation, exit_code in (
        ("cargo version", 0),
        ("cargo fetch --locked", 17),
    ):
        record_cargo_diagnostic(
            diagnostics,
            CargoDiagnostic(
                operation=operation,
                working_directory=str(tmp_path),
                cargo_home=None,
                exit_code=exit_code,
                elapsed_seconds=0.25,
                stdout="",
                stderr="",
            ),
        )

    records = [
        json.loads(line)
        for line in diagnostics.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["operation"] for record in records] == [
        "cargo version",
        "cargo fetch --locked",
    ], "a failure must be readable in the context of the steps before it"
