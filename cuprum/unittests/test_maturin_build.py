"""Unit tests for maturin pin synchronization and wheel build output."""

from __future__ import annotations

import importlib.metadata as im
import re
import shutil
import subprocess  # noqa: S404 - tests assert trusted maturin command handling.
import sys
import typing as typ
import zipfile

import pytest

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


_MATURIN_PIN_RE = re.compile(r"maturin==(\d+\.\d+\.\d+)")
_WORKFLOW_PIN_RE = re.compile(r'MATURIN_VERSION:\s*"(\d+\.\d+\.\d+)"')
_ACTION_PIN_RE = re.compile(r'default:\s*"(\d+\.\d+\.\d+)"')
_AARCH64_CONTAINER_PIN_RE = re.compile(
    r"^\s*MANYLINUX_AARCH64_CONTAINER:\s*([^\s#]+)\s+#\s*\S.*$",
    re.MULTILINE,
)
_AARCH64_CONTAINER_USAGE_RE = re.compile(
    r"^\s*container:\s*\$\{\{\s*env\.MANYLINUX_AARCH64_CONTAINER\s*\}\}\s*$",
    re.MULTILINE,
)
_MANYLINUX_CONTAINER_SHA256_RE = re.compile(
    r"^ghcr\.io/rust-cross/manylinux_2_28-cross@sha256:[0-9a-f]{64}$"
)


def _require_pin_match(
    match: re.Match[str] | None,
    location: str,
    *,
    subject: str = "maturin version pin",
) -> str:
    """Return the captured version from a pin match or raise with the location."""
    if match is None:
        msg = f"Could not locate {subject} in {location}"
        raise AssertionError(msg)
    return match.group(1)


def _read_text(root: pth.Path, relative: str) -> str:
    """Read a repository-relative UTF-8 text file.

    Deliberately uncached. A full run of this module reads seven files —
    ``pyproject.toml`` and ``build-wheels.yml`` three times each, ``action.yml``
    once — totalling roughly 93 microseconds against a module runtime of about
    0.8 seconds, so a cache could save at most four redundant reads and about
    0.006% of the time.

    Against that, a cache would have to stay correct: these tests assert on
    repository files, and a memoized read would serve stale content to any
    future test that writes one. Paying a correctness hazard in a test helper
    for a saving three orders of magnitude below the noise is the wrong trade.
    """
    return (root / relative).read_text(encoding="utf-8")


def _read_expected_maturin_version(root: pth.Path) -> str:
    """Read the maturin version pinned as the dev dependency in pyproject.toml."""
    return _require_pin_match(
        _MATURIN_PIN_RE.search(_read_text(root, "pyproject.toml")),
        "pyproject.toml",
    )


def _read_maturin_pins(root: pth.Path) -> dict[str, str]:
    """Read the maturin version pins from every synchronized location."""
    return {
        # Reuse the dev-dependency reader so "how to read the pyproject pin"
        # lives in exactly one place.
        "pyproject.toml": _read_expected_maturin_version(root),
        "build-wheels.yml": _require_pin_match(
            _WORKFLOW_PIN_RE.search(
                _read_text(root, ".github/workflows/build-wheels.yml"),
            ),
            ".github/workflows/build-wheels.yml",
        ),
        "build-wheels/action.yml": _require_pin_match(
            _ACTION_PIN_RE.search(
                _read_text(root, ".github/actions/build-wheels/action.yml"),
            ),
            ".github/actions/build-wheels/action.yml",
        ),
    }


def _read_manylinux_aarch64_container_ref(root: pth.Path) -> str:
    """Read the pinned manylinux aarch64 container reference from the workflow."""
    return _require_pin_match(
        _AARCH64_CONTAINER_PIN_RE.search(
            _read_text(root, ".github/workflows/build-wheels.yml"),
        ),
        ".github/workflows/build-wheels.yml",
        subject="MANYLINUX_AARCH64_CONTAINER pin",
    )


def _workflow_uses_manylinux_aarch64_container_ref(root: pth.Path) -> bool:
    """Report whether the workflow references the pinned manylinux container."""
    workflow = _read_text(root, ".github/workflows/build-wheels.yml")
    return _AARCH64_CONTAINER_USAGE_RE.search(workflow) is not None


def test_maturin_pins_are_synchronized() -> None:
    """Maturin version pins stay aligned across CI and dev dependencies."""
    pins = _read_maturin_pins(repo_root())
    assert len(set(pins.values())) == 1, f"Expected one maturin pin, found {pins!r}"


def test_installed_maturin_matches_expected_pin() -> None:
    """The active maturin CLI matches the pinned development dependency."""
    if shutil.which("maturin") is None:
        pytest.skip("maturin is not installed.")
    expected = _read_expected_maturin_version(repo_root())
    installed = im.version("maturin")
    assert installed == expected, (
        f"Expected maturin {expected}, but {installed} is installed"
    )


def test_maturin_script_locatable_true_when_script_present(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detection succeeds when a ``maturin`` script sits in a scheme's dir."""
    scripts_dir = tmp_path / "bin"
    scripts_dir.mkdir()
    (scripts_dir / "maturin").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        "tests.helpers.maturin.sysconfig.get_scheme_names", lambda: ("posix_prefix",)
    )
    monkeypatch.setattr(
        "tests.helpers.maturin.sysconfig.get_path", lambda *_a, **_k: str(scripts_dir)
    )

    assert maturin_script_locatable(), (
        "a maturin script in a scheme's scripts directory must be discovered"
    )


