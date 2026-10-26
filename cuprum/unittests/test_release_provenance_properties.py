"""Generated invariants for release provenance parsing and staging."""

from __future__ import annotations

import dataclasses as dc
import json
import pathlib as pth
import tempfile
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scripts.release_assets import ReleaseAssetError, State, stage_pypi
from scripts.release_provenance import has_matching_subject
from tests.helpers.release_asset_provenance import _digest, _provenance

if typ.TYPE_CHECKING:
    from hypothesis.strategies import DrawFn

_JSON_LEAVES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(),
)
_JSON_VALUES = st.recursive(
    _JSON_LEAVES,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(), children, max_size=4),
    ),
    max_leaves=12,
)


@dc.dataclass(frozen=True, slots=True)
class _MatchingSubjectCase:
    """JSON document containing a generated matching subject."""

    name: str
    digest: str
    document: list[object]


@st.composite
def _matching_subject_cases(draw: DrawFn) -> _MatchingSubjectCase:
    """Build a valid subject among unrelated results and subjects."""
    name = draw(st.text(min_size=1))
    digest = draw(st.text(alphabet="0123456789abcdef", min_size=64, max_size=64))
    subject_prefix = draw(st.lists(_JSON_VALUES, max_size=4))
    subject_suffix = draw(st.lists(_JSON_VALUES, max_size=4))
    result_prefix = draw(st.lists(_JSON_VALUES, max_size=4))
    result_suffix = draw(st.lists(_JSON_VALUES, max_size=4))
    subject = {"name": name, "digest": {"sha256": digest.upper()}}
    result = {
        "verificationResult": {
            "statement": {"subject": [*subject_prefix, subject, *subject_suffix]}
        }
    }
    return _MatchingSubjectCase(name, digest, [*result_prefix, result, *result_suffix])


@dc.dataclass(frozen=True, slots=True)
class _BatchDirectories:
    """Scratch directories used by one generated staging case."""

    github_files: pth.Path
    provenance: pth.Path
    publish: pth.Path


@dc.dataclass(frozen=True, slots=True)
class _BatchAsset:
    """Current and carried bytes for one generated release filename."""

    name: str
    local_bytes: bytes
    carried_bytes: bytes


@dc.dataclass(frozen=True, slots=True)
class _BatchStagingCase:
    """Prepared state and expected publish-directory snapshots."""

    state: State
    directories: _BatchDirectories
    before: dict[str, bytes]
    carried_bytes: dict[str, bytes]
    rejected_name: str


def _make_batch_directories(root: pth.Path) -> _BatchDirectories:
    """Create the local, carried and provenance directories for a batch."""
    publish = root / "publish"
    github_files = root / "github-files"
    provenance = root / "provenance"
    for directory in (publish, github_files, provenance):
        directory.mkdir(parents=True)
    return _BatchDirectories(github_files, provenance, publish)


def _batch_assets(payloads: list[bytes], reverse_order: bool) -> list[_BatchAsset]:
    """Build distinguishable current and carried bytes for generated assets."""
    assets = [
        _BatchAsset(f"asset-{index}.whl", b"current:" + payload, b"carried:" + payload)
        for index, payload in enumerate(payloads)
    ]
    return list(reversed(assets)) if reverse_order else assets


def _write_batch_assets(
    directories: _BatchDirectories,
    assets: list[_BatchAsset],
    rejected_name: str,
    reject_one: bool,
) -> None:
    """Write generated assets and omit one selected provenance record."""
    for asset in assets:
        (directories.publish / asset.name).write_bytes(asset.local_bytes)
        (directories.github_files / asset.name).write_bytes(asset.carried_bytes)
        if not reject_one or asset.name != rejected_name:
            (directories.provenance / f"{asset.name}.json").write_text(
                json.dumps(_provenance(asset.name, _digest(asset.carried_bytes))),
                encoding="utf-8",
            )


def _make_batch_staging_case(
    root: pth.Path,
    payloads: list[bytes],
    *,
    reverse_order: bool,
    reject_one: bool,
    rejected_position: int,
) -> _BatchStagingCase:
    """Create isolated local and carried files for one generated batch."""
    directories = _make_batch_directories(root)
    assets = _batch_assets(payloads, reverse_order)
    rejected_name = f"asset-{rejected_position % len(assets)}.whl"
    _write_batch_assets(directories, assets, rejected_name, reject_one)
    carried_bytes = {asset.name: asset.carried_bytes for asset in assets}
    github_digests: dict[str, str | None] = {
        name: _digest(content) for name, content in carried_bytes.items()
    }
    state = State(directories.publish, {}, github_digests)
    before = {path.name: path.read_bytes() for path in directories.publish.iterdir()}
    return _BatchStagingCase(state, directories, before, carried_bytes, rejected_name)


@given(document=_JSON_VALUES, name=st.text(), digest=st.text())
def test_subject_parser_is_total_for_json_values(
    document: object, name: str, digest: str
) -> None:
    """Untrusted JSON shapes produce a boolean instead of lookup errors."""
    assert isinstance(has_matching_subject(document, name, digest), bool)


@given(case=_matching_subject_cases())
def test_matching_subject_is_found_among_unrelated_json(
    case: _MatchingSubjectCase,
) -> None:
    """An exact subject matches in any position and ignores digest casing."""
    assert has_matching_subject(case.document, case.name, case.digest)


@given(
    name=st.text(min_size=1),
    digest=st.text(alphabet="0123456789abcdef", min_size=64, max_size=64),
    wrong_name=st.booleans(),
)
def test_subject_parser_rejects_a_near_match(
    name: str, digest: str, wrong_name: bool
) -> None:
    """Changing either identity field makes the subject fail the match."""
    subject_name = f"{name}\0" if wrong_name else name
    first_digit = "1" if digest[0] == "0" else "0"
    subject_digest = digest if wrong_name else first_digit + digest[1:]

    assert not has_matching_subject(
        _provenance(subject_name, subject_digest), name, digest
    )


@given(
    payloads=st.lists(st.binary(min_size=1, max_size=32), min_size=2, max_size=5),
    reverse_order=st.booleans(),
    reject_one=st.booleans(),
    rejected_position=st.integers(min_value=0, max_value=4),
)
def test_batch_staging_preserves_validation_before_copy_in_any_order(
    payloads: list[bytes],
    reverse_order: bool,
    reject_one: bool,
    rejected_position: int,
) -> None:
    """Generated batches copy verified assets or preserve all local bytes."""
    with tempfile.TemporaryDirectory(prefix="release-provenance-") as directory:
        case = _make_batch_staging_case(
            pth.Path(directory),
            payloads,
            reverse_order=reverse_order,
            reject_one=reject_one,
            rejected_position=rejected_position,
        )
        if reject_one:
            with pytest.raises(ReleaseAssetError, match=case.rejected_name):
                stage_pypi(
                    case.state,
                    case.directories.github_files,
                    case.directories.provenance,
                )
            expected = case.before
        else:
            assert stage_pypi(
                case.state,
                case.directories.github_files,
                case.directories.provenance,
            )
            expected = case.carried_bytes

        actual = {
            path.name: path.read_bytes() for path in case.directories.publish.iterdir()
        }
    assert actual == expected
