"""Exercise the wheel workflow's maturin ``Generator`` verification step.

Maturin records the release that built a wheel in the WHEEL metadata's
``Generator`` header. The ``verify-wheel-install`` job asserts that header
against the workflow's ``MATURIN_VERSION`` before installing anything, so a
wheel built by a different maturin fails there instead of being published as
if the pin had built it.

Reading the YAML cannot show that. These tests build real wheel archives,
place them where the step's glob looks, and run the checked-in shell body, so
each case covers the whole process boundary: the glob, the header parse, and
the exit status of the defect.
"""

from __future__ import annotations

import typing as typ
import zipfile

from tests.helpers.workflow_steps import find_step, run_bash, step_script

if typ.TYPE_CHECKING:
    import subprocess
    from pathlib import Path

WORKFLOW = "build-wheels.yml"
JOB = "verify-wheel-install"
STEP = "Verify the native wheels record the pinned maturin"

PIN = "1.15.0"

#: A wheel-shaped name carrying the stable-ABI tag the step's glob selects.
_WHEEL_TEMPLATE = "cuprum-0.0.0-cp312-abi3-manylinux_2_28_{platform}.whl"

_METADATA_TEMPLATE = (
    "Wheel-Version: 1.0\n"
    "{generator}"
    "Root-Is-Purelib: false\n"
    "Tag: cp312-abi3-manylinux_2_28_{platform}\n"
)


def _metadata(*, platform: str, generator: str | None) -> str:
    """Render WHEEL metadata, optionally leaving out the Generator header."""
    header = "" if generator is None else f"Generator: {generator}\n"
    return _METADATA_TEMPLATE.format(platform=platform, generator=header)


def _write_wheel(
    library: Path,
    *,
    platform: str,
    generator: str | None = f"maturin ({PIN})",
) -> Path:
    """Write a minimal wheel whose WHEEL metadata names ``generator``."""
    wheel = library / _WHEEL_TEMPLATE.format(platform=platform)
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "cuprum-0.0.0.dist-info/WHEEL",
            _metadata(platform=platform, generator=generator),
        )
        archive.writestr("cuprum/__init__.py", "")
    return wheel


def _run_step(root: Path) -> subprocess.CompletedProcess[str]:
    """Run the verification step over whatever wheels ``root`` now holds."""
    environment = {
        "MATURIN_VERSION": PIN,
        "NATIVE_WHEEL_DIR_PATTERN": str(root / "dist" / "wheels-native-*"),
    }
    return run_bash(
        step_script(workflow=WORKFLOW, job=JOB, name=STEP),
        cwd=root,
        env=environment,
    )


def _library(root: Path) -> Path:
    """Create and return the native-wheel directory the step's glob expects."""
    library = root / "dist" / "wheels-native-x86_64"
    library.mkdir(parents=True)
    return library


def test_a_matching_generator_is_accepted(tmp_path: Path) -> None:
    """A wheel whose header names the pinned release passes and is counted."""
    wheel = _write_wheel(_library(tmp_path), platform="x86_64")
    assert wheel.exists(), "the fixture wheel must have been written"

    result = _run_step(tmp_path)

    assert result.returncode == 0, (
        f"a wheel recording maturin {PIN} must be accepted: {result.stderr}"
    )
    assert f"1 native wheel(s) record maturin {PIN}" in result.stdout, (
        f"the step must report the count it verified, got {result.stdout!r}"
    )


def test_an_empty_wheel_tree_fails(tmp_path: Path) -> None:
    """Nothing to verify is a failure, not a vacuous pass."""
    _library(tmp_path)

    result = _run_step(tmp_path)

    assert result.returncode != 0, (
        "a run that matched no native wheel must fail rather than certify nothing"
    )
    assert "no abi3 native wheel matched" in result.stderr, (
        f"the failure must say the glob matched nothing, got {result.stderr!r}"
    )


def test_a_foreign_generator_fails_and_names_the_wheel(
    tmp_path: Path,
) -> None:
    """A wheel built by another maturin fails, naming both versions."""
    wheel = _write_wheel(
        _library(tmp_path), platform="x86_64", generator="maturin (1.14.0)"
    )

    result = _run_step(tmp_path)

    assert result.returncode != 0, "a foreign generator must fail the check"
    assert wheel.name in result.stderr, (
        f"the failure must name the offending wheel, got {result.stderr!r}"
    )
    assert "1.14.0" in result.stderr, (
        f"the failure must name the version the wheel records, got {result.stderr!r}"
    )
    assert PIN in result.stderr, (
        f"the failure must name the expected version, got {result.stderr!r}"
    )


def test_a_wheel_without_a_generator_header_fails(tmp_path: Path) -> None:
    """WHEEL metadata with no Generator header cannot certify the pin."""
    wheel = _write_wheel(_library(tmp_path), platform="x86_64", generator=None)

    result = _run_step(tmp_path)

    assert result.returncode != 0, "a missing Generator header must fail the check"
    assert "no Generator header" in result.stderr, (
        f"the failure must say the header is absent, got {result.stderr!r}"
    )
    assert wheel.name in result.stderr, (
        f"the failure must name the offending wheel, got {result.stderr!r}"
    )


def test_every_native_wheel_is_checked(tmp_path: Path) -> None:
    """One bad wheel among several fails the whole step."""
    library = _library(tmp_path)
    _write_wheel(library, platform="x86_64")
    bad = _write_wheel(library, platform="aarch64", generator="maturin (1.14.0)")

    result = _run_step(tmp_path)

    assert result.returncode != 0, "a single bad wheel must fail the whole check"
    assert bad.name in result.stderr, (
        f"the failure must name the bad wheel, got {result.stderr!r}"
    )


def test_the_step_reads_the_workflow_pin() -> None:
    """The step takes its expected version from the workflow environment."""
    step = find_step(workflow=WORKFLOW, job=JOB, name=STEP)
    environment = step.get("env")

    assert isinstance(environment, dict), "the verification step must declare env"
    assert environment.get("MATURIN_VERSION") == "${{ env.MATURIN_VERSION }}", (
        "the step must read the workflow-level MATURIN_VERSION pin rather than "
        f"declare its own, got {environment.get('MATURIN_VERSION')!r}"
    )