def test_maturin_script_locatable_false_when_script_absent(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detection reports unavailable when no scheme dir has the script.

    This reproduces the layered ``uv run --with ...`` overlay from issue
    #211: the scripts directory exists (populated with unrelated tools such
    as mutmut itself) but contains no file named ``maturin``, which is
    exactly the condition under which maturin's own ``python -m maturin``
    entry point fails with "Unable to find `maturin` script".
    """
    scripts_dir = tmp_path / "bin"
    scripts_dir.mkdir()
    (scripts_dir / "mutmut").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        "tests.helpers.maturin.sysconfig.get_scheme_names", lambda: ("posix_prefix",)
    )
    monkeypatch.setattr(
        "tests.helpers.maturin.sysconfig.get_path", lambda *_a, **_k: str(scripts_dir)
    )

    assert not maturin_script_locatable(), (
        "no maturin script is present, so discovery must report unavailable"
    )


def test_maturin_script_locatable_matches_windows_exe_launcher(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detection accepts ``maturin.exe``, the real launcher on Windows.

    Matching is stem-based on purpose: maturin's own ``get_maturin_path``
    compares ``os.path.splitext(f)[0]`` against ``"maturin"``, so it accepts
    any extension. On the ``windows-2022`` wheel target in
    ``.github/workflows/build-wheels.yml`` the installed launcher *is*
    ``maturin.exe``. Narrowing this to an exact ``maturin`` filename would
    make the probe report unavailable on Windows and silently skip the
    native-wheel contract there, so this test pins the mirrored behaviour.
    """
    scripts_dir = tmp_path / "Scripts"
    scripts_dir.mkdir()
    (scripts_dir / "maturin.exe").write_bytes(b"MZ")
    monkeypatch.setattr(
        "tests.helpers.maturin.sysconfig.get_scheme_names", lambda: ("nt",)
    )
    monkeypatch.setattr(
        "tests.helpers.maturin.sysconfig.get_path", lambda *_a, **_k: str(scripts_dir)
    )

    assert maturin_script_locatable(), (
        "stem-based matching must accept maturin.exe, the Windows launcher"
    )


def test_manylinux_aarch64_container_is_pinned_to_sha256() -> None:
    """Aarch64 manylinux container pin must be immutable."""
    container_ref = _read_manylinux_aarch64_container_ref(repo_root())
    assert _MANYLINUX_CONTAINER_SHA256_RE.fullmatch(container_ref), (
        f"Expected SHA-256 pinned container ref, found {container_ref!r}"
    )


def test_manylinux_aarch64_container_is_referenced_by_build_step() -> None:
    """The build job should consume the pinned aarch64 container variable."""
    assert _workflow_uses_manylinux_aarch64_container_ref(repo_root()), (
        "Expected build-wheels.yml to reference env.MANYLINUX_AARCH64_CONTAINER"
    )


@pytest.mark.parametrize(
    "container_ref",
    [
        "ghcr.io/rust-cross/manylinux_2_28-cross:aarch64",
        "ghcr.io/rust-cross/manylinux_2_28-cross:latest",
        "ghcr.io/rust-cross/manylinux_2_28-cross@sha256:tooshort",
        "ghcr.io/rust-cross/manylinux_2_28-cross@sha256:" + "g" * 64,
        "",
    ],
)
def test_manylinux_aarch64_container_ref_rejects_mutable_tag(
    container_ref: str,
) -> None:
    """Aarch64 manylinux container refs reject mutable or invalid pins."""
    assert _MANYLINUX_CONTAINER_SHA256_RE.fullmatch(container_ref) is None


def test_manylinux_aarch64_container_pin_regex_rejects_missing_comment() -> None:
    """Aarch64 manylinux container pins require the source-tag comment."""
    yaml_line = (
        "MANYLINUX_AARCH64_CONTAINER: "
        "ghcr.io/rust-cross/manylinux_2_28-cross@sha256:"
        "4864c3e931d790def6dba05cbf133b236b242d0c732f77546c68663c7923116e"
    )

    assert _AARCH64_CONTAINER_PIN_RE.search(yaml_line) is None


def test_manylinux_aarch64_container_usage_regex_rejects_literal_image() -> None:
    """Aarch64 manylinux container usage requires the pinned env variable."""
    yaml_line = (
        "        container: "
        "ghcr.io/rust-cross/manylinux_2_28-cross@sha256:"
        "4864c3e931d790def6dba05cbf133b236b242d0c732f77546c68663c7923116e"
    )

    assert _AARCH64_CONTAINER_USAGE_RE.search(yaml_line) is None


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
        """Record the command and create the expected wheel artifact."""
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


def test_wheel_build_snapshot_rejects_wheel_without_metadata(
    tmp_path: pth.Path,
) -> None:
    """A wheel with WHEEL but no METADATA raises the documented error.

    ``metadata_name`` is derived from the WHEEL entry by string
    substitution rather than looked up in the archive, so without an
    explicit membership check ``ZipFile.read`` would surface a ``KeyError``
    that the helper's documented ``Raises`` contract does not advertise.
    """
    whl_path = tmp_path / "cuprum-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(whl_path, "w") as archive:
        archive.writestr(
            "cuprum-0.0.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: maturin (1.0.0)\nRoot-Is-Purelib: false\n",
        )

    with pytest.raises(AssertionError, match=r"missing \.dist-info/METADATA"):
        wheel_build_snapshot(whl_path)


@pytest.mark.timeout(0)
def test_maturin_wheel_build_snapshot(
    tmp_path: pth.Path,
    snapshot: SnapshotAssertion,
) -> None:
    """Native wheel metadata and layout match the expected maturin output."""
    root = repo_root()
    expected = _read_expected_maturin_version(root)
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
    so a missing ``METADATA`` must not surface as the ``KeyError`` that
    ``ZipFile.read`` would otherwise raise.
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
