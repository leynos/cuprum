#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Generate ``typos.toml`` from the shared en-GB-oxendict dictionary.

The shared dictionary is refreshed into an untracked repository-local cache
only when the authoritative copy is newer. A valid cache remains usable when
the network is unavailable, and ``typos.local.toml`` supplies the narrow
repository-specific policy that must not weaken the estate-wide base.
"""

import logging
import tomllib
import urllib.error
import urllib.parse
from pathlib import Path

import typos_rollout as rollout

DEFAULT_BASE_URL = (
    "https://raw.githubusercontent.com/leynos/agent-helper-scripts/"
    "refs/heads/main/data/typos-oxendict-base.toml"
)
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

_logger = logging.getLogger(__name__)


def dictionary_from_cache(repository: Path = REPOSITORY_ROOT) -> rollout.Dictionary:
    """Load the cached shared base merged with local repository policy.

    Parameters
    ----------
    repository : Path
        Repository containing the cached shared dictionary and optional local
        overlay. Defaults to the repository root.

    Returns
    -------
    rollout.Dictionary
        The shared base dictionary overlaid with any local policy.
    """
    dictionary = rollout.load_dictionary(repository / ".typos-oxendict-base.toml")
    local_overlay = repository / "typos.local.toml"
    if local_overlay.exists():
        dictionary = rollout.merge_dictionaries(
            dictionary,
            rollout.load_dictionary(local_overlay),
        )
    return dictionary


def render_config(repository: Path = REPOSITORY_ROOT) -> str:
    """Render deterministic configuration from the populated local cache.

    Parameters
    ----------
    repository : Path
        Repository containing the populated cache and optional local overlay.
        Defaults to the repository root.

    Returns
    -------
    str
        The rendered ``typos.toml`` configuration text.
    """
    return rollout.render_typos_config(dictionary_from_cache(repository))


def _tracked_remote_fallback(
    source: str | Path,
    destination: Path,
    error: OSError | urllib.error.URLError,
) -> rollout.RefreshResult | None:
    """Return a valid tracked config only for an unavailable HTTPS authority."""
    if not isinstance(source, str) or urllib.parse.urlsplit(source).scheme != "https":
        return None
    try:
        tomllib.loads(destination.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return None
    _logger.warning(
        "Falling back to the tracked typos configuration; the shared "
        "dictionary authority was unreachable",
        extra={
            "event": "typos_rollout.tracked_config_fallback",
            "error_type": type(error).__name__,
        },
    )
    return rollout.RefreshResult("tracked-config", destination)


def main(
    output: Path | None = None,
    *,
    repository: Path = REPOSITORY_ROOT,
    source: str | Path = DEFAULT_BASE_URL,
    offline: bool = False,
) -> rollout.RefreshResult:
    """Refresh the shared base cache and write the merged configuration.

    Parameters
    ----------
    output : Path | None
        Destination for the generated configuration. Defaults to
        ``repository / "typos.toml"``.
    repository : Path
        Repository containing the cache, local overlay, and default output.
    source : str | Path
        Authoritative shared dictionary source, as a local path or HTTPS URL.
    offline : bool
        When True, require an existing valid cache and skip network refresh.

    Returns
    -------
    rollout.RefreshResult
        The refresh outcome, or a tracked-config fallback when the source is
        unreachable but a valid tracked configuration is already present.

    Raises
    ------
    OSError
        If the cache cannot be refreshed and no tracked fallback is usable.
    urllib.error.URLError
        If the source cannot be reached and no tracked fallback is usable.
    ValueError
        If the shared dictionary source cannot be parsed while loading or
        merging.
    TypeError
        If the shared dictionary source is not a mapping while loading or
        merging.
    tomllib.TOMLDecodeError
        If the shared dictionary source or the rendered configuration is
        not valid TOML.
    """  # noqa: DOC502 - load, merge, and render errors propagate from the callees
    destination = output if output is not None else repository / "typos.toml"
    try:
        result = rollout.refresh_base(
            source,
            repository / ".typos-oxendict-base.toml",
            metadata=repository / ".typos-oxendict-base.json",
            offline=offline,
        )
    except (OSError, urllib.error.URLError) as error:
        fallback = _tracked_remote_fallback(source, destination, error)
        if fallback is not None:
            return fallback
        raise
    rollout.write_config(destination, dictionary_from_cache(repository))
    return result


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    refresh = main()
    print(f"{refresh.status}: {REPOSITORY_ROOT / 'typos.toml'}")
