"""The compiler-cache evidence vocabulary shared by its writer and reader.

One module owns the schema version, the allowlisted counter and duration names,
the closed vocabularies, and the pure classifiers both sides apply. A writer
that copied a counter the reader rejects, or a reader that admitted a label the
writer never emits, would make the evidence silently incomparable; keeping the
two on one vocabulary is what makes the round trip checkable.

Examples
--------
>>> is_decimal("42")
True
>>> backend_of('Local disk: "/home/runner/.cache/sccache"')
'local'
>>> backend_of("GitHub Actions Cache")
'gha'
>>> zero_requests_kind(0, expects_rustc=False, target_present=False)
'no-rustc-route'
"""

from __future__ import annotations

import datetime as dt
import re
import typing as typ
from urllib.parse import urlsplit

if typ.TYPE_CHECKING:
    import collections.abc as cabc

SCHEMA_VERSION = 1
RECORD_KIND = "compiler_cache_observation"
#: The only statistics counters copied out of sccache, by our own name.
COUNTERS: typ.Final = (
    "compile_requests",
    "requests_executed",
    "requests_unsupported_compiler",
    "requests_not_compile",
    "requests_not_cacheable",
    "cache_hits",
    "cache_misses",
    "cache_timeouts",
    "cache_read_errors",
    "cache_write_errors",
    "cache_writes",
    "compile_fails",
    "non_cacheable_compilations",
    "forced_recaches",
    "dist_errors",
)
#: Durations, in seconds, that sccache reports as ``{secs, nanos}``.
DURATIONS: typ.Final = (
    "cache_write_duration",
    "cache_read_hit_duration",
    "compiler_write_duration",
)
COMPILER_SHAPES = frozenset({
    "debug",
    "release",
    "lint",
    "coverage",
    "modern-stable",
    "loom",
    "unknown",
})
SETUP_STATUSES = frozenset({"started", "fallback", "absent"})
BACKENDS = frozenset({"local", "gha", "none", "unknown"})
CACHE_HOSTS = frozenset({"github-results", "github-legacy", "non-github", "absent"})
ZERO_KINDS = frozenset({"nonzero", "no-rustc-route", "fresh-target", "unexplained"})
EVENT_CLASSES = frozenset({"pull_request", "push", "schedule", "dispatch", "other"})
STATES = frozenset({"ok", "degraded", "noncompliant"})
HEX40 = re.compile(r"[0-9a-f]{40}")
DECIMAL = re.compile(r"[0-9]+")
BOUNDED = re.compile(r"[A-Za-z0-9._:/+-]{0,200}")
BRACKET = re.compile(r"[a-z][a-z0-9-]{0,30}")
PYTHON_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){0,2}")
WORKFLOW = re.compile(r"[A-Za-z0-9._-]+\.ya?ml")
REASON = re.compile(r"[a-z0-9_]{1,40}")


def is_decimal(value: str) -> bool:
    """Return whether ``value`` is a non-empty run of ASCII digits.

    Parameters
    ----------
    value : str
        The candidate text.

    Returns
    -------
    bool
        Whether the text is decimal, which excludes signs and whitespace.
    """
    return bool(DECIMAL.fullmatch(value))


def backend_of(location: object) -> str:
    """Classify sccache's reported cache location without recording it.

    Parameters
    ----------
    location : object
        The ``cache_location`` value from the statistics JSON.

    Returns
    -------
    str
        ``local``, ``gha``, ``none`` or ``unknown``. The raw value may carry a
        runner path or a service address, so it is never persisted.
    """
    text = str(location or "").lower()
    if not text:
        return "none"
    if "github actions cache" in text:
        return "gha"
    if text.startswith("local disk"):
        return "local"
    return "unknown"


