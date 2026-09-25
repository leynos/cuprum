"""Behaviour of the release workflow's artefact collection and publish steps.

The steps run the checked-in workflow scripts against a scratch directory, so
the contract is what the release actually executes: exactly one sdist travels
with the wheels, files already on PyPI are skipped by name, and a run with
nothing left to upload succeeds without calling ``uv publish``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess  # ruff: ignore[suspicious-subprocess-import] - executes checked-in workflow code.
import sys
import typing as typ

import pytest
import yaml

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import pathlib as pth

_WORKFLOW_PATH = ".github/workflows/release.yml"
_SKIP_STEP = "Skip artefacts already on PyPI"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The release workflow's steps run under Bash on Linux.",
)


def _step_script(name: str) -> str:
    """Return the ``run`` script of one publish-job step."""
    workflow = yaml.safe_load((repo_root() / _WORKFLOW_PATH).read_text("utf-8"))
    steps = workflow["jobs"]["publish"]["steps"]
    script = next((step.get("run") for step in steps if step.get("name") == name), None)
    assert isinstance(script, str), f"release publish job must have step {name!r}"
    return script


def _run_bash(
    script: str, cwd: pth.Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``script`` under Bash in ``cwd``."""
    bash = shutil.which("bash", path=os.defpath)
    assert bash is not None, "the release workflow test requires Bash"
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - checked-in workflow code.
        [bash, "-c", script],
        capture_output=True,
        check=False,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        text=True,
    )


def _touch(directory: pth.Path, *names: str) -> None:
    """Create empty artefact files named ``names`` under ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"")


def _skip_filter_script() -> str:
    """Return the Python filter embedded in the skip step."""
    script = _step_script(_SKIP_STEP)
    return script.split("python - <<'PY'\n", maxsplit=1)[1].rsplit("PY", 1)[0]


def test_collection_gathers_the_sdist_with_every_wheel(tmp_path: pth.Path) -> None:
    """Wheels from every artefact directory and the one sdist are collected."""
    _touch(tmp_path / "dist" / "wheels-pure", "c-1-py3-none-any.whl", "c-1.tar.gz")
    _touch(tmp_path / "dist" / "wheels-native-linux", "c-1-cp312-abi3-linux.whl")

    completed = _run_bash(_step_script("Collect release artefacts"), tmp_path)

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

    completed = _run_bash(_step_script("Collect release artefacts"), tmp_path)

    assert completed.returncode != 0, "a missing sdist must fail the release"
    assert "Expected exactly one source distribution." in completed.stderr


def test_artefacts_already_on_pypi_are_skipped_by_name(tmp_path: pth.Path) -> None:
    """Only files whose names are absent from the index remain to upload."""
    publish = tmp_path / "dist" / "publish"
    _touch(publish, "c-1-py3-none-any.whl", "c-1.tar.gz", "c-1-cp312-abi3-linux.whl")
    index = {
        "files": [{"filename": "c-1-py3-none-any.whl"}, {"filename": "c-1.tar.gz"}]
    }
    (tmp_path / "pypi-index.json").write_text(json.dumps(index), encoding="utf-8")

    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - checked-in workflow code.
        [sys.executable, "-c", _skip_filter_script()],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert sorted(path.name for path in publish.iterdir()) == [
        "c-1-cp312-abi3-linux.whl"
    ], "published names must be dropped and new ones kept"
    assert "::notice::Skipping c-1.tar.gz: already on PyPI" in completed.stdout


def _fake_uv(tmp_path: pth.Path) -> pth.Path:
    """Install a ``uv`` stand-in that records its arguments."""
    tools = tmp_path / "tools"
    tools.mkdir()
    fake = tools / "uv"
    fake.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "${UV_ARGUMENTS}"\n', encoding="utf-8"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return tools


@pytest.mark.parametrize(
    ("remaining", "expect_upload"),
    [(("c-1-cp312-abi3-linux.whl",), True), ((), False)],
    ids=["uploads-remaining", "nothing-left"],
)
def test_publish_uploads_only_what_remains(
    tmp_path: pth.Path, remaining: tuple[str, ...], *, expect_upload: bool
) -> None:
    """Remaining files are uploaded with ``--check-url``; none means success."""
    _touch(tmp_path / "dist" / "publish", *remaining)
    tools = _fake_uv(tmp_path)
    arguments = tmp_path / "uv-arguments"
    env = {
        "PATH": os.pathsep.join((str(tools), os.defpath)),
        "UV_ARGUMENTS": str(arguments),
    }

    completed = _run_bash(_step_script("Publish to PyPI with uv"), tmp_path, env)

    assert completed.returncode == 0, completed.stderr
    assert arguments.exists() is expect_upload, "uv publish ran unexpectedly"
    if expect_upload:
        assert arguments.read_text(encoding="utf-8").splitlines() == [
            "publish",
            "--check-url",
            "https://pypi.org/simple/",
            f"dist/publish/{remaining[0]}",
        ], "upload must pass --check-url and every remaining artefact"
