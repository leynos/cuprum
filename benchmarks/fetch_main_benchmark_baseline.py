"""Download the latest successful `main` benchmark baseline artifact."""

from __future__ import annotations

import argparse
import dataclasses as dc
import io
import json
import os
import pathlib as pth
import time
import typing as typ
import urllib.error
import urllib.parse
import urllib.request
import zipfile

GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_TOKEN_ENV_VAR = "GITHUB_TOKEN"  # noqa: S105 - env var name, not a credential
MAIN_BASELINE_NOT_FOUND_EXIT_CODE = 3
_REQUEST_TIMEOUT_SECONDS = 10.0
_RETRY_DELAYS_SECONDS = (0.5, 1.0)
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR_MIN = 500
_HTTP_SERVER_ERROR_MAX = 600


@dc.dataclass(frozen=True, slots=True)
class ArtifactQuery:
    """GitHub Actions workflow artifact lookup configuration."""

    repository: str
    workflow: str
    branch: str
    event: str
    artifact_name: str
    api_base_url: str = GITHUB_API_BASE_URL


def _require_mapping(value: object, *, name: str) -> typ.Mapping[str, object]:
    """Validate that *value* is a JSON object."""
    if not isinstance(value, dict):
        msg = f"{name} must be an object"
        raise TypeError(msg)
    return typ.cast("dict[str, object]", value)


def _require_list(value: object, *, name: str) -> list[object]:
    """Validate that *value* is a JSON array."""
    if not isinstance(value, list):
        msg = f"{name} must be a list"
        raise TypeError(msg)
    return typ.cast("list[object]", value)


def _require_non_empty_string(value: object, *, name: str) -> str:
    """Validate that *value* is a non-empty string."""
    if not isinstance(value, str) or not value.strip():
        msg = f"{name} must be a non-empty string"
        raise ValueError(msg)
    return value


def _require_int(value: object, *, name: str) -> int:
    """Validate that *value* is an integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{name} must be an integer"
        raise TypeError(msg)
    return value


def _require_bool(value: object, *, name: str) -> bool:
    """Validate that *value* is a boolean."""
    if not isinstance(value, bool):
        msg = f"{name} must be a boolean"
        raise TypeError(msg)
    return value


def _should_retry_request_failure(exc: Exception) -> bool:
    """Return ``True`` when a GitHub API failure is transient."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == _HTTP_TOO_MANY_REQUESTS or (
            _HTTP_SERVER_ERROR_MIN <= exc.code < _HTTP_SERVER_ERROR_MAX
        )
    return isinstance(exc, urllib.error.URLError)


def _with_retry[T](
    operation: typ.Callable[[], T],
    *,
    description: str,
) -> T:
    """Run *operation* with bounded retry/backoff for transient HTTP failures."""
    last_exc: Exception | None = None
    for attempt in range(len(_RETRY_DELAYS_SECONDS) + 1):
        try:
            return operation()
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            if not _should_retry_request_failure(exc):
                raise
            last_exc = exc
            if attempt == len(_RETRY_DELAYS_SECONDS):
                break
            time.sleep(_RETRY_DELAYS_SECONDS[attempt])
    if last_exc is None:
        raise RuntimeError(description)
    raise last_exc


def _load_json_response(*, url: str, token: str) -> typ.Mapping[str, object]:
    """Load a GitHub API JSON response."""
    request = urllib.request.Request(  # noqa: S310 - URL is selected by trusted caller
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "cuprum-benchmark-ratchet",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    def _open_json_response() -> typ.Mapping[str, object]:
        with urllib.request.urlopen(  # noqa: S310 - authenticated GitHub API call
            request,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            payload = json.load(response)
        return _require_mapping(payload, name=f"response from {url}")

    return _with_retry(_open_json_response, description=f"load JSON from {url}")


def _download_bytes(*, url: str, token: str) -> bytes:
    """Download raw bytes from an authenticated URL."""
    request = urllib.request.Request(  # noqa: S310 - URL is returned by the GitHub API
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "cuprum-benchmark-ratchet",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    def _open_archive() -> bytes:
        with urllib.request.urlopen(  # noqa: S310 - authenticated GitHub artifact download
            request,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            return response.read()

    return _with_retry(_open_archive, description=f"download archive from {url}")


def _find_artifact_url_in_run(
    *,
    artifacts_payload: typ.Mapping[str, object],
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
    workflow_runs_payload: typ.Mapping[str, object],
    artifacts_payload_by_run: typ.Mapping[int, typ.Mapping[str, object]],
    artifact_name: str,
) -> str | None:
    """Return the latest non-expired artifact download URL, if available."""
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
    """Return the normalized extraction path for an archive member."""
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
    """Extract a downloaded artifact zip into *output_dir* safely."""
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
    """Query GitHub Actions and return the latest matching artifact URL."""
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
    workflow_runs_payload = _with_retry(
        lambda: _load_json_response(url=workflow_runs_url, token=token),
        description=f"load workflow runs from {workflow_runs_url}",
    )
    workflow_runs = _require_list(
        workflow_runs_payload.get("workflow_runs"),
        name="workflow_runs",
    )

    artifacts_payload_by_run: dict[int, typ.Mapping[str, object]] = {}
    for index, run_value in enumerate(workflow_runs):
        run = _require_mapping(run_value, name=f"workflow_runs[{index}]")
        run_id = _require_int(run.get("id"), name=f"workflow_runs[{index}].id")
        artifacts_url = (
            f"{query.api_base_url}/repos/{encoded_repository}/actions/runs/"
            f"{run_id}/artifacts?per_page=100"
        )
        artifacts_payload_by_run[run_id] = _with_retry(
            lambda artifacts_url=artifacts_url: _load_json_response(
                url=artifacts_url,
                token=token,
            ),
            description=f"load artifacts for run {run_id}",
        )

    return select_latest_artifact_download_url(
        workflow_runs_payload=workflow_runs_payload,
        artifacts_payload_by_run=artifacts_payload_by_run,
        artifact_name=query.artifact_name,
    )


def _parse_args(argv: typ.Sequence[str] | None) -> argparse.Namespace:
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


def main(argv: typ.Sequence[str] | None = None) -> int:
    """Run the baseline artifact fetch CLI."""
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

    archive_bytes = _with_retry(
        lambda: _download_bytes(url=download_url, token=token),
        description=f"download benchmark baseline archive from {download_url}",
    )
    extract_artifact_archive(
        archive_bytes=archive_bytes,
        output_dir=args.output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
