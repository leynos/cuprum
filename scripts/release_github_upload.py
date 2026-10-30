"""Stage and verify the files the GitHub Release upload will receive.

``publish-release`` stages every artefact the release lacks, checks that each
file fetched from PyPI matches PyPI's index digest, and then requires approved
build provenance for those exact bytes. Only then does the workflow upload
them, so PyPI's index cannot by itself authorise bytes the release publishes.

The CLI imports this module in the release job, so it stays
standard-library-only; workflow ``curl``/``gh`` steps own network access.
``scripts/release_assets.py`` owns the canonical-source decisions; this module
owns how their GitHub upload set is assembled and checked.
"""

from __future__ import annotations

import shutil
import typing as typ

from scripts.release_assets import (
    PypiFile,
    ReleaseAssetError,
    State,
    require_digest,
    sha256_of,
)
from scripts.release_provenance import ProvenanceRecordError, require_matching_record

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


def _pypi_fetch_list(
    lacking: cabc.Iterable[str], pypi: cabc.Mapping[str, PypiFile]
) -> list[tuple[str, str]]:
    """Return the PyPI (name, url) pairs to fetch, insisting on HTTPS."""
    fetch = [(name, pypi[name].url) for name in lacking if name in pypi]
    if any(not url.startswith("https://") for _, url in fetch):
        msg = "every PyPI download must use HTTPS"
        raise ReleaseAssetError(msg)
    return fetch


def _copy_pypi_gap(
    lacking: cabc.Iterable[str],
    pypi: cabc.Mapping[str, PypiFile],
    publish: Path,
    output: Path,
) -> None:
    """Copy each lacking artefact that PyPI does not list into ``output``."""
    for name in lacking:
        if name not in pypi:
            shutil.copyfile(publish / name, output / name)


def _copy_unattached_bundles(
    bundles: Path, github: cabc.Mapping[str, str | None], output: Path
) -> None:
    """Copy each bundle the GitHub Release does not already hold into ``output``."""
    for bundle in sorted(bundles.iterdir()):
        if bundle.name not in github:
            shutil.copyfile(bundle, output / bundle.name)


def stage_github(state: State, bundles: Path, output: Path) -> list[tuple[str, str]]:
    """Stage what the GitHub Release lacks; return the PyPI files to fetch."""
    lacking = sorted(state.plan().github_uploads)
    fetch = _pypi_fetch_list(lacking, state.pypi)
    output.mkdir(parents=True, exist_ok=True)
    _copy_pypi_gap(lacking, state.pypi, state.publish, output)
    _copy_unattached_bundles(bundles, state.github, output)
    return fetch


def check_fetched(state: State, output: Path) -> None:
    """Raise unless every file fetched from PyPI matches PyPI's digest."""
    for name in sorted(state.plan().github_uploads & set(state.pypi)):
        require_digest(output / name, state.pypi[name].sha256)


def _provenance_failure(name: str, staged: Path, evidence: Path) -> str | None:
    """Describe why one fetched file lacks approved build provenance."""
    try:
        require_matching_record(evidence / f"{name}.json", name, sha256_of(staged))
    except OSError:
        return "cannot read the fetched bytes"
    except ProvenanceRecordError as error:
        return str(error)
    return None


def check_provenance(state: State, output: Path, evidence: Path) -> None:
    """Raise unless every fetched PyPI file has approved build provenance.

    PyPI's index digest establishes only that a download arrived intact; it
    says nothing about who built the bytes. Those bytes are uploaded to the
    GitHub Release, whose assets are meant to be traceable to this
    repository's release workflow, so each one must also match an approved
    attestation. The digest is computed from the staged bytes rather than
    taken from the index, so an index record that agrees with malicious bytes
    cannot authorise them.

    Parameters
    ----------
    state : State
        The staged artefacts and both destinations' holdings.
    output : Path
        The GitHub upload directory holding every file fetched from PyPI.
    evidence : Path
        One ``<name>.json`` verifier result per fetched file.

    Raises
    ------
    ReleaseAssetError
        When any fetched file has no approved provenance for its staged
        bytes; the message names every failing file.
    """
    problems = [
        f"{name} has no approved build provenance ({reason})"
        for name in sorted(state.plan().github_uploads & set(state.pypi))
        if (reason := _provenance_failure(name, output / name, evidence))
    ]
    if problems:
        msg = "; ".join(problems)
        raise ReleaseAssetError(msg)
