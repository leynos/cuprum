"""Behaviour of the release workflow's GitHub Release steps.

Each step runs its checked-in script against a ``gh`` stand-in that records
every call, so the assertions are about what the release asks GitHub to do: a
re-run reuses the tag's release instead of creating a second, a new release is
drafted and marked pre-release from the version check, assets are uploaded
with ``--clobber``, and the release is made visible last.
"""

from __future__ import annotations

import os
import sys
import typing as typ

import pytest

from tests.helpers.release_workflow import install_tool, run_bash, step_script

if typ.TYPE_CHECKING:
    import pathlib as pth

_TAG = "v1.2.3"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The release workflow's steps run under Bash on Linux.",
)


def _run_with_fake_gh(
    tmp_path: pth.Path,
    script: str,
    *,
    view_status: int = 0,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, list[list[str]]]:
    """Run ``script`` with a recording ``gh``; return status, stderr, and calls."""
    calls = tmp_path / "gh-calls"
    install_tool(
        tmp_path / "tools",
        "gh",
        'printf \'%s\\n\' "$*" >> "${GH_CALLS}"\n'
        'if [ "$1 $2" = "release view" ]; then exit "${GH_VIEW_STATUS}"; fi',
    )
    env = {
        "PATH": os.pathsep.join((str(tmp_path / "tools"), os.defpath)),
        "GH_CALLS": str(calls),
        "GH_VIEW_STATUS": str(view_status),
        "GITHUB_REF_NAME": _TAG,
        **(extra_env or {}),
    }
    completed = run_bash(script, tmp_path, env)
    recorded = (
        [line.split() for line in calls.read_text(encoding="utf-8").splitlines()]
        if calls.exists()
        else []
    )
    return completed.returncode, completed.stderr, recorded


def test_an_existing_release_is_reused(tmp_path: pth.Path) -> None:
    """A re-run finds the tag's release and creates nothing."""
    status, stderr, calls = _run_with_fake_gh(
        tmp_path,
        step_script("draft-release", "Create or reuse the draft release"),
        extra_env={"IS_PRERELEASE": "false"},
    )

    assert status == 0, stderr
    assert calls == [["release", "view", _TAG]], "an existing release must be reused"


@pytest.mark.parametrize(
    ("is_prerelease", "expect_prerelease_flag"),
    [("true", True), ("false", False)],
    ids=["prerelease", "final"],
)
def test_a_missing_release_is_created_as_a_draft(
    tmp_path: pth.Path, is_prerelease: str, *, expect_prerelease_flag: bool
) -> None:
    """A first run drafts the release and carries the pre-release status."""
    status, stderr, calls = _run_with_fake_gh(
        tmp_path,
        step_script("draft-release", "Create or reuse the draft release"),
        view_status=1,
        extra_env={"IS_PRERELEASE": is_prerelease},
    )

    assert status == 0, stderr
    assert len(calls) == 2, f"expected a lookup then a create, got {calls}"
    create = calls[1]
    assert create[:3] == ["release", "create", _TAG], create
    assert {"--draft", "--verify-tag", "--generate-notes"} <= set(create), (
        "the release must start as a draft of the pushed tag"
    )
    assert ("--prerelease" in create) is expect_prerelease_flag, (
        "the pre-release flag must follow the version check"
    )


def test_every_artefact_and_the_bundle_are_uploaded_with_clobber(
    tmp_path: pth.Path,
) -> None:
    """A re-run replaces the assets a previous attempt uploaded."""
    for relative in (
        "dist/publish/c-1-py3-none-any.whl",
        "dist/publish/c-1.tar.gz",
        "dist/attestations/cuprum-v1.2.3.sigstore.json",
    ):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_bytes(b"")

    status, stderr, calls = _run_with_fake_gh(
        tmp_path, step_script("draft-release", "Upload release assets")
    )

    assert status == 0, stderr
    assert calls == [
        [
            "release",
            "upload",
            _TAG,
            "--clobber",
            "dist/publish/c-1-py3-none-any.whl",
            "dist/publish/c-1.tar.gz",
            "dist/attestations/cuprum-v1.2.3.sigstore.json",
        ]
    ], "every wheel, the sdist, and the provenance bundle must be attached"


def test_an_empty_upload_fails(tmp_path: pth.Path) -> None:
    """A release with nothing to attach fails rather than publishing bare."""
    status, stderr, calls = _run_with_fake_gh(
        tmp_path, step_script("draft-release", "Upload release assets")
    )

    assert status != 0, "an upload with no assets must fail"
    assert "No release assets to upload." in stderr
    assert calls == [], "nothing may be sent to GitHub"


@pytest.mark.parametrize("is_prerelease", ["true", "false"])
def test_the_release_is_published_with_its_prerelease_status(
    tmp_path: pth.Path, is_prerelease: str
) -> None:
    """The draft is made visible, keeping the version check's status."""
    status, stderr, calls = _run_with_fake_gh(
        tmp_path,
        step_script("publish-release", "Publish the GitHub Release"),
        extra_env={"IS_PRERELEASE": is_prerelease},
    )

    assert status == 0, stderr
    assert calls == [
        ["release", "edit", _TAG, "--draft=false", f"--prerelease={is_prerelease}"]
    ]
