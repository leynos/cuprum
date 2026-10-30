"""Provenance checks on the PyPI bytes the GitHub Release publishes.

``check_provenance()`` is the gate before ``gh release upload``: PyPI's index
digest proves only that a download arrived intact, so each fetched file must
also match an approved attestation bound to its staged bytes. These tests pin
the rejections — absent, malformed, and split-subject evidence — and the one
case that may proceed, a record whose single subject carries both the exact
filename and the digest of the bytes actually staged.
"""

from __future__ import annotations

import json
import tempfile
import typing as typ
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scripts.release_assets import PypiFile, ReleaseAssetError, State
from scripts.release_github_upload import check_provenance
from tests.helpers.release_asset_provenance import provenance_record, sha256_hex

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_ASSET = "cuprum-1.2.3-py3-none-any.whl"
_SDIST = "cuprum-1.2.3.tar.gz"


def _fetched(name: str) -> bytes:
    """Return the bytes the release fetched from PyPI for ``name``."""
    return f"fetched {name}".encode()


def _different(digest: str) -> str:
    """Return a hex digest that differs from ``digest`` in its first digit."""
    return ("1" if digest[0] == "0" else "0") + digest[1:]


def _split_record(name: str, digest: str, other_name: str) -> str:
    """Render evidence whose filename and digest never share a subject."""
    named = {"name": name, "digest": {"sha256": _different(digest)}}
    numbered = {"name": other_name, "digest": {"sha256": digest}}
    return json.dumps([
        {"verificationResult": {"statement": {"subject": [named]}}},
        {"verificationResult": {"statement": {"subject": [numbered]}}},
    ])


def _stage(
    root: Path,
    names: cabc.Sequence[str],
    records: cabc.Mapping[str, str | None],
) -> tuple[State, Path, Path]:
    """Stage ``names`` as PyPI-only holdings with the given evidence.

    ``records`` maps a name to its verifier JSON, or to ``None`` to leave the
    record absent. Names it omits get an approved record binding the exact
    filename to the digest of the bytes staged in the GitHub upload directory.

    Returns
    -------
    tuple[State, Path, Path]
        The release state, the GitHub upload directory, and the evidence
        directory.
    """
    publish, output, evidence = root / "publish", root / "output", root / "evidence"
    for directory in (publish, output, evidence):
        directory.mkdir()
    pypi = {}
    for name in names:
        content = _fetched(name)
        (publish / name).write_bytes(f"rebuilt {name}".encode())
        (output / name).write_bytes(content)
        pypi[name] = PypiFile(sha256_hex(content), f"https://files.test/{name}")
        document = records.get(
            name, json.dumps(provenance_record(name, sha256_hex(content)))
        )
        if document is not None:
            (evidence / f"{name}.json").write_text(document, encoding="utf-8")
    return State(publish, pypi, {}), output, evidence


def _checked(
    root: Path,
    names: cabc.Sequence[str],
    records: cabc.Mapping[str, str | None],
) -> None:
    """Run the provenance gate over staged fetch results."""
    state, output, evidence = _stage(root, names, records)
    check_provenance(state, output, evidence)


def test_approved_provenance_for_the_staged_bytes_passes(
    tmp_path: Path,
) -> None:
    """A single subject carrying the exact filename and digest is sufficient."""
    _checked(tmp_path, (_ASSET, _SDIST), {})


def test_uppercase_digests_are_normalized_before_comparison(
    tmp_path: Path,
) -> None:
    """The verifier's digest spelling must not decide the outcome."""
    digest = sha256_hex(_fetched(_ASSET))
    approved = json.dumps(provenance_record(_ASSET, digest.upper()))

    _checked(tmp_path, (_ASSET,), {_ASSET: approved})


