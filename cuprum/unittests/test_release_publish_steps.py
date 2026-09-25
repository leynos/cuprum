"""Behaviour of the release workflow's version check, collection, and skip steps.

The steps run the checked-in workflow scripts against a scratch directory, so
the contract is what the release actually executes: the tag must equal the
declared version, exactly one sdist travels with the wheels, and files already
on PyPI are skipped by name, with nothing left meaning no upload at all.
"""

from __future__ import annotations

import json
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - executes checked-in workflow code.
import sys
import tomllib
import typing as typ

import pytest

from tests.helpers.docs import repo_root
from tests.helpers.release_workflow import (
    outputs,
    python_heredoc,
    run_bash,
    step_script,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

_VERSION_STEP = ("check-version", "Check the pyproject.toml version against the tag")
_COLLECT_STEP = ("attest", "Collect release artefacts")
_SKIP_STEP = ("publish-pypi", "Skip artefacts already on PyPI")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The release workflow's steps run under Bash on Linux.",
)


def _touch(directory: pth.Path, *names: str) -> None:
    """Create empty artefact files named ``names`` under ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"")


def _run_python(
    program: str, cwd: pth.Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run an embedded workflow Python program in ``cwd``."""
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - checked-in workflow code.
        [sys.executable, "-c", program],
        capture_output=True,
        check=False,
        cwd=cwd,
        env={**os.environ, **env},
        text=True,
    )


def _check_version(
    tmp_path: pth.Path, declared: str, tag_version: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Run the pyproject.toml check against one declared and tag version."""
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "cuprum"\nversion = "{declared}"\n', encoding="utf-8"
    )
    github_output = tmp_path / "github-output"
    completed = _run_python(
        python_heredoc(step_script(*_VERSION_STEP)),
        tmp_path,
        {"TAG_VERSION": tag_version, "GITHUB_OUTPUT": str(github_output)},
    )
    return completed, outputs(github_output)


@pytest.mark.parametrize(
    ("version", "expected_prerelease"),
    [
        ("0.2.0-beta1", "true"),
        ("0.2.0b1", "true"),
        ("1.0.0-rc.2", "true"),
        ("1.0.0a3", "true"),
        ("1.2.3", "false"),
        ("10.20.30", "false"),
    ],
)
def test_a_matching_tag_passes_and_reports_its_prerelease_status(
    tmp_path: pth.Path, version: str, expected_prerelease: str
) -> None:
    """The tag-derived version must equal the declared one, in either spelling."""
    completed, written = _check_version(tmp_path, version, version)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert written == {"prerelease": expected_prerelease}, (
        f"{version} must be reported as prerelease={expected_prerelease}"
    )


@pytest.mark.parametrize(
    ("declared", "tag_version"),
    [("1.2.2", "1.2.3"), ("0.2.0-beta1", "0.2.0b1"), ("0.2.0-beta1", "0.2.0")],
    ids=["older-manifest", "respelled-prerelease", "dropped-prerelease"],
)
def test_a_mismatched_tag_fails_before_anything_is_published(
    tmp_path: pth.Path, declared: str, tag_version: str
) -> None:
    """A tag naming another version stops the release with an annotation."""
    completed, written = _check_version(tmp_path, declared, tag_version)

    assert completed.returncode != 0, "a mismatched tag must fail the release"
    assert "::error file=pyproject.toml,title=Tag/pyproject.toml mismatch::" in (
        completed.stdout
    )
    assert written == {}, "a failed check must not report a prerelease status"


def test_the_manifests_the_tag_is_checked_against_agree() -> None:
    """Both checks compare the same tag, so the two manifests must agree."""
    root = repo_root()
    pyproject = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
    cargo = tomllib.loads((root / "rust/cuprum-rust/Cargo.toml").read_text("utf-8"))

    assert pyproject["project"]["version"] == cargo["package"]["version"], (
        "no tag can pass both version checks while the manifests disagree"
    )


def test_collection_gathers_the_sdist_with_every_wheel(tmp_path: pth.Path) -> None:
    """Wheels from every artefact directory and the one sdist are collected."""
    _touch(tmp_path / "dist" / "wheels-pure", "c-1-py3-none-any.whl", "c-1.tar.gz")
    _touch(tmp_path / "dist" / "wheels-native-linux", "c-1-cp312-abi3-linux.whl")

    completed = run_bash(step_script(*_COLLECT_STEP), tmp_path)

    assert completed.returncode == 0, completed.stderr
    collected = sorted(path.name for path in (tmp_path / "dist" / "publish").iterdir())
    assert collected == [
        "c-1-cp312-abi3-linux.whl",
        "c-1-py3-none-any.whl",
        "c-1.tar.gz",
    ], "every wheel and the sdist must be staged for upload"


def test_collection_fails_without_a_source_distribution(tmp_path: pth.Path) -> None:
    """A release that lost its sdist fails before anything is uploaded."""
    _touch(tmp_path / "dist" / "wheels-pure", "c-1-py3-none-any.whl")

    completed = run_bash(step_script(*_COLLECT_STEP), tmp_path)

    assert completed.returncode != 0, "a missing sdist must fail the release"
    assert "Expected exactly one source distribution." in completed.stderr


@pytest.mark.parametrize(
    ("published", "remaining", "has_remaining"),
    [
        (
            ("c-1-py3-none-any.whl", "c-1.tar.gz"),
            ["c-1-cp312-abi3-linux.whl"],
            "true",
        ),
        (
            ("c-1-py3-none-any.whl", "c-1.tar.gz", "c-1-cp312-abi3-linux.whl"),
            [],
            "false",
        ),
    ],
    ids=["uploads-remaining", "nothing-left"],
)
def test_artefacts_already_on_pypi_are_skipped_by_name(
    tmp_path: pth.Path,
    published: tuple[str, ...],
    remaining: list[str],
    has_remaining: str,
) -> None:
    """Only unpublished names remain, and the step says whether any do."""
    publish = tmp_path / "dist" / "publish"
    _touch(publish, "c-1-py3-none-any.whl", "c-1.tar.gz", "c-1-cp312-abi3-linux.whl")
    index = {"files": [{"filename": name} for name in published]}
    (tmp_path / "pypi-index.json").write_text(json.dumps(index), encoding="utf-8")
    github_output = tmp_path / "github-output"

    completed = _run_python(
        python_heredoc(step_script(*_SKIP_STEP)),
        tmp_path,
        {"GITHUB_OUTPUT": str(github_output)},
    )

    assert completed.returncode == 0, completed.stderr
    assert sorted(path.name for path in publish.iterdir()) == remaining, (
        "published names must be dropped and new ones kept"
    )
    assert "::notice::Skipping c-1.tar.gz: already on PyPI" in completed.stdout
    assert outputs(github_output) == {"remaining": has_remaining}, (
        "the upload is guarded on this output, so it must match what is left"
    )
