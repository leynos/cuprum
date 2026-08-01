"""Tests for the native wheel build and its snapshot output."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests assert trusted maturin command handling.
import sys
import typing as typ
import zipfile

import pytest

from cuprum.unittests._maturin_pin_support import read_expected_maturin_version
from tests.helpers.docs import repo_root
from tests.helpers.maturin import (
    MaturinBuildError,
    build_native_wheel_artifact,
    maturin_script_locatable,
    toolchain_available,
    wheel_build_snapshot,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

    from syrupy.assertion import SnapshotAssertion


def _build_with_fake_subprocess_run(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_run: cabc.Callable[..., subprocess.CompletedProcess[str]],
) -> pth.Path:
    """Build the native wheel while replacing ``subprocess.run``."""
    monkeypatch.setattr(subprocess, "run", fake_run)
    return build_native_wheel_artifact(repo_root(), tmp_path / "wheelhouse")


def test_build_native_wheel_artifact_uses_locked_cargo_deps(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native wheel builds pass ``--locked`` through to maturin."""
    captured_command: list[str] = []

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """Record the command and create the expected wheel artifact.

        Returns
        -------
        subprocess.CompletedProcess[str]
            A successful completed process for the recorded command.
        """
        captured_command.extend(command)
        (tmp_path / "wheelhouse" / "cuprum-test.whl").touch()
        return subprocess.CompletedProcess(command, 0, "", "")

    wheel_path = _build_with_fake_subprocess_run(tmp_path, monkeypatch, fake_run)

    assert wheel_path.name == "cuprum-test.whl", (
        "native wheel helper should return the wheel created by fake maturin"
    )
    assert "--locked" in captured_command, (
        "native wheel build should pass --locked through to maturin"
    )


def test_build_native_wheel_artifact_reports_maturin_stderr(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native wheel build failures include the command and captured stderr."""

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """Raise a deterministic maturin command failure.

        Raises
        ------
        CalledProcessError
            Always, to simulate a failed maturin invocation.
        """
        assert _kwargs.get("capture_output") is True, (
            "native wheel builds should capture maturin output"
        )
        assert _kwargs.get("text") is True, (
            "native wheel builds should decode captured output as text"
        )
        assert _kwargs.get("check") is True, (
            "native wheel builds should require maturin command success"
        )
        raise subprocess.CalledProcessError(
            101,
            command,
            output="stdout text",
            stderr="cargo fetch failed",
        )

    with pytest.raises(MaturinBuildError) as exc_info:
        _build_with_fake_subprocess_run(tmp_path, monkeypatch, fake_run)

    error_text = str(exc_info.value)
    assert exc_info.value.stderr == "cargo fetch failed", (
        "maturin build errors should preserve raw captured stderr"
    )
    assert "python" in error_text, (
        "maturin failure diagnostics should include the Python executable"
    )
    assert "maturin build" in error_text, (
        "maturin failure diagnostics should include the build command"
    )
    assert "cargo fetch failed" in error_text, (
        "maturin failure diagnostics should include captured stderr"
    )


@pytest.mark.timeout(0)
def test_maturin_wheel_build_snapshot(
    tmp_path: pth.Path,
    snapshot: SnapshotAssertion,
) -> None:
    """Native wheel metadata and layout match the expected maturin output."""
    root = repo_root()
    expected = read_expected_maturin_version(root)
    if not toolchain_available():
        pytest.skip("Rust toolchain unavailable.")
    if not maturin_script_locatable():
        # A layered/ephemeral interpreter (for example, a `uv run --with
        # ...` overlay, as used by the mutmut mutation-testing workflow)
        # can import the maturin module via sys.path while sys.prefix
        # points at a temporary environment that never received maturin's
        # own compiled script. maturin's `python -m maturin` entry point
        # then fails with "Unable to find `maturin` script" before it can
        # invoke cargo. See tests.helpers.maturin.maturin_script_locatable
        # for the detection logic, which mirrors maturin's own lookup.
        pytest.skip(
            "maturin's compiled script is not locatable via this "
            "interpreter's sysconfig scripts directories (sys.prefix="
            f"{sys.prefix!r}); this is expected in layered/ephemeral "
            "interpreters such as a `uv run --with ...` overlay."
        )

    wheel_path = build_native_wheel_artifact(root, tmp_path / "wheelhouse")
    snapshot_payload = wheel_build_snapshot(wheel_path)
    assert snapshot_payload["generator"] == expected, (
        f"Expected generator {expected!r}, found {snapshot_payload['generator']!r}"
    )
    assert snapshot_payload == snapshot, (
        "Built wheel metadata, file list, and build settings changed."
    )


@pytest.mark.parametrize(
    ("members", "expected_message"),
    [
        pytest.param(
            {"cuprum-0.1.0.dist-info/METADATA": "Name: cuprum\n"},
            "wheel is missing .dist-info/WHEEL metadata",
            id="missing_wheel",
        ),
        pytest.param(
            {"cuprum-0.1.0.dist-info/WHEEL": "Root-Is-Purelib: false\n"},
            "wheel is missing .dist-info/METADATA metadata",
            id="missing_metadata",
        ),
    ],
)
def test_wheel_build_snapshot_reports_missing_dist_info(
    tmp_path: pth.Path,
    members: dict[str, str],
    expected_message: str,
) -> None:
    """A wheel missing either dist-info member fails with AssertionError.

    ``wheel_build_snapshot`` documents ``AssertionError`` for absent metadata,
    so neither member may surface as the ``KeyError`` that ``ZipFile.read``
    would otherwise raise. ``METADATA`` is the easier one to get wrong:
    ``metadata_name`` is derived from the ``WHEEL`` entry by string
    substitution rather than looked up in the archive, so it needs an explicit
    membership check to honour the documented contract.
    """
    whl_path = tmp_path / "cuprum-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(whl_path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)

    # `match=` searches rather than fullmatches, so it would still pass if the
    # diagnostic gained a prefix or suffix. Compare the message exactly.
    with pytest.raises(AssertionError) as exc_info:
        wheel_build_snapshot(whl_path)

    assert str(exc_info.value) == expected_message, (
        f"expected exactly {expected_message!r}, found {str(exc_info.value)!r}"
    )
