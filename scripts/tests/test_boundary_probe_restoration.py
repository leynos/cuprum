"""Test unsafe-probe classification and byte-safe source restoration."""

from __future__ import annotations

import typing as typ

import pytest

from scripts import boundary_probe
from scripts.boundary_probe import probe_appended, unsafe_was_forbidden

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_unsafe_detection_requires_both_failure_and_the_forbid_diagnostic() -> None:
    """An unrelated compile failure must not be read as a successful rejection."""
    assert unsafe_was_forbidden(1, "error: forbid(unsafe_code)"), (
        "a failed compile naming forbid(unsafe_code) is a rejection"
    )
    assert unsafe_was_forbidden(1, "error: -F unsafe-code"), (
        "the -F unsafe-code spelling is the same rejection"
    )
    assert not unsafe_was_forbidden(0, "error: forbid(unsafe_code)"), (
        "a diagnostic without a failure exit is not a rejection"
    )
    assert not unsafe_was_forbidden(1, "error: unused import"), (
        "an unrelated failure is not a rejection"
    )


def test_a_probe_is_removed_even_when_compilation_fails(tmp_path: Path) -> None:
    """The context manager restores the source on both exit paths."""
    target = tmp_path / "lib.rs"
    target.write_text("pub fn production() {}\n", encoding="utf-8")
    original = target.read_text(encoding="utf-8")
    probe = "pub fn unsafe_probe() { unsafe {} }"
    with probe_appended(target, probe):
        assert probe in target.read_text(encoding="utf-8"), "the probe was not applied"
    assert target.read_text(encoding="utf-8") == original, (
        "the probe was not removed on the normal path"
    )
    msg = "boom"
    with (
        pytest.raises(ValueError, match=msg),
        probe_appended(target, probe),
    ):
        raise ValueError(msg)
    assert target.read_text(encoding="utf-8") == original, (
        "the probe was not removed when the body raised"
    )


def test_check_target_rejects_an_unrelated_failure_and_restores_source(
    tmp_path: Path,
) -> None:
    """The integrated probe check requires its diagnostic and restores source."""
    target = tmp_path / "lib.rs"
    original = b"pub fn production() {}\n"
    target.write_bytes(original)
    logs = tmp_path / "logs"
    logs.mkdir()
    compiled_source: list[bytes] = []

    def compile_workspace(_workspace: Path) -> tuple[int, str]:
        """Record the temporary source and return an unrelated failure."""
        compiled_source.append(target.read_bytes())
        return 1, "error: unexpected compiler failure"

    configuration = boundary_probe._ProbeConfiguration(
        probes=("pub fn unsafe_probe() { unsafe {} }",),
        compile_workspace=compile_workspace,
    )
    with pytest.raises(RuntimeError, match="did not reject unsafe probe"):
        boundary_probe.check_target(
            workspace=tmp_path,
            path=target,
            logs=logs,
            configuration=configuration,
        )

    assert len(compiled_source) == 1, "the unsafe probe must reach the compiler"
    assert b"unsafe_probe" in compiled_source[0], "the temporary probe was not applied"
    assert target.read_bytes() == original, (
        "the rejected probe must restore original bytes"
    )
    assert len(list(logs.iterdir())) == 1, "the rejection diagnostic must be retained"
