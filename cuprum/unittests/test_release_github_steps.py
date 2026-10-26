"""Behaviour of the release workflow's GitHub Release steps.

Each step runs its checked-in script against a ``gh`` stand-in that records
every call, so the assertions are about what the release asks GitHub to do: a
re-run reuses the tag's release instead of creating a second, a new release is
drafted and marked pre-release from the version check, the draft's existing
assets are snapshotted for the PyPI job, and the release is made visible last.
The reconciling upload and digest check are covered by
``test_release_reconciliation.py``.
"""

from __future__ import annotations

import json
import os
import sys
import typing as typ

import pytest

from tests.helpers.release_workflow import (
    AttestationOverrides,
    FakeRelease,
    fake_release,
    install_tool,
    link_scripts,
    outputs,
    run_bash,
    step_script,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

_TAG = "v1.2.3"
_CARRIED_ASSET = "c-1.tar.gz"

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


def test_the_snapshot_carries_the_release_bytes_for_this_runs_names(
    tmp_path: pth.Path,
) -> None:
    """Only assets sharing a name with this run's artefacts are downloaded."""
    fake = fake_release(tmp_path)
    link_scripts(tmp_path)
    publish = tmp_path / "dist" / "publish"
    publish.mkdir(parents=True)
    for name in ("c-1-py3-none-any.whl", "c-1.tar.gz"):
        (publish / name).write_bytes(b"rebuilt")
    for name in (_CARRIED_ASSET, "c-0.9.tar.gz", "cuprum-v1.2.3-run1-1.sigstore.json"):
        (fake.github / name).write_bytes(b"published")
    fake.add_attestation(fake.github / _CARRIED_ASSET)

    completed = run_bash(
        step_script("draft-release", "Snapshot the release assets"),
        tmp_path,
        fake.env(GITHUB_REF_NAME=_TAG, GITHUB_REF=f"refs/tags/{_TAG}"),
    )

    assert completed.returncode == 0, completed.stderr
    verified = run_bash(
        step_script("draft-release", "Verify carried asset provenance"),
        tmp_path,
        fake.env(GITHUB_REF_NAME=_TAG, GITHUB_REF=f"refs/tags/{_TAG}"),
    )
    assert verified.returncode == 0, verified.stderr
    state = tmp_path / "github-state"
    carried = {path.name: path.read_bytes() for path in (state / "files").iterdir()}
    assert carried == {"c-1.tar.gz": b"published"}, (
        "the PyPI job needs GitHub's bytes for exactly the names both share"
    )
    listed = json.loads((state / "assets.json").read_text(encoding="utf-8"))
    assert len(listed["assets"]) == 3, "the snapshot must list every asset"
    assert [call[:2] for call in fake.calls("gh")] == [
        ["release", "view"],
        ["release", "download"],
        ["--version"],
        ["attestation", "verify"],
    ], "the step snapshots assets, then records the verification attempt"
    verify_call = fake.calls("gh")[-1]
    assert verify_call == [
        "attestation",
        "verify",
        f"github-state/files/{_CARRIED_ASSET}",
        "--repo",
        "leynos/cuprum",
        "--signer-workflow",
        "leynos/cuprum/.github/workflows/release.yml",
        "--source-ref",
        f"refs/tags/{_TAG}",
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ], "the verifier must bind this asset to the release identity policy"
    record = json.loads(
        (state / "provenance" / f"{_CARRIED_ASSET}.json").read_text("utf-8")
    )
    subject = record[0]["verificationResult"]["statement"]["subject"][0]
    assert subject["name"] == _CARRIED_ASSET, (
        "the saved attestation must name the carried release asset"
    )


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


def _carried_asset(fake: FakeRelease) -> tuple[pth.Path, dict[str, str]]:
    """Seed one local build and a differently built GitHub Release asset."""
    link_scripts(fake.root)
    publish = fake.root / "dist" / "publish"
    publish.mkdir(parents=True)
    (publish / _CARRIED_ASSET).write_bytes(b"fresh attested build")
    asset = fake.github / _CARRIED_ASSET
    asset.write_bytes(b"prior release asset")
    environment = fake.env(
        GITHUB_REF_NAME=_TAG,
        GITHUB_REF=f"refs/tags/{_TAG}",
    )
    snapshot = run_bash(
        step_script("draft-release", "Snapshot the release assets"),
        fake.root,
        environment,
    )
    assert snapshot.returncode == 0, snapshot.stderr
    return asset, environment


@pytest.mark.parametrize(
    ("overrides", "extra_env"),
    [
        (None, {}),
        (AttestationOverrides(repository="attacker/other"), {}),
        (
            AttestationOverrides(
                signer_workflow="leynos/cuprum/.github/workflows/other.yml"
            ),
            {},
        ),
        (AttestationOverrides(source_ref="refs/tags/v9.9.9"), {}),
        (AttestationOverrides(self_hosted=True), {}),
        (AttestationOverrides(sha256="0" * 64), {}),
        (
            AttestationOverrides(),
            {"FAKE_ATTEST_FAIL": "controlled verifier failure"},
        ),
    ],
    ids=[
        "missing-attestation",
        "wrong-repository",
        "wrong-workflow",
        "wrong-tag",
        "self-hosted-runner",
        "another-digest",
        "verifier-failure",
    ],
)
def test_failed_verification_warns_and_saves_no_record(
    tmp_path: pth.Path,
    overrides: AttestationOverrides | None,
    extra_env: dict[str, str],
) -> None:
    """The snapshot continues while unverified assets stay marked untrusted."""
    fake = fake_release(tmp_path)
    asset, environment = _carried_asset(fake)
    if overrides is not None:
        fake.add_attestation(asset, overrides)
    completed = run_bash(
        step_script("draft-release", "Verify carried asset provenance"),
        tmp_path,
        {**environment, **extra_env},
    )

    record = tmp_path / "github-state" / "provenance" / f"{_CARRIED_ASSET}.json"
    verify_calls = [
        call for call in fake.calls("gh") if call[:2] == ["attestation", "verify"]
    ]
    assert completed.returncode == 0, completed.stderr
    assert f"GitHub asset {_CARRIED_ASSET}" in completed.stdout, (
        "verification failure must warn about the specific carried asset"
    )
    assert not record.exists(), "failed verification must remove partial JSON output"
    assert len(verify_calls) == 3, (
        "verification failures must stop after three attempts"
    )
    assert outputs(tmp_path / "github-output")["verification_failures"] == "1", (
        "a verifier warning must be reported to phase telemetry"
    )


def test_transient_verification_failure_retries_before_saving_provenance(
    tmp_path: pth.Path,
) -> None:
    """A temporary verifier failure gets another bounded verification attempt."""
    fake = fake_release(tmp_path)
    asset, environment = _carried_asset(fake)
    fake.add_attestation(asset)

    completed = run_bash(
        step_script("draft-release", "Verify carried asset provenance"),
        tmp_path,
        {**environment, "FAKE_ATTEST_FAIL_COUNT": "1"},
    )

    verify_calls = [
        call for call in fake.calls("gh") if call[:2] == ["attestation", "verify"]
    ]
    record = tmp_path / "github-state" / "provenance" / f"{_CARRIED_ASSET}.json"
    assert completed.returncode == 0, completed.stderr
    assert len(verify_calls) == 2, "a first transient failure must be retried once"
    assert verify_calls[0] == verify_calls[1], "each retry must preserve the policy"
    assert record.is_file(), "successful retry must save its approved verifier result"
    assert outputs(tmp_path / "github-output")["verification_failures"] == "0", (
        "a successful retry must not be reported as a remaining failure"
    )
    assert "Could not verify provenance" not in completed.stdout, (
        "a later successful attempt must not leave a failure warning"
    )


def test_the_verifier_requires_github_cli_2_102_0(
    tmp_path: pth.Path,
) -> None:
    """An older CLI cannot silently skip the repository-store policy flags."""
    fake = fake_release(tmp_path)
    asset, environment = _carried_asset(fake)
    fake.add_attestation(asset)
    completed = run_bash(
        step_script("draft-release", "Verify carried asset provenance"),
        tmp_path,
        {**environment, "FAKE_GH_VERSION": "2.101.9"},
    )

    assert completed.returncode != 0, "unsupported CLI versions must stop the step"
    assert "GitHub CLI 2.102.0 or newer is required" in completed.stderr, (
        "the step must explain which CLI version it requires"
    )
    record = tmp_path / "github-state" / "provenance" / f"{_CARRIED_ASSET}.json"
    assert not record.exists(), "an older CLI must not save provenance output"
    assert not any(
        call[:2] == ["attestation", "verify"] for call in fake.calls("gh")
    ), "an older CLI must fail before invoking the verifier"


def test_the_fake_gh_rejects_operations_the_workflow_does_not_use(
    tmp_path: pth.Path,
) -> None:
    """An unsupported fake operation cannot look like a successful API call."""
    fake = fake_release(tmp_path)

    completed = run_bash("gh api user", tmp_path, fake.env())

    assert completed.returncode == 2, "unsupported fake gh calls must fail explicitly"
    assert "unsupported fake gh operation: api user" in completed.stderr, (
        "the fake gh error must identify the unsupported operation"
    )
