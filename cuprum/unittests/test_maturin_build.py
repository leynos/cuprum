"""Tests for the native wheel build and its snapshot output."""

from __future__ import annotations

import importlib.metadata
import subprocess  # ruff: ignore[suspicious-subprocess-import] - tests assert trusted maturin command handling.
import sys
import tomllib
import typing as typ
import zipfile

import pytest

from cuprum.unittests._maturin_pin_support import read_expected_maturin_version
from tests.helpers.docs import repo_root
from tests.helpers.maturin import (
    MaturinBuildError,
    build_native_wheel_artefact,
    maturin_script_locatable,
    toolchain_available,
    wheel_build_snapshot,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

    from syrupy.assertion import SnapshotAssertion


# Redacted into the syrupy snapshot in place of the built wheel's maturin
# generator version. The raw value is asserted against the pyproject pin in
# `test_maturin_wheel_build_snapshot`, so the snapshot itself stays stable
# across maturin bumps instead of churning on every pin update.
MATURIN_GENERATOR_PLACEHOLDER = "<maturin-version>"
# Redacted in place of the wheel's `Version` header, which is asserted against
# the project version first, so a release bump does not rewrite the snapshot.
PROJECT_VERSION_PLACEHOLDER = "<project-version>"


def _build_with_fake_subprocess_run(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_run: cabc.Callable[..., subprocess.CompletedProcess[str]],
) -> pth.Path:
    """Build the native wheel while replacing ``subprocess.run``."""
    monkeypatch.setattr(subprocess, "run", fake_run)
    return build_native_wheel_artefact(repo_root(), tmp_path / "wheelhouse")


def test_build_native_wheel_artefact_uses_locked_cargo_deps(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native wheel builds pass ``--locked`` through to maturin."""
    captured_command: list[str] = []

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """Record the command and create the expected wheel artefact."""
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


def test_build_native_wheel_artefact_reports_maturin_stderr(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native wheel build failures include the command and captured stderr."""

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """Raise a deterministic maturin command failure."""
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

    wheel_path = build_native_wheel_artefact(root, tmp_path / "wheelhouse")
    # One stable-ABI wheel serves every supported interpreter from 3.12 on.
    assert "-cp312-abi3-" in wheel_path.name, (
        f"native wheel must target the CPython 3.12 stable ABI: {wheel_path.name}"
    )
    snapshot_payload = wheel_build_snapshot(wheel_path)
    assert snapshot_payload["generator"] == expected, (
        f"Expected generator {expected!r}, found {snapshot_payload['generator']!r}"
    )
    assert not any(
        entry.startswith("cuprum/unittests/") for entry in snapshot_payload["entries"]
    ), "distribution wheels must exclude the in-package unittest suite"
    # The installed distribution's metadata carries `pyproject.toml`'s version
    # after the build backend's PEP 440 normalization (`0.2.0-beta1` becomes
    # `0.2.0b1`), which is the form a wheel's `Version` header must use.
    project_version = importlib.metadata.version("cuprum")
    wheel_version = snapshot_payload["metadata"]["version"]
    assert wheel_version == project_version, (
        f"native wheel version {wheel_version!r} != project {project_version!r}"
    )
    # The generator and project versions are pinned by the assertions above, so
    # the snapshot compares redacted placeholders instead of the raw strings and
    # stays stable across maturin and release bumps.
    redacted_payload = {
        **snapshot_payload,
        "generator": MATURIN_GENERATOR_PLACEHOLDER,
        "metadata": {
            **snapshot_payload["metadata"],
            "version": PROJECT_VERSION_PLACEHOLDER,
        },
    }
    assert redacted_payload == snapshot, (
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


def test_stable_abi_floor_matches_requires_python() -> None:
    """The native wheel's stable-ABI floor is the minimum supported Python.

    A lower floor would advertise wheels for unsupported interpreters; a higher
    one would silently send the oldest supported interpreter to the pure Python
    wheel.
    """
    root = repo_root()
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    requires_python = pyproject["project"]["requires-python"]
    manifest = tomllib.loads(
        (root / "rust" / "cuprum-rust" / "Cargo.toml").read_text(encoding="utf-8")
    )
    features = manifest["dependencies"]["pyo3"]["features"]
    abi3_floors = [feature for feature in features if feature.startswith("abi3-py")]

    major, minor = requires_python.removeprefix(">=").split(".")
    expected = f"abi3-py{major}{minor}"
    assert abi3_floors == [expected], (
        f"requires-python {requires_python} needs pyo3 feature {expected}, "
        f"got {abi3_floors}"
    )