def cache_host_class(url: object) -> str:
    """Classify the Actions cache URL's host without recording the URL.

    Parameters
    ----------
    url : object
        The value of ``ACTIONS_CACHE_URL``, or anything else.

    Returns
    -------
    str
        ``github-results`` for the v2 results service, ``github-legacy`` for
        the v1 cache service, ``non-github`` for a proxy on another host, or
        ``absent`` when nothing usable is set.
    """
    text = str(url or "")
    if not text:
        return "absent"
    host = (urlsplit(text).hostname or "").lower()
    if not host:
        return "absent"
    if not host.endswith("actions.githubusercontent.com"):
        return "non-github"
    # Both GitHub services live under the same parent: the v2 results receiver
    # is a distinct prefix, and the v1 cache service is the bare host.
    return "github-results" if host.startswith("results-receiver") else "github-legacy"


def event_class(name: object) -> str:
    """Map a GitHub event name onto the experiment's closed vocabulary."""
    text = str(name or "")
    if text in {"pull_request", "push", "schedule"}:
        return text
    if text in {"workflow_dispatch", "repository_dispatch"}:
        return "dispatch"
    return "other"


def workflow_name(reference: object) -> str:
    """Return the workflow file name from ``GITHUB_WORKFLOW_REF``.

    Parameters
    ----------
    reference : object
        A value such as ``owner/repo/.github/workflows/ci.yml@refs/heads/main``.

    Returns
    -------
    str
        The file name when it is a workflow path, else ``unknown``. The rest of
        the reference, which can name a branch, is never recorded.
    """
    path = str(reference or "").partition("@")[0]
    name = path.rsplit("/", 1)[-1]
    return name if WORKFLOW.fullmatch(name) else "unknown"


def zero_requests_kind(
    requests: int | None, *, expects_rustc: bool | None, target_present: bool | None
) -> str:
    """Classify a bracket's request count against its declared expectation.

    Parameters
    ----------
    requests : int | None
        The bracket's ``compile_requests`` delta, or ``None`` when unknown.
    expects_rustc : bool | None
        Whether the bracket should invoke rustc.
    target_present : bool | None
        Whether a Cargo ``target`` directory already existed when the bracket
        opened, which can make Cargo legitimately compile nothing.

    Returns
    -------
    str
        ``nonzero`` for any positive count, ``no-rustc-route`` when the route
        declares none, ``fresh-target`` when an existing target explains the
        zero, and ``unexplained`` otherwise.
    """
    if requests is None:
        return "unexplained"
    if requests > 0:
        return "nonzero"
    if expects_rustc is False:
        return "no-rustc-route"
    if target_present is True:
        return "fresh-target"
    return "unexplained"


def bounded(value: object, pattern: re.Pattern[str] = BOUNDED) -> str:
    """Return ``value`` when it matches ``pattern``, else ``unknown``."""
    text = str(value or "")
    return text if pattern.fullmatch(text) else "unknown"


def sha(value: object) -> str:
    """Return a 40-character lowercase hex SHA, or ``unknown``."""
    text = str(value or "").lower()
    return text if HEX40.fullmatch(text) else "unknown"


def boolean(value: object) -> bool | None:
    """Read a declared tri-state boolean from its environment spelling."""
    text = str(value or "").lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def delta_value(post: object, pre: object) -> int | float | None:
    """Subtract two allowlisted counters, preserving ``unknown``."""
    # `bool` must be matched before `int`: Python's `True` is an `int`, and a
    # boolean in a counter field means the document is malformed, not that the
    # bracket compiled one unit of work.
    match post, pre:
        case bool(), _:
            return None
        case _, bool():
            return None
        case int(), int():
            return post - pre
        case float(), int() | float():
            return round(post - float(pre), 6)
        case _:
            return None


def delta_map(
    post: cabc.Mapping[str, object], pre: cabc.Mapping[str, object]
) -> dict[str, int | float | None]:
    """Subtract every key in ``post`` from the same key in ``pre``."""
    return {name: delta_value(value, pre.get(name)) for name, value in post.items()}


def now() -> str:
    """Return the current UTC time in whole-second ISO 8601 with ``Z``."""
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
