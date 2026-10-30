"""End-to-end behaviour of the release's asset reconciliation across its jobs.

Each test runs the checked-in steps of ``draft-release``, ``publish-pypi``, and
``publish-release`` in order, against ``gh`` and ``curl`` stand-ins whose
directories hold the GitHub Release's assets and PyPI's files. The one step
that cannot run here, pypa's upload action, is modelled as copying the staged
files to PyPI without overwriting, which is what ``skip-existing`` promises.
The assertions are about bytes: every name ends up identical on both sides,
taken from PyPI first, then GitHub, then this run, and nothing is replaced.
The provenance gate runs between the index-digest check and the upload, so
these tests also pin which PyPI bytes the release may receive: only those an
approved attestation binds to the exact filename, digest, tag, and commit.
"""

from __future__ import annotations

import json
import shutil
import sys
import typing as typ

import pytest

from tests.helpers.release_asset_provenance import provenance_record, sha256_hex
from tests.helpers.release_workflow import (
    AttestationOverrides,
    FakeRelease,
    fake_release,
    link_scripts,
    outputs,
    run_bash,
    step_script,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

_TAG = "v1.2.3"
_BUNDLE = "cuprum-v1.2.3-run7-1.sigstore.json"
#: One name per reconciliation case: PyPI only, GitHub only, neither, both.
_PYPI_ONLY, _GITHUB_ONLY, _NEITHER, _BOTH = (
    "c-1-cp312-abi3-linux.whl",
    "c-1-cp312-abi3-macos.whl",
    "c-1-py3-none-any.whl",
    "c-1.tar.gz",
)
_ARTEFACTS = (_PYPI_ONLY, _GITHUB_ONLY, _NEITHER, _BOTH)
#: The provenance gate sits after the index-digest check and before the
#: upload, so nothing unverified can reach the release.
_PROVENANCE_STEP = "Verify provenance of PyPI bytes"
_UPLOAD_STEP = "Upload the assets the release lacks"
_PRE_PROVENANCE = (
    "Fetch the PyPI index",
    "List the release assets",
    "Stage the assets the release lacks",
    "Check the fetched PyPI bytes",
)
_PUBLISH_STEPS = (*_PRE_PROVENANCE, _PROVENANCE_STEP)
_PREDICATE_TYPE = "https://slsa.dev/provenance/v1"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The release workflow's steps run under Bash on Linux.",
)


def _local(name: str) -> bytes:
    """Return the bytes this run's build produced for ``name``."""
    return f"rebuilt {name}".encode()


def _job_directory(fake: FakeRelease, job: str) -> tuple[pth.Path, dict[str, str]]:
    """Stage one job's workspace with this run's artefacts and bundle."""
    cwd = fake.root / job
    for name in _ARTEFACTS:
        (cwd / "dist" / "publish").mkdir(parents=True, exist_ok=True)
        (cwd / "dist" / "publish" / name).write_bytes(_local(name))
    (cwd / "dist" / "attestations").mkdir(parents=True)
    (cwd / "dist" / "attestations" / _BUNDLE).write_bytes(b"bundle")
    link_scripts(cwd)
    return cwd, fake.env(GITHUB_REF_NAME=_TAG, GITHUB_OUTPUT=str(cwd / "output"))


def _step(job: str, name: str, cwd: pth.Path, env: dict[str, str]) -> None:
    """Run one step and require it to succeed."""
    completed = run_bash(step_script(job, name), cwd, env)
    assert completed.returncode == 0, f"{job}/{name}: {completed.stderr}"


def _draft_and_publish_pypi(fake: FakeRelease) -> None:
    """Run the snapshot, the PyPI staging, and the modelled PyPI upload."""
    draft, env = _job_directory(fake, "draft-release")
    _step("draft-release", "Snapshot the release assets", draft, env)
    _step("draft-release", "Verify carried asset provenance", draft, env)
    pypi, env = _job_directory(fake, "publish-pypi")
    shutil.copytree(draft / "github-state", pypi / "github-state")
    _step("publish-pypi", "Fetch the PyPI index", pypi, env)
    _step("publish-pypi", "Skip artefacts already on PyPI", pypi, env)
    if outputs(pypi / "output")["remaining"] == "true":
        for staged in (pypi / "dist" / "publish").iterdir():
            assert not (fake.pypi / staged.name).exists(), "PyPI never overwrites"
            shutil.copyfile(staged, fake.pypi / staged.name)
            # `attest` attested these bytes before the PyPI upload, so the
            # release job can verify the copy PyPI now serves back.
            fake.attest_approved(fake.pypi / staged.name)