@pytest.mark.parametrize(
    "document",
    [
        None,
        "not-json",
        "[]",
        "{}",
        '{"verificationResult": []}',
        '{"verificationResult": {"statement": []}}',
        '{"verificationResult": {"statement": {"subject": {}}}}',
        '{"verificationResult": {"statement": {"subject": []}}}',
        '[{"verificationResult": {"statement": {"subject": [{"name": "'
        + _ASSET
        + '", "digest": []}]}}}]',
    ],
    ids=[
        "absent",
        "invalid-json",
        "empty-array",
        "wrong-root",
        "wrong-verification-result",
        "wrong-statement",
        "wrong-subject-container",
        "empty-subject",
        "wrong-digest-container",
    ],
)
def test_missing_or_malformed_evidence_is_rejected(
    tmp_path: Path, document: str | None
) -> None:
    """Failing closed is the only safe answer for untrustworthy evidence."""
    with pytest.raises(ReleaseAssetError, match=_ASSET):
        _checked(tmp_path, (_ASSET,), {_ASSET: document})


def test_a_split_subject_binding_is_rejected(tmp_path: Path) -> None:
    """The filename and its digest must appear in the same subject."""
    digest = sha256_hex(_fetched(_ASSET))
    split = _split_record(_ASSET, digest, other_name=f"other-{_ASSET}")

    with pytest.raises(ReleaseAssetError, match=_ASSET):
        _checked(tmp_path, (_ASSET,), {_ASSET: split})


def test_the_index_digest_cannot_authorize_other_bytes(tmp_path: Path) -> None:
    """An index record agreeing with malicious bytes is not provenance."""
    genuine = json.dumps(provenance_record(_ASSET, sha256_hex(b"genuine build")))
    state, output, evidence = _stage(tmp_path, (_ASSET,), {_ASSET: genuine})
    (output / _ASSET).write_bytes(b"malicious bytes matching the index")
    state.pypi[_ASSET] = PypiFile(
        sha256_hex(b"malicious bytes matching the index"),
        f"https://files.test/{_ASSET}",
    )

    with pytest.raises(ReleaseAssetError, match=_ASSET):
        check_provenance(state, output, evidence)


def test_every_failing_file_is_named_in_one_error(tmp_path: Path) -> None:
    """A single report must identify every file that lacks evidence."""
    with pytest.raises(ReleaseAssetError) as error:
        _checked(tmp_path, (_ASSET, _SDIST), {_ASSET: None, _SDIST: "[]"})

    assert error.value.args[0].count("has no approved build provenance") == 2, (
        "both failing files must be reported, not only the first"
    )
    assert _ASSET in error.value.args[0]
    assert _SDIST in error.value.args[0]


def test_a_name_the_release_already_holds_needs_no_evidence(tmp_path: Path) -> None:
    """Only files fetched for upload from PyPI are checked."""
    state, output, evidence = _stage(tmp_path, (_ASSET,), {_ASSET: None})
    held = State(
        state.publish,
        state.pypi,
        {_ASSET: sha256_hex(_fetched(_ASSET))},
    )

    check_provenance(held, output, evidence)


def test_a_name_pypi_lacks_needs_no_evidence(tmp_path: Path) -> None:
    """Locally built bytes are not fetched, so no record is required."""
    state, output, evidence = _stage(tmp_path, (_ASSET,), {_ASSET: None})
    local_only = State(state.publish, {}, {})

    check_provenance(local_only, output, evidence)


@given(payload=st.binary(max_size=64), suffix=st.text(min_size=1))
def test_a_split_subject_never_authorizes_the_bytes(
    payload: bytes, suffix: str
) -> None:
    """No arrangement of split subjects binds a filename to its digest."""
    digest = sha256_hex(payload)
    document = _split_record(_ASSET, digest, other_name=f"other-{suffix}")
    with tempfile.TemporaryDirectory(prefix="release-pypi-provenance-") as directory:
        root = Path(directory)
        state, output, evidence = _stage(root, (_ASSET,), {_ASSET: None})
        # The index agrees with these bytes, so only provenance can reject them.
        (output / _ASSET).write_bytes(payload)
        state.pypi[_ASSET] = PypiFile(digest, f"https://files.test/{_ASSET}")
        (evidence / f"{_ASSET}.json").write_text(document, encoding="utf-8")

        with pytest.raises(ReleaseAssetError):
            check_provenance(state, output, evidence)
