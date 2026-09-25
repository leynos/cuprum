"""End-to-end behaviour of the release's asset reconciliation across its jobs.

Each test runs the checked-in steps of ``draft-release``, ``publish-pypi``, and
``publish-release`` in order, against ``gh`` and ``curl`` stand-ins whose
directories hold the GitHub Release's assets and PyPI's files. The one step
that cannot run here, pypa's upload action, is modelled as copying the staged
files to PyPI without overwriting, which is what ``skip-existing`` promises.
The assertions are about bytes: every name ends up identical on both sides,
taken from PyPI first, then GitHub, then this run, and nothing is replaced.
"""

from __future__ import annotations

import shutil
import sys
import typing as typ

import pytest

from tests.helpers.release_workflow import (
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
_PUBLISH_STEPS = (
    "Fetch the PyPI index",
    "List the release assets",
    "Stage the assets the release lacks",
    "Check the fetched PyPI bytes",
)

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
    pypi, env = _job_directory(fake, "publish-pypi")
    shutil.copytree(draft / "github-state", pypi / "github-state")
    _step("publish-pypi", "Fetch the PyPI index", pypi, env)
    _step("publish-pypi", "Skip artefacts already on PyPI", pypi, env)
    if outputs(pypi / "output")["remaining"] == "true":
        for staged in (pypi / "dist" / "publish").iterdir():
            assert not (fake.pypi / staged.name).exists(), "PyPI never overwrites"
            shutil.copyfile(staged, fake.pypi / staged.name)


def _publish_release(fake: FakeRelease, *, verify: bool = True) -> pth.Path:
    """Run ``publish-release`` up to its digest check; return its workspace."""
    cwd, env = _job_directory(fake, "publish-release")
    for name in _PUBLISH_STEPS:
        _step("publish-release", name, cwd, env)
    if outputs(cwd / "output")["pending"] == "true":
        _step("publish-release", "Upload the assets the release lacks", cwd, env)
    if verify:
        _step(
            "publish-release", "Check both destinations hold the same bytes", cwd, env
        )
    return cwd


def _holdings(directory: pth.Path) -> dict[str, bytes]:
    """Return each file's bytes in one destination."""
    return {path.name: path.read_bytes() for path in directory.iterdir()}


@pytest.fixture
def fake(tmp_path: pth.Path) -> FakeRelease:
    """Return destinations seeded with one name per reconciliation case."""
    release = fake_release(tmp_path)
    (release.pypi / _PYPI_ONLY).write_bytes(b"pypi")
    (release.github / _GITHUB_ONLY).write_bytes(b"github")
    (release.pypi / _BOTH).write_bytes(b"both")
    (release.github / _BOTH).write_bytes(b"both")
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
    (fake.github / _BUNDLE).write_bytes(b"bundle")

    cwd = _publish_release(fake)

    assert outputs(cwd / "output")["pending"] == "true"
    assert (fake.github / _PYPI_ONLY).read_bytes() == f"published {_PYPI_ONLY}".encode()