def _publish_release(fake: FakeRelease, *, verify: bool = True) -> pth.Path:
    """Run ``publish-release`` up to its digest check; return its workspace."""
    cwd, env = _job_directory(fake, "publish-release")
    for name in _PUBLISH_STEPS:
        _step("publish-release", name, cwd, env)
    if outputs(cwd / "output")["pending"] == "true":
        _step("publish-release", _UPLOAD_STEP, cwd, env)
    if verify:
        _step(
            "publish-release", "Check both destinations hold the same bytes", cwd, env
        )
    return cwd


def _holdings(directory: pth.Path) -> dict[str, bytes]:
    """Return each file's bytes in one destination."""
    return {path.name: path.read_bytes() for path in directory.iterdir()}


def _seeded(root: pth.Path) -> FakeRelease:
    """Return destinations holding one name per reconciliation case."""
    release = fake_release(root)
    (release.pypi / _PYPI_ONLY).write_bytes(b"pypi")
    (release.github / _GITHUB_ONLY).write_bytes(b"github")
    release.attest_approved(release.github / _GITHUB_ONLY)
    (release.pypi / _BOTH).write_bytes(b"both")
    (release.github / _BOTH).write_bytes(b"both")
    return release


@pytest.fixture
def fake(tmp_path: pth.Path) -> FakeRelease:
    """Return the seeded destinations with PyPI's bytes attested.

    PyPI's bytes are attested as this tag's build, which is what an earlier
    run of the same tag stored. Without that record the release could not
    upload them, so every test that expects a successful publish relies on it.

    Returns
    -------
    FakeRelease
        The fake destinations, ready for a successful publish.
    """
    release = _seeded(tmp_path)
    release.attest_approved(release.pypi / _PYPI_ONLY)
    return release


def test_every_name_converges_on_its_canonical_bytes(fake: FakeRelease) -> None:
    """PyPI's bytes win, then GitHub's, then this run's, on both sides."""
    _draft_and_publish_pypi(fake)
    _publish_release(fake)

    expected = {
        _PYPI_ONLY: b"pypi",
        _GITHUB_ONLY: b"github",
        _NEITHER: _local(_NEITHER),
        _BOTH: b"both",
    }
    assert _holdings(fake.pypi) == expected
    assert _holdings(fake.github) == {**expected, _BUNDLE: b"bundle"}


def test_verified_prior_run_asset_is_published_to_pypi(fake: FakeRelease) -> None:
    """A tag-bound prior attestation permits interrupted-run carryover."""
    _draft_and_publish_pypi(fake)

    assert (fake.pypi / _GITHUB_ONLY).read_bytes() == b"github", (
        "PyPI must receive the bytes from the approved prior release"
    )
    assert any(call[:2] == ["attestation", "verify"] for call in fake.calls("gh")), (
        "the draft job must verify the carried asset before PyPI staging"
    )


def test_unverified_differing_asset_stops_before_pypi_upload(
    tmp_path: pth.Path,
) -> None:
    """An untrusted GitHub asset fails staging before either destination writes."""
    fake = fake_release(tmp_path)
    (fake.github / _GITHUB_ONLY).write_bytes(b"untrusted carried bytes")
    draft, env = _job_directory(fake, "draft-release")
    _step("draft-release", "Snapshot the release assets", draft, env)
    _step("draft-release", "Verify carried asset provenance", draft, env)

    pypi, env = _job_directory(fake, "publish-pypi")
    shutil.copytree(draft / "github-state", pypi / "github-state")
    _step("publish-pypi", "Fetch the PyPI index", pypi, env)
    local_bytes = _local(_GITHUB_ONLY)
    completed = run_bash(
        step_script("publish-pypi", "Skip artefacts already on PyPI"),
        pypi,
        env,
    )

    assert completed.returncode != 0, "unverified release bytes must stop staging"
    assert _GITHUB_ONLY in completed.stderr, (
        "the staging failure must name the unverified release asset"
    )
    assert "delete the named asset and rerun" in completed.stderr, (
        "the staging failure must explain the recovery action"
    )
    assert (pypi / "dist" / "publish" / _GITHUB_ONLY).read_bytes() == local_bytes, (
        "failed provenance validation must preserve this run's local bytes"
    )
    assert not (fake.pypi / _GITHUB_ONLY).exists(), "PyPI receives no bytes"


