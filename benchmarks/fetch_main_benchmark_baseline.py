"""Download the latest successful `main` benchmark baseline artefact."""

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

from benchmarks._validation import (
    _require_list,
    _require_mapping,
    _require_non_empty_string,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_TOKEN_ENV_VAR = "GITHUB_TOKEN"  # noqa: S105 - env var name, not a credential
MAIN_BASELINE_NOT_FOUND_EXIT_CODE = 3
_REQUEST_TIMEOUT_SECONDS = 10.0
_RETRY_DELAYS_SECONDS = (0.5, 1.0)
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR_MIN = 500
_HTTP_SERVER_ERROR_MAX = 600
_GITHUB_REDIRECT_HEADERS_TO_STRIP = (
    "Authorization",
    "X-github-api-version",
)


@dc.dataclass(frozen=True, slots=True)
class ArtefactQuery:
    """GitHub Actions workflow artefact lookup configuration.

    Attributes
    ----------
    repository
        GitHub repository in ``owner/name`` form.
    workflow
        Workflow file name or workflow identifier to query.
    branch
        Branch whose successful workflow runs are considered.
    event
        Workflow event used to filter successful runs.
    artefact_name
        Name of the workflow artefact to locate.
    api_base_url
        Base URL for GitHub API requests.
    """

    repository: str
    workflow: str
    branch: str
    event: str
    artefact_name: str
    api_base_url: str = GITHUB_API_BASE_URL


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
    operation: cabc.Callable[[], T],
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


class _ArtefactArchiveRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strip GitHub-only headers when following cross-origin archive redirects."""

    def _strip_cross_origin_headers(  # noqa: PLR6301
        self,
        req: urllib.request.Request,
        redirected_request: urllib.request.Request,
    ) -> None:
        """Strip sensitive headers when a redirect crosses host boundaries."""
        source_parts = urllib.parse.urlsplit(req.full_url)
        destination_parts = urllib.parse.urlsplit(redirected_request.full_url)
        source_origin = (source_parts.scheme, source_parts.netloc)
        destination_origin = (destination_parts.scheme, destination_parts.netloc)
        if source_origin == destination_origin:
            return
        for header in _GITHUB_REDIRECT_HEADERS_TO_STRIP:
            redirected_request.remove_header(header)

    def redirect_request(
        self,
        req: urllib.request.Request,
        *args: object,
        **kwargs: object,
    ) -> urllib.request.Request | None:
        redirected_request = super().redirect_request(req, *args, **kwargs)  # type: ignore[arg-type]
        if redirected_request is None:
            return None
        self._strip_cross_origin_headers(req, redirected_request)
        return redirected_request


def _load_json_response(*, url: str, token: str) -> cabc.Mapping[str, object]:
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

    def _open_json_response() -> cabc.Mapping[str, object]:
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
    opener = urllib.request.build_opener(_ArtefactArchiveRedirectHandler())

    def _open_archive() -> bytes:
        with opener.open(
            request,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            return response.read()

    return _with_retry(_open_archive, description=f"download archive from {url}")


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
    """Select the latest non-expired artefact download URL.

    Parameters
    ----------
    workflow_runs_payload
        GitHub workflow-runs response containing runs in newest-first order.
    artefacts_payload_by_run
        GitHub artefact responses keyed by workflow-run identifier.
    artefact_name
        Name of the artefact to select.

    Returns
    -------
    str or None
        Download URL for the first matching, non-expired artefact in workflow
        run order, or ``None`` when no supplied run contains one.

    Raises
    ------
    TypeError
        If a response field has an unexpected type.
    ValueError
        If a required response string is empty.
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
    """Extract a downloaded artefact zip safely.

    Parameters
    ----------
    archive_bytes
        Bytes containing the downloaded zip archive.
    output_dir
        Directory that receives the extracted regular files.

    Returns
    -------
    tuple[pathlib.Path, ...]
        Extracted file paths in archive-member order. Directory entries are
        omitted.

    Raises
    ------
    OSError
        If a destination directory or extracted file cannot be written.
    ValueError
        If an archive member would escape ``output_dir``.
    zipfile.BadZipFile
        If ``archive_bytes`` does not contain a valid zip archive.
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
    """Query GitHub Actions for the latest matching artefact URL.

    Parameters
    ----------
    query
        Repository, workflow, run filters, artefact name, and API base URL.
    token
        GitHub token used to authenticate API requests.

    Returns
    -------
    str or None
        Download URL for the latest matching, non-expired artefact, or ``None``
        when no successful run contains one.

    Raises
    ------
    json.JSONDecodeError
        If GitHub returns malformed JSON.
    TypeError
        If a GitHub response field has an unexpected type.
    urllib.error.URLError
        If a GitHub request fails after the bounded retries.
    ValueError
        If a required GitHub response string is empty.
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
        dest="artefact_name",
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
    argv
        Command-line arguments without the executable name. ``None`` reads
        arguments from :data:`sys.argv` through :mod:`argparse`.

    Returns
    -------
    int
        Zero after extracting an artefact, or
        :data:`MAIN_BASELINE_NOT_FOUND_EXIT_CODE` when no matching artefact
        exists.

    Raises
    ------
    SystemExit
        If arguments are invalid or the configured token environment variable
        is empty.
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
