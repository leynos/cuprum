"""Regression coverage for provenance checks on carried release assets."""

from __future__ import annotations

import json
import typing as typ

import pytest

from scripts.release_assets import PypiFile, ReleaseAssetError, State, stage_pypi
from tests.helpers.release_asset_provenance import provenance_record, sha256_hex

if typ.TYPE_CHECKING:
    import pathlib as pth

_ASSET = "cuprum-1.2.3-py3-none-any.whl"
_PYPI_ASSET = "a-cuprum-1.2.3.tar.gz"


@pytest.fixture
def release_paths(
    tmp_path: pth.Path,
) -> tuple[pth.Path, pth.Path, pth.Path]:
    """Create the local, carried, and provenance directories for a release case."""
    publish = tmp_path / "publish"
    github_files = tmp_path / "github-files"
    provenance = tmp_path / "provenance"
    for directory in (publish, github_files, provenance):
        directory.mkdir()
    return publish, github_files, provenance


def test_unattested_carried_bytes_cannot_replace_the_local_build(
    release_paths: tuple[pth.Path, pth.Path, pth.Path],
) -> None:
    """GitHub's self-reported digest cannot authorize a different wheel."""
    local_bytes = b"attested bytes from this run"
    carried_bytes = b"unattested bytes already on GitHub"
    publish, github_files, provenance = release_paths
    (publish / _ASSET).write_bytes(local_bytes)
    (github_files / _ASSET).write_bytes(carried_bytes)
    state = State(
        publish=publish,
        pypi={},
        github={_ASSET: sha256_hex(carried_bytes)},
    )

    with pytest.raises(ReleaseAssetError, match=_ASSET) as error:
        stage_pypi(state, github_files, provenance)

    assert (publish / _ASSET).read_bytes() == local_bytes, (
        "rejected provenance must preserve the local attested wheel"
    )
    assert "delete the named asset and rerun" in str(error.value), (
        "the error must explain how to recover from an untrusted asset"
    )


@pytest.mark.parametrize(
    "record",
    [
        None,
        "not-json",
        [],
        [{"verificationResult": []}],
        [{"verificationResult": {"statement": []}}],
        [{"verificationResult": {"statement": {"subject": {}}}}],
        [{"verificationResult": {"statement": {"subject": []}}}],
        [
            {
                "verificationResult": {
                    "statement": {"subject": [{"name": _ASSET, "digest": []}]}
                }
            }
        ],
        provenance_record(_ASSET, "0" * 64),
        provenance_record(
            "other.whl", sha256_hex(b"unattested bytes already on GitHub")
        ),
        {"verificationResult": {"statement": {"subject": []}}},
    ],
    ids=[
        "absent",
        "malformed-json",
        "empty",
        "wrong-verification-result",
        "wrong-statement",
        "wrong-subject-container",
        "empty-subject",
        "wrong-digest-container",
        "wrong-digest",
        "wrong-name",
        "wrong-root",
    ],
)
def test_untrusted_records_fail_before_any_publish_mutation(
    release_paths: tuple[pth.Path, pth.Path, pth.Path], record: object
) -> None:
    """Invalid provenance cannot replace carried bytes or delete PyPI files."""
    local_bytes = b"attested bytes from this run"
    pypi_bytes = b"already published"
    carried_bytes = b"unattested bytes already on GitHub"
    publish, github_files, provenance = release_paths
    (publish / _PYPI_ASSET).write_bytes(pypi_bytes)
    (publish / _ASSET).write_bytes(local_bytes)
    (github_files / _ASSET).write_bytes(carried_bytes)
    if record is not None:
        (provenance / f"{_ASSET}.json").write_text(
            record if isinstance(record, str) else json.dumps(record),
            encoding="utf-8",
        )
    state = State(
        publish=publish,
        pypi={_PYPI_ASSET: PypiFile(sha256_hex(pypi_bytes), "https://pypi.test/file")},
        github={_ASSET: sha256_hex(carried_bytes)},
    )
    before = {path.name: path.read_bytes() for path in publish.iterdir()}

    with pytest.raises(ReleaseAssetError, match=_ASSET):
        stage_pypi(state, github_files, provenance)

    assert {path.name: path.read_bytes() for path in publish.iterdir()} == before, (
        "invalid provenance must leave every publish-directory byte unchanged"
    )