def test_reported_digest_mismatch_stops_before_pypi_upload(fake: FakeRelease) -> None:
    """Reject carried bytes that disagree with GitHub's release metadata."""
    reported_digest = sha256_hex(b"different release metadata")
    fake.reported_digests[_GITHUB_ONLY] = reported_digest
    draft, env = _job_directory(fake, "draft-release")
    _step("draft-release", "Snapshot the release assets", draft, env)
    _step("draft-release", "Verify carried asset provenance", draft, env)

    record = draft / "github-state" / "provenance" / f"{_GITHUB_ONLY}.json"
    assert json.loads(record.read_text(encoding="utf-8")) == provenance_record(
        _GITHUB_ONLY, sha256_hex(b"github")
    ), "the verifier must attest the downloaded bytes, not the metadata override"

    pypi, env = _job_directory(fake, "publish-pypi")
    shutil.copytree(draft / "github-state", pypi / "github-state")
    _step("publish-pypi", "Fetch the PyPI index", pypi, env)
    publish = pypi / "dist" / "publish"
    before_publish = _holdings(publish)
    before_pypi = _holdings(fake.pypi)
    completed = run_bash(
        step_script("publish-pypi", "Skip artefacts already on PyPI"), pypi, env
    )

    if completed.returncode == 0:
        for staged in publish.iterdir():
            destination = fake.pypi / staged.name
            if not destination.exists():
                shutil.copyfile(staged, destination)
        _publish_release(fake)

    assert completed.returncode != 0, "mismatched release metadata must stop staging"
    assert _GITHUB_ONLY in completed.stderr, "the error must name the carried asset"
    assert f"expected {reported_digest}" in completed.stderr, (
        "the error must identify the independently reported digest"
    )
    assert "delete the named asset and rerun" in completed.stderr, (
        "the error must explain the recovery action"
    )
    assert _holdings(publish) == before_publish, "staging must not change local bytes"
    assert _holdings(fake.pypi) == before_pypi, "PyPI must receive no uploaded bytes"
    assert not (fake.root / "publish-release").exists(), (
        "a failed staging step must prevent the publish-release job"
    )
    assert not any(call[:2] == ["release", "upload"] for call in fake.calls("gh")), (
        "a failed staging step must not upload to the GitHub release"
    )


def test_each_destination_is_sent_only_what_it_lacks(fake: FakeRelease) -> None:
    """GitHub is sent the PyPI-only and new names and the bundle, nothing else."""
    _draft_and_publish_pypi(fake)
    _publish_release(fake)

    uploads = [call for call in fake.calls("gh") if call[:2] == ["release", "upload"]]
    assert len(uploads) == 1, uploads
    sent = sorted(path.rsplit("/", 1)[-1] for path in uploads[0][3:])
    assert sent == sorted((_PYPI_ONLY, _NEITHER, _BUNDLE))
    assert not any("--clobber" in call for call in fake.calls("gh"))


def test_a_rerun_after_success_uploads_nothing(fake: FakeRelease) -> None:
    """A second complete run finds every name on both sides and changes nothing."""
    _draft_and_publish_pypi(fake)
    _publish_release(fake)
    before = (_holdings(fake.pypi), _holdings(fake.github))
    for job in ("draft-release", "publish-pypi", "publish-release"):
        shutil.rmtree(fake.root / job)

    _draft_and_publish_pypi(fake)
    cwd = _publish_release(fake)

    assert (_holdings(fake.pypi), _holdings(fake.github)) == before
    assert outputs(fake.root / "publish-pypi" / "output")["remaining"] == "false"
    assert outputs(cwd / "output")["pending"] == "false"


def test_the_check_fails_when_the_destinations_disagree(fake: FakeRelease) -> None:
    """Differing bytes under one name stop the release before it is visible."""
    (fake.github / _BOTH).write_bytes(b"clobbered by an earlier workflow")
    _draft_and_publish_pypi(fake)
    cwd = _publish_release(fake, verify=False)

    completed = run_bash(
        step_script("publish-release", "Check both destinations hold the same bytes"),
        cwd,
        fake.env(GITHUB_REF_NAME=_TAG, GITHUB_OUTPUT=str(cwd / "output")),
    )

    assert completed.returncode != 0, "a digest mismatch must fail the release"
    assert f"{_BOTH} differs" in completed.stderr
    assert (fake.github / _BOTH).read_bytes() == b"clobbered by an earlier workflow"


def test_bytes_that_do_not_match_the_index_are_never_uploaded(
    fake: FakeRelease,
) -> None:
    """A PyPI download whose digest differs from the index stops the upload."""
    _draft_and_publish_pypi(fake)
    cwd, env = _job_directory(fake, "publish-release")
    for name in _PUBLISH_STEPS[:2]:
        _step("publish-release", name, cwd, env)
    (fake.pypi / _PYPI_ONLY).write_bytes(b"tampered in transit")
    _step("publish-release", _PUBLISH_STEPS[2], cwd, env)

    completed = run_bash(step_script("publish-release", _PUBLISH_STEPS[3]), cwd, env)

    assert completed.returncode != 0, "a download must match PyPI's digest"
    assert _PYPI_ONLY in completed.stderr
    assert _PYPI_ONLY not in _holdings(fake.github)


