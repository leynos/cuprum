"""Download the latest successful `main` benchmark baseline artifact."""

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
class ArtifactQuery:
    """GitHub Actions workflow artifact lookup configuration."""

    repository: str
    workflow: str
    branch: str
    event: str
    artifact_name: str
    api_base_url: str = GITHUB_API_BASE_URL


def _find_artifact_url_in_run(
    *,
    artifacts_payload: cabc.Mapping[str, object],
    run_id: int,
    artifact_name: str,
) -> str | None:
    """Return the download URL for a matching non-expired artifact, or ``None``."""
    artifacts = _require_list(
        artifacts_payload.get("artifacts"),
        name=f"artifacts for run {run_id}",
    )
    for artifact_index, artifact_value in enumerate(artifacts):
        artifact = _require_mapping(
            artifact_value,
            name=f"artifacts[{artifact_index}] for run {run_id}",
        )
        name = _require_non_empty_string(
            artifact.get("name"),
            name=f"artifacts[{artifact_index}].name for run {run_id}",
        )
        expired = _require_bool(
            artifact.get("expired"),
            name=f"artifacts[{artifact_index}].expired for run {run_id}",
        )
        if name != artifact_name or expired:
            continue
        return _require_non_empty_string(
            artifact.get("archive_download_url"),
            name=(f"artifacts[{artifact_index}].archive_download_url for run {run_id}"),
        )
    return None


def select_latest_artifact_download_url(
    *,
    workflow_runs_payload: cabc.Mapping[str, object],
    artifacts_payload_by_run: cabc.Mapping[int, cabc.Mapping[str, object]],
    artifact_name: str,
) -> str | None:
    """Return the latest non-expired artifact download URL, if available.

    Parameters
    ----------
    workflow_runs_payload : cabc.Mapping[str, object]
        The GitHub Actions workflow-runs API payload to search.
    artifacts_payload_by_run : cabc.Mapping[int, cabc.Mapping[str, object]]
        Artifact-listing payloads indexed by their workflow run ID.
    artifact_name : str
        Name of the artifact to match within each run's artifacts.

    Returns
    -------
    str | None
        The download URL from the newest run with a live matching artifact,
        or ``None`` when none is found.
    """
    workflow_runs = _require_list(
        workflow_runs_payload.get("workflow_runs"),
        name="workflow_runs",
    )
    for index, run_value in enumerate(workflow_runs):
        run = _require_mapping(run_value, name=f"workflow_runs[{index}]")
        run_id = _require_int(run.get("id"), name=f"workflow_runs[{index}].id")
        artifacts_payload = artifacts_payload_by_run.get(run_id)
        if artifacts_payload is None:
            continue
        url = _find_artifact_url_in_run(
            artifacts_payload=artifacts_payload,
            run_id=run_id,
            artifact_name=artifact_name,
        )
        if url is not None:
            return url
    return None


def _artifact_member_path(*, output_dir: pth.Path, archive_name: str) -> pth.Path:
    """Return the normalised extraction path for an archive member."""
    destination = (output_dir / archive_name).resolve()
    output_root = output_dir.resolve()
    if destination != output_root and output_root not in destination.parents:
        msg = f"archive member path escapes output directory: {archive_name!r}"
        raise ValueError(msg)
    return destination


def extract_artifact_archive(
    *,
    archive_bytes: bytes,
    output_dir: pth.Path,
) -> tuple[pth.Path, ...]:
    """Extract a downloaded artifact zip into *output_dir* safely.

    Parameters
    ----------
    archive_bytes : bytes
        Raw bytes of the downloaded artifact ZIP archive.
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
            destination = _artifact_member_path(
                output_dir=output_dir,
                archive_name=member.filename,
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, mode="r") as source:
                destination.write_bytes(source.read())
            extracted_paths.append(destination)
    return tuple(extracted_paths)


def find_latest_artifact_download_url(
    *,
    query: ArtifactQuery,
    token: str,
) -> str | None:
    """Query GitHub Actions and return the latest matching artifact URL.

    Parameters
    ----------
    query : ArtifactQuery
        The artifact lookup describing the repository, workflow, branch,
        event, artifact name, and API base URL.
    token : str
        GitHub authentication token sent with the API requests.

    Returns
    -------
    str | None
        The download URL for the latest matching artifact, or ``None`` when
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

    artifacts_payload_by_run: dict[int, cabc.Mapping[str, object]] = {}
    for index, run_value in enumerate(workflow_runs):
        run = _require_mapping(run_value, name=f"workflow_runs[{index}]")
        run_id = _require_int(run.get("id"), name=f"workflow_runs[{index}].id")
        artifacts_url = (
            f"{query.api_base_url}/repos/{encoded_repository}/actions/runs/"
            f"{run_id}/artifacts?per_page=100"
        )
        artifacts_payload_by_run[run_id] = _load_json_response(
            url=artifacts_url,
            token=token,
        )

    return select_latest_artifact_download_url(
        workflow_runs_payload=workflow_runs_payload,
        artifacts_payload_by_run=artifacts_payload_by_run,
        artifact_name=query.artifact_name,
    )


def _parse_args(argv: cabc.Sequence[str] | None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
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
        help="Artifact name to download from the latest successful run.",
    )
    parser.add_argument(
        "--output-dir",
        type=pth.Path,
        required=True,
        help="Directory that receives the extracted artifact files.",
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
    """Run the baseline artifact fetch CLI.

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

    download_url = find_latest_artifact_download_url(
        query=ArtifactQuery(
            repository=args.repository,
            workflow=args.workflow,
            branch=args.branch,
            event=args.event,
            artifact_name=args.artifact_name,
        ),
        token=token,
    )
    if download_url is None:
        return MAIN_BASELINE_NOT_FOUND_EXIT_CODE

    archive_bytes = _download_bytes(url=download_url, token=token)
    extract_artifact_archive(
        archive_bytes=archive_bytes,
        output_dir=args.output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