def test_all_carried_assets_are_checked_before_any_copy(
    release_paths: tuple[pth.Path, pth.Path, pth.Path],
) -> None:
    """A later untrusted asset cannot leave an earlier asset partly staged."""
    names = ("a-first.whl", "b-second.whl")
    local_bytes = {name: f"fresh {name}".encode() for name in names}
    carried_bytes = {name: f"prior {name}".encode() for name in names}
    publish, github_files, provenance = release_paths
    for name in names:
        (publish / name).write_bytes(local_bytes[name])
        (github_files / name).write_bytes(carried_bytes[name])
    (provenance / f"{names[0]}.json").write_text(
        json.dumps(provenance_record(names[0], sha256_hex(carried_bytes[names[0]]))),
        encoding="utf-8",
    )
    state = State(
        publish,
        {},
        {name: sha256_hex(content) for name, content in carried_bytes.items()},
    )

    with pytest.raises(ReleaseAssetError, match=names[1]):
        stage_pypi(state, github_files, provenance)

    assert {name: (publish / name).read_bytes() for name in names} == local_bytes, (
        "no carried asset may be copied before all provenance records validate"
    )


def test_matching_provenance_allows_a_verified_prior_run_asset(
    release_paths: tuple[pth.Path, pth.Path, pth.Path],
) -> None:
    """A prior run's exact filename and digest can be carried to PyPI."""
    local_bytes = b"attested bytes from this run"
    carried_bytes = b"verified bytes from an interrupted run"
    publish, github_files, provenance = release_paths
    (publish / _ASSET).write_bytes(local_bytes)
    (github_files / _ASSET).write_bytes(carried_bytes)
    expected_digest = sha256_hex(carried_bytes)
    (provenance / f"{_ASSET}.json").write_text(
        json.dumps(provenance_record(_ASSET, expected_digest.upper())), encoding="utf-8"
    )
    state = State(publish, {}, {_ASSET: expected_digest})

    remaining = stage_pypi(state, github_files, provenance)

    assert remaining is True, "a verified carried asset must remain publishable"
    assert (publish / _ASSET).read_bytes() == carried_bytes, (
        "approved prior-run bytes may replace the current build"
    )


def test_identical_local_bytes_need_no_prior_run_record(
    release_paths: tuple[pth.Path, pth.Path, pth.Path],
) -> None:
    """The current run's attested bytes already match the release digest."""
    local_bytes = b"same attested bytes"
    publish, github_files, provenance = release_paths
    (publish / _ASSET).write_bytes(local_bytes)
    state = State(publish, {}, {_ASSET: sha256_hex(local_bytes)})

    assert stage_pypi(state, github_files, provenance), (
        "identical local bytes should stay in the PyPI staging directory"
    )
    assert (publish / _ASSET).read_bytes() == local_bytes, (
        "matching local bytes must be retained without a provenance record"
    )


def test_pypi_held_asset_needs_no_carried_provenance(
    release_paths: tuple[pth.Path, pth.Path, pth.Path],
) -> None:
    """PyPI's existing filename is dropped without inspecting GitHub records."""
    local_bytes = b"rebuilt bytes"
    publish, github_files, provenance = release_paths
    (publish / _ASSET).write_bytes(local_bytes)
    state = State(
        publish,
        {_ASSET: PypiFile(sha256_hex(b"pypi bytes"), "https://pypi.test/file")},
        {_ASSET: sha256_hex(b"untrusted github bytes")},
    )

    assert stage_pypi(state, github_files, provenance) is False, (
        "PyPI-held names must not remain in the upload directory"
    )
    assert not (publish / _ASSET).exists(), (
        "PyPI's existing file should be removed from the pending upload"
    )