def test_pypi_downloads_retry_and_are_bounded(fake: FakeRelease) -> None:
    """Every PyPI file download keeps the retry and timeout flags."""
    _draft_and_publish_pypi(fake)
    _publish_release(fake)

    downloads = [call for call in fake.calls("curl") if "--fail" in call]
    assert downloads, "the PyPI-only name must be fetched from PyPI"
    for call in downloads:
        joined = " ".join(call)
        for flag in ("--retry 5", "--retry-all-errors", "--connect-timeout 10"):
            assert flag in joined, f"a PyPI download must pass {flag}"


def test_a_pypi_only_name_uploads_when_the_bundle_is_already_attached(
    fake: FakeRelease,
) -> None:
    """A re-run of ``publish-release`` alone still fetches PyPI's missing bytes."""
    for name in _ARTEFACTS:
        (fake.pypi / name).write_bytes(f"published {name}".encode())
        if name != _PYPI_ONLY:
            shutil.copyfile(fake.pypi / name, fake.github / name)
    fake.attest_pypi_holdings()
    (fake.github / _BUNDLE).write_bytes(b"bundle")

    cwd = _publish_release(fake)

    assert outputs(cwd / "output")["pending"] == "true"
    assert (fake.github / _PYPI_ONLY).read_bytes() == f"published {_PYPI_ONLY}".encode()


def _ready_to_verify(
    fake: FakeRelease, *, extra: str | None = None
) -> tuple[pth.Path, dict[str, str]]:
    """Run ``publish-release`` up to, but not including, the provenance gate.

    ``extra`` names one more locally built artefact to stage, so a test can
    present more than the four fixed reconciliation cases.

    Returns
    -------
    tuple[pathlib.Path, dict[str, str]]
        The job's workspace and the environment its steps run with.
    """
    _draft_and_publish_pypi(fake)
    cwd, env = _job_directory(fake, "publish-release")
    if extra is not None:
        (cwd / "dist" / "publish" / extra).write_bytes(_local(extra))
    for name in _PRE_PROVENANCE:
        _step("publish-release", name, cwd, env)
    return cwd, env


def _no_github_upload(fake: FakeRelease) -> None:
    """Require that no asset reached the GitHub Release."""
    uploads = [call for call in fake.calls("gh") if call[:2] == ["release", "upload"]]
    assert not uploads, "a failed provenance gate must not upload anything"


def test_a_fetched_file_without_provenance_is_never_uploaded(
    tmp_path: pth.Path,
) -> None:
    """The index digest alone must not place bytes on the release."""
    fake = _seeded(tmp_path)
    cwd, env = _ready_to_verify(fake)

    completed = run_bash(step_script("publish-release", _PROVENANCE_STEP), cwd, env)

    assert completed.returncode != 0, "an unattested file must stop the upload"
    assert _PYPI_ONLY in completed.stderr, "the failure must name the file"
    assert "Could not verify build provenance" in completed.stderr
    assert _PYPI_ONLY not in _holdings(fake.github)
    _no_github_upload(fake)


def test_the_index_digest_cannot_authorize_other_bytes(tmp_path: pth.Path) -> None:
    """An attestation for genuine bytes never covers malicious replacements."""
    fake = _seeded(tmp_path)
    fake.attest_approved(fake.pypi / _PYPI_ONLY)
    (fake.pypi / _PYPI_ONLY).write_bytes(b"malicious bytes matching the index")
    cwd, env = _ready_to_verify(fake)

    completed = run_bash(step_script("publish-release", _PROVENANCE_STEP), cwd, env)

    assert completed.returncode != 0, (
        "bytes the index describes but no run attested must stop the upload"
    )
    assert _PYPI_ONLY in completed.stderr
    assert _PYPI_ONLY not in _holdings(fake.github)
    _no_github_upload(fake)


