"""Download the latest successful `main` benchmark baseline artefact."""

from __future__ import annotations

import argparse
import dataclasses as dc
import io
import os
import pathlib as pth
import typing as typ
import urllib.parse
import zipfile

from benchmarks._github_http import _download_bytes, _load_json_response
from benchmarks._validation import (
    _require_bool,
    _require_int,
    _require_list,
    _require_mapping,
    _require_non_empty_string,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_TOKEN_ENV_VAR = "GITHUB_TOKEN"  # noqa: S105 - env var name, not a credential
MAIN_BASELINE_NOT_FOUND_EXIT_CODE = 3


@dc.dataclass(frozen=True, slots=True)
class ArtefactQuery:
    """GitHub Actions workflow artefact lookup configuration."""

    repository: str
    workflow: str
    branch: str
    event: str
    artefact_name: str
    api_base_url: str = GITHUB_API_BASE_URL


def _find_artefact_url_in_run(
    *,
    artefacts_payload: cabc.Mapping[str, object],
    run_id: int,
    artefact_name: str,
) -> str | None:
    """Return the download URL for a matching non-expired artefact, or ``None``."""
    artefacts = _require_list(
        artefacts_payload.get("artifacts"),
        name=f"artefacts for run {run_id}",
    )
    for artefact_index, artefact_value in enumerate(artefacts):
        artefact = _require_mapping(
            artefact_value,
            name=f"artefacts[{artefact_index}] for run {run_id}",
        )
        name = _require_non_empty_string(
            artefact.get("name"),
            name=f"artefacts[{artefact_index}].name for run {run_id}",
        )
        expired = _require_bool(
            artefact.get("expired"),
            name=f"artefacts[{artefact_index}].expired for run {run_id}",
        )
        if name != artefact_name or expired:
            continue
        return _require_non_empty_string(
            artefact.get("archive_download_url"),
            name=(f"artefacts[{artefact_index}].archive_download_url for run {run_id}"),
        )
    return None


def select_latest_artefact_download_url(
    *,
    workflow_runs_payload: cabc.Mapping[str, object],
    artefacts_payload_by_run: cabc.Mapping[int, cabc.Mapping[str, object]],
    artefact_name: str,
) -> str | None:
    """Return the latest non-expired artefact download URL, if available.

    Parameters
    ----------
    workflow_runs_payload : cabc.Mapping[str, object]
        The GitHub Actions workflow-runs API payload to search.
    artefacts_payload_by_run : cabc.Mapping[int, cabc.Mapping[str, object]]
        Artefact-listing payloads indexed by their workflow run ID.
    artefact_name : str
        Name of the artefact to match within each run's artefacts.

    Returns
    -------
    str | None
        The download URL from the newest run with a live matching artefact,
        or ``None`` when none is found.
    """
    workflow_runs = _require_list(
        workflow_runs_payload.get("workflow_runs"),
        name="workflow_runs",
    )
    for index, run_value in enumerate(workflow_runs):
        run = _require_mapping(run_value, name=f"workflow_runs[{index}]")
        run_id = _require_int(run.get("id"), name=f"workflow_runs[{index}].id")
        artefacts_payload = artefacts_payload_by_run.get(run_id)
        if artefacts_payload is None:
            continue
        url = _find_artefact_url_in_run(
            artefacts_payload=artefacts_payload,
            run_id=run_id,
            artefact_name=artefact_name,
        )
        if url is not None:
            return url
    return None


def _artefact_member_path(*, output_dir: pth.Path, archive_name: str) -> pth.Path:
    """Return the normalized extraction path for an archive member."""
    destination = (output_dir / archive_name).resolve()
    output_root = output_dir.resolve()
    if destination != output_root and output_root not in destination.parents:
        msg = f"archive member path escapes output directory: {archive_name!r}"
        raise ValueError(msg)
    return destination


def extract_artefact_archive(
    *,
    archive_bytes: bytes,
    output_dir: pth.Path,
) -> tuple[pth.Path, ...]:
    """Extract a downloaded artefact zip into *output_dir* safely.

    Parameters
    ----------
    archive_bytes : bytes
        Raw bytes of the downloaded artefact ZIP archive.
    output_dir : pathlib.Path
        Destination directory into which archive members are extracted.

    Returns
    -------
    tuple[pathlib.Path, ...]
        The paths of the files written during extraction.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    extracted_paths: list[pth.Path] = []
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            destination = _artefact_member_path(
                output_dir=output_dir,
                archive_name=member.filename,
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, mode="r") as source:
                destination.write_bytes(source.read())
            extracted_paths.append(destination)
    return tuple(extracted_paths)


def find_latest_artefact_download_url(
    *,
    query: ArtefactQuery,
    token: str,
) -> str | None:
    """Query GitHub Actions and return the latest matching artefact URL.

    Parameters
    ----------
    query : ArtefactQuery
        The artefact lookup describing the repository, workflow, branch,
        event, artefact name, and API base URL.
    token : str
        GitHub authentication token sent with the API requests.

    Returns
    -------
    str | None
        The download URL for the latest matching artefact, or ``None`` when
        none is available.
    """
    encoded_repository = urllib.parse.quote(query.repository, safe="/")
    encoded_workflow = urllib.parse.quote(query.workflow, safe="")
    params = urllib.parse.urlencode({
        "branch": query.branch,
        "event": query.event,
        "per_page": 20,
        "status": "success",
    })
    workflow_runs_url = (
        f"{query.api_base_url}/repos/{encoded_repository}/actions/workflows/"
        f"{encoded_workflow}/runs?{params}"
    )
    workflow_runs_payload = _load_json_response(url=workflow_runs_url, token=token)
    workflow_runs = _require_list(
        workflow_runs_payload.get("workflow_runs"),
        name="workflow_runs",
    )

    artefacts_payload_by_run: dict[int, cabc.Mapping[str, object]] = {}
    for index, run_value in enumerate(workflow_runs):
        run = _require_mapping(run_value, name=f"workflow_runs[{index}]")
        run_id = _require_int(run.get("id"), name=f"workflow_runs[{index}].id")
        artefacts_url = (
            f"{query.api_base_url}/repos/{encoded_repository}/actions/runs/"
            f"{run_id}/artifacts?per_page=100"
        )
        artefacts_payload_by_run[run_id] = _load_json_response(
            url=artefacts_url,
            token=token,
        )

    return select_latest_artefact_download_url(
        workflow_runs_payload=workflow_runs_payload,
        artefacts_payload_by_run=artefacts_payload_by_run,
        artefact_name=query.artefact_name,
    )


def _parse_args(argv: cabc.Sequence[str] | None) -> argparse.Namespace:
    """Parse CLI arguments."""
    # An explicit prog keeps the usage line naming this script regardless of
    # how the interpreter was launched: Python 3.14 derives the default from
    # the actual invocation (for example `python3 -m pytest`), not argv[0].
    parser = argparse.ArgumentParser(
        prog="fetch_main_benchmark_baseline.py",
        description=__doc__,
    )
    parser.add_argument(
        "--repository",
        required=True,
        help="GitHub repository in owner/name form.",
    )
    parser.add_argument(
        "--workflow",
        required=True,
        help="Workflow file name or workflow identifier.",
    )
    parser.add_argument(
        "--artifact-name",
        required=True,
        help="Artefact name to download from the latest successful run.",
    )
    parser.add_argument(
        "--output-dir",
        type=pth.Path,
        required=True,
        help="Directory that receives the extracted artefact files.",
    )
    parser.add_argument(
        "--branch",
        default="main",
        help="Branch to query for successful workflow runs.",
    )
    parser.add_argument(
        "--event",
        default="push",
        help="Workflow event to query for successful runs.",
    )
    parser.add_argument(
        "--token-env",
        default=GITHUB_TOKEN_ENV_VAR,
        help="Environment variable containing the GitHub token.",
    )
    return parser.parse_args(argv)


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Run the baseline artefact fetch CLI.

    Parameters
    ----------
    argv : cabc.Sequence[str] | None
        Optional CLI argument sequence; when ``None`` the process
        arguments are used.

    Returns
    -------
    int
        ``0`` on success, or the not-found exit code when no baseline exists.

    Raises
    ------
    SystemExit
        If the configured token environment variable is unset or empty.
    """
    args = _parse_args(argv)
    token = os.environ.get(args.token_env, "").strip()
    if not token:
        msg = f"missing GitHub token in environment variable {args.token_env}"
        raise SystemExit(msg)

    download_url = find_latest_artefact_download_url(
        query=ArtefactQuery(
            repository=args.repository,
            workflow=args.workflow,
            branch=args.branch,
            event=args.event,
            artefact_name=args.artefact_name,
        ),
        token=token,
    )
    if download_url is None:
        return MAIN_BASELINE_NOT_FOUND_EXIT_CODE

    archive_bytes = _download_bytes(url=download_url, token=token)
    extract_artefact_archive(
        archive_bytes=archive_bytes,
        output_dir=args.output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
