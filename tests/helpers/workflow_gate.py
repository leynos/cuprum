"""Pure models for the benchmark path gate."""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def matches_filter(pattern: str, path: str) -> bool:
    """Return whether a changed ``path`` matches a declared filter ``pattern``.

    Parameters
    ----------
    pattern : str
        Literal path or ``dir/**`` prefix declared by the filter.
    path : str
        Changed path to compare with ``pattern``.

    Returns
    -------
    bool
        Whether the path matches the declared pattern.
    """
    if pattern.endswith("/**"):
        return path.startswith(pattern.removesuffix("**"))
    return path == pattern


def bench_output(
    changed_paths: cabc.Collection[str], filter_patterns: cabc.Collection[str]
) -> bool:
    """Model the ``bench`` output the filter produces for changed paths.

    Parameters
    ----------
    changed_paths : collections.abc.Collection[str]
        Paths changed by the event under test.
    filter_patterns : collections.abc.Collection[str]
        Path patterns declared by the ``bench`` filter.

    Returns
    -------
    bool
        Whether at least one changed path matches at least one filter pattern.
    """
    return any(
        matches_filter(pattern, path)
        for pattern in filter_patterns
        for path in changed_paths
    )


def benchmark_runs(*, event_name: str, bench: bool, detector_succeeded: bool) -> bool:
    """Model whether ``benchmark-ratchet`` runs for a gate input.

    Parameters
    ----------
    event_name : str
        GitHub event name that triggered the workflow.
    bench : bool
        Whether the path filter reported benchmark-relevant changes.
    detector_succeeded : bool
        Whether the change detector completed successfully.

    Returns
    -------
    bool
        Whether the benchmark job should run for the event and filter verdict.
    """
    return detector_succeeded and (event_name != "pull_request" or bench)