def test_an_attested_pypi_file_carries_over_to_the_release(
    fake: FakeRelease,
) -> None:
    """PyPI's bytes may be uploaded when this tag and commit attested them."""
    _draft_and_publish_pypi(fake)
    cwd = _publish_release(fake)

    assert (fake.github / _PYPI_ONLY).read_bytes() == b"pypi", (
        "approved PyPI bytes must reach the release"
    )
    evidence = cwd / "pypi-provenance" / f"{_PYPI_ONLY}.json"
    assert evidence.exists(), "the verifier record must be kept for the gate"
    assert not (cwd / "github-upload" / f"{_PYPI_ONLY}.json").exists(), (
        "verifier output must stay outside the directory that is uploaded"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        AttestationOverrides(source_ref="refs/tags/v9.9.9"),
        AttestationOverrides(source_digest="b" * 40),
        AttestationOverrides(
            signer_workflow="attacker/other/.github/workflows/release.yml"
        ),
    ],
    ids=["other-tag", "other-commit", "other-signer"],
)
def test_evidence_for_another_identity_does_not_authorize_the_bytes(
    tmp_path: pth.Path, overrides: AttestationOverrides
) -> None:
    """Only this tag, commit, and signer workflow may approve the upload."""
    fake = _seeded(tmp_path)
    fake.add_attestation(fake.pypi / _PYPI_ONLY, overrides)
    cwd, env = _ready_to_verify(fake)

    completed = run_bash(step_script("publish-release", _PROVENANCE_STEP), cwd, env)

    assert completed.returncode != 0, "a foreign identity must not authorize bytes"
    assert _PYPI_ONLY in completed.stderr
    assert _PYPI_ONLY not in _holdings(fake.github)
    _no_github_upload(fake)


def test_a_subject_name_mismatch_is_rejected(tmp_path: pth.Path) -> None:
    """A record must name the exact file, not merely carry its digest."""
    fake = _seeded(tmp_path)
    fake.add_attestation(
        fake.pypi / _PYPI_ONLY, AttestationOverrides(subject_name="other.whl")
    )
    cwd, env = _ready_to_verify(fake)

    completed = run_bash(step_script("publish-release", _PROVENANCE_STEP), cwd, env)

    assert completed.returncode != 0, "the digest alone must not authorize the file"
    assert _PYPI_ONLY in completed.stderr
    assert "has no approved build provenance" in completed.stderr
    assert _PYPI_ONLY not in _holdings(fake.github)
    _no_github_upload(fake)


def test_a_later_unattested_file_stops_the_whole_upload(tmp_path: pth.Path) -> None:
    """One passing candidate must not let a later failing one slip through."""
    later = "c-2-py3-none-any.whl"
    fake = _seeded(tmp_path)
    fake.attest_approved(fake.pypi / _PYPI_ONLY)
    (fake.pypi / later).write_bytes(b"second pypi file")
    cwd, env = _ready_to_verify(fake, extra=later)

    completed = run_bash(step_script("publish-release", _PROVENANCE_STEP), cwd, env)

    assert completed.returncode != 0, "the later candidate must stop the upload"
    assert later in completed.stderr, "the failure must name the later file"
    assert _PYPI_ONLY not in _holdings(fake.github), (
        "an earlier verified file must not be uploaded either"
    )
    _no_github_upload(fake)


def test_an_old_github_cli_fails_before_any_verification(fake: FakeRelease) -> None:
    """An unpatched CLI cannot be trusted to enforce the identity flags."""
    cwd, env = _ready_to_verify(fake)
    before = len(fake.calls("gh"))

    completed = run_bash(
        step_script("publish-release", _PROVENANCE_STEP),
        cwd,
        {**env, "FAKE_GH_VERSION": "2.101.9"},
    )

    assert completed.returncode != 0, "an old CLI must fail the gate"
    assert "GitHub CLI 2.102.0 or newer is required; found 2.101.9." in (
        completed.stderr
    )
    assert not any(
        call[:2] == ["attestation", "verify"] for call in fake.calls("gh")[before:]
    ), "no attestation may be checked with an unpatched CLI"


def test_the_verifier_is_called_with_the_full_identity_policy(
    fake: FakeRelease,
) -> None:
    """Every identity flag the policy depends on must reach the verifier."""
    _draft_and_publish_pypi(fake)
    _publish_release(fake)
    env = fake.env()
    verification = next(
        call
        for call in fake.calls("gh")
        if call[:2] == ["attestation", "verify"]
        and call[2].startswith("github-upload/")
    )

    assert verification == [
        "attestation",
        "verify",
        f"github-upload/{_PYPI_ONLY}",
        "--repo",
        env["GITHUB_REPOSITORY"],
        "--signer-workflow",
        f"{env['GITHUB_REPOSITORY']}/.github/workflows/release.yml",
        "--source-ref",
        env["GITHUB_REF"],
        "--source-digest",
        env["GITHUB_SHA"],
        "--predicate-type",
        _PREDICATE_TYPE,
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ], "the verifier must receive exactly the release policy's identity"
