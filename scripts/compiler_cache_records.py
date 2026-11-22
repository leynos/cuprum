"""Validate compiler-cache evidence and join it to GitHub's jobs API.

The writer runs inside a paid job and fails open; this reader runs offline and
fails closed. A malformed artefact must not be able to flatten into a plausible
summary, so every record is checked against the exact schema version, the
closed vocabularies, and the allowlisted counter and duration names. Missing
values stay ``None`` (unknown); the reader never substitutes zero.

The jobs API does not expose the workflow's job key, only its display name, so
the join to a record's ``job`` is declared through a mapping whose value may
contain a ``{leg}`` placeholder. A record the mapping cannot place keeps an
``unknown`` result and contributes no whole-job seconds rather than a guess.

Examples
--------
>>> integer(1)
True
>>> integer(True)
False
>>> job_result({"conclusion": "success"})
'passed'
"""

from __future__ import annotations

import json
import math
import re
import typing as typ

from scripts import compiler_cache_schema as schema

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

RECORD_KEYS = frozenset({
    "schema_version",
    "record_kind",
    "bracket",
    "run_id",
    "run_attempt",
    "job",
    "workflow",
    "event_class",
    "pr_number",
    "recorded_at",
    "setup_status",
    "backend",
    "wrapper_present",
    "cache_service_v2",
    "cache_host_class",
    "sccache_version",
    "rustc",
    "family",
    "matrix_leg",
    "compiler_shape",
    "python_version",
    "toolchain",
    "dev_fast",
    "checkout_sha",
    "head_sha",
    "base_sha",
    "matched_key",
    "restore_hit",
    "restore_seconds",
    "restore_bytes",
    "save_seconds",
    "save_bytes",
    "counters",
    "durations",
    "not_cached",
    "zero_requests",
    "state",
})
RUSTC_KEYS = frozenset({"release", "commit", "host"})
CONSOLE_KEYS = frozenset({
    "schema_version",
    "observed_at",
    "branch_protection",
    "storage_gb",
    "eviction_days",
    "billing_rounding",
    "initial_population_seconds",
    "incremental_transfer_seconds",
    "incremental_cost_seconds",
    "note",
})


class InvalidEvidenceError(ValueError):
    """Raised when an input document falls outside the evidence contract."""


def _require(*, condition: bool, message: str) -> None:
    """Raise :class:`InvalidEvidenceError` when ``condition`` is false."""
    if not condition:
        raise InvalidEvidenceError(message)


def integer(value: object) -> bool:
    """Report whether ``value`` is an integer and not a boolean."""
    return isinstance(value, int) and not isinstance(value, bool)


def number(value: object) -> bool:
    """Report whether ``value`` is a finite int or float and not a boolean."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def number_or_none(value: object) -> float | None:
    """Return ``value`` as a float when it is a number, else ``None``."""
    return float(typ.cast("float", value)) if number(value) else None


def _mapping(value: object, keys: frozenset[str], where: str) -> dict:
    """Require exactly ``keys`` and return the mapping."""
    _require(condition=isinstance(value, dict), message=f"{where} must be an object")
    mapping = typ.cast("dict[str, object]", value)
    _require(
        condition=set(mapping) == keys,
        message=f"{where} must have exactly {sorted(keys)}",
    )
    return mapping


IDENTITY_STRINGS = (
    "bracket",
    "run_id",
    "run_attempt",
    "job",
    "workflow",
    "event_class",
    "pr_number",
    "recorded_at",
    "setup_status",
    "backend",
    "cache_host_class",
    "sccache_version",
    "family",
    "matrix_leg",
    "compiler_shape",
    "python_version",
    "toolchain",
    "checkout_sha",
    "head_sha",
    "base_sha",
    "matched_key",
    "zero_requests",
    "state",
)
#: Fields that may carry the writer's `unknown` sentinel instead of a value.
_UNKNOWN_OK = frozenset({
    "workflow",
    "sccache_version",
    "family",
    "matrix_leg",
    "python_version",
    "toolchain",
    "checkout_sha",
    "head_sha",
    "base_sha",
    "matched_key",
})


def _run_identity(record: dict[str, object]) -> None:
    """Require the run identity fields to be decimal strings."""
    for name in ("run_id", "run_attempt"):
        value = record[name]
        _require(
            condition=isinstance(value, str) and value.isascii() and value.isdecimal(),
            message=f"{name} must be a decimal string",
        )
    _require(
        condition=type(record["schema_version"]) is int,
        message="schema_version must be an integer",
    )


def _strings(record: dict[str, object]) -> None:
    """Require every identity field to be a string of its declared shape."""
    for name in IDENTITY_STRINGS:
        _require(
            condition=isinstance(record[name], str),
            message=f"{name} must be a string",
        )
    _require(
        condition=schema.BRACKET.fullmatch(str(record["bracket"])) is not None,
        message="bracket must be a lowercase identifier",
    )
    _require(
        condition=str(record["pr_number"]) in {"", "unknown"}
        or str(record["pr_number"]).isdecimal(),
        message="pr_number must be decimal, empty, or unknown",
    )
    timestamp = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
    _require(
        condition=timestamp.fullmatch(str(record["recorded_at"])) is not None,
        message="recorded_at must be a UTC ISO 8601 timestamp",
    )
    for name in ("checkout_sha", "head_sha", "base_sha"):
        value = str(record[name])
        _require(
            condition=value == "unknown" or schema.HEX40.fullmatch(value) is not None,
            message=f"{name} must be a lowercase 40-character SHA or unknown",
        )
    for name in _UNKNOWN_OK:
        value = str(record[name])
        _require(
            condition=value == "unknown" or schema.BOUNDED.fullmatch(value) is not None,
            message=f"{name} must be a bounded identifier or unknown",
        )


def _labels(record: dict[str, object]) -> None:
    """Require every closed-vocabulary label to be inside its vocabulary."""
    _require(
        condition=record["schema_version"] == schema.SCHEMA_VERSION,
        message="unsupported schema version",
    )
    _require(
        condition=record["record_kind"] == schema.RECORD_KIND,
        message="wrong record kind",
    )
    vocabularies = (
        ("event_class", schema.EVENT_CLASSES),
        ("setup_status", schema.SETUP_STATUSES),
        ("backend", schema.BACKENDS),
        ("cache_host_class", schema.CACHE_HOSTS),
        ("compiler_shape", schema.COMPILER_SHAPES),
        ("zero_requests", schema.ZERO_KINDS),
        ("state", schema.STATES),
    )
    for name, vocabulary in vocabularies:
        _require(
            condition=record[name] in vocabulary,
            message=f"{name} is outside its vocabulary: {record[name]!r}",
        )


def _flags(record: dict[str, object]) -> None:
    """Require booleans, tri-state booleans, and numeric timings."""
    for name in ("wrapper_present", "cache_service_v2"):
        _require(condition=isinstance(record[name], bool), message=name)
    for name in ("dev_fast", "restore_hit"):
        _require(
            condition=record[name] is None or isinstance(record[name], bool),
            message=f"{name} must be a bool or null",
        )
    for name in ("restore_seconds", "save_seconds"):
        _require(
            condition=record[name] is None or number(record[name]),
            message=f"{name} must be a number or null",
        )
    for name in ("restore_bytes", "save_bytes"):
        _require(
            condition=record[name] is None or integer(record[name]),
            message=f"{name} must be an integer or null",
        )
    rustc = _mapping(record["rustc"], RUSTC_KEYS, "rustc")
    _require(
        condition=all(isinstance(value, str) for value in rustc.values()),
        message="rustc values must be strings",
    )


def _nested(record: dict[str, object]) -> None:
    """Validate the counters, durations, and non-cacheable reasons."""
    counters = _mapping(record["counters"], frozenset(schema.COUNTERS), "counters")
    for name, value in counters.items():
        _require(
            condition=value is None or integer(value),
            message=f"counter {name} must be an integer or null",
        )
    durations = _mapping(record["durations"], frozenset(schema.DURATIONS), "durations")
    for name, value in durations.items():
        _require(
            condition=value is None or number(value),
            message=f"duration {name} must be a number or null",
        )
    not_cached = record["not_cached"]
    _require(
        condition=isinstance(not_cached, dict),
        message="not_cached must be an object",
    )
    _require(
        condition=all(
            integer(value) for value in typ.cast("dict", not_cached).values()
        ),
        message="not_cached values must be integers",
    )


def validate_record(record: object) -> dict[str, object]:
    """Validate one record against the versioned evidence schema.

    Parameters
    ----------
    record : object
        A parsed JSON line.

    Returns
    -------
    dict[str, object]
        The record, unchanged, as a mapping.

    Notes
    -----
    Fails the contract, through :func:`_require`, when a key is missing or
    extra, a type is wrong, a closed vocabulary is violated, or a counter is
    neither an integer nor ``null``.
    """
    mapping = _mapping(record, RECORD_KEYS, "record")
    _run_identity(mapping)
    _strings(mapping)
    _labels(mapping)
    _flags(mapping)
    _nested(mapping)
    return mapping


def load_records(paths: cabc.Iterable[Path]) -> list[dict[str, object]]:
    """Load, validate, and deduplicate every record under ``paths``.

    Parameters
    ----------
    paths : collections.abc.Iterable[pathlib.Path]
        Files or directories. A directory is searched recursively for
        ``*.jsonl``.

    Returns
    -------
    list[dict[str, object]]
        Records ordered by ``recorded_at``.

    Raises
    ------
    InvalidEvidenceError
        When a line is not the declared schema, or a
        ``(run_id, run_attempt, job, bracket, matrix_leg)`` repeats.
    """
    files: list[Path] = []
    for path in paths:
        files.extend(sorted(path.rglob("*.jsonl")) if path.is_dir() else [path])
    seen: dict[tuple[str, str, str, str, str], dict[str, object]] = {}
    for file in files:
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            message = f"{file} could not be read: {error}"
            raise InvalidEvidenceError(message) from error
        for number_, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                document = json.loads(line)
            except json.JSONDecodeError as error:
                message = f"{file}:{number_} is not JSON: {error}"
                raise InvalidEvidenceError(message) from error
            record = validate_record(document)
            key = (
                str(record["run_id"]),
                str(record["run_attempt"]),
                str(record["job"]),
                str(record["bracket"]),
                str(record["matrix_leg"]),
            )
            _require(
                condition=key not in seen,
                message=f"{file}:{number_} duplicates {key}",
            )
            seen[key] = record
    return sorted(seen.values(), key=lambda item: str(item["recorded_at"]))


def load_console(path: Path | None) -> dict[str, object] | None:
    """Load and validate the dated console-observation document."""
    if path is None:
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        message = f"{path} could not be read: {error}"
        raise InvalidEvidenceError(message) from error
    mapping = _mapping(document, CONSOLE_KEYS, "console observation")
    _require(
        condition=mapping["schema_version"] == schema.SCHEMA_VERSION,
        message="unsupported console schema version",
    )
    _require(
        condition=mapping["branch_protection"] in {"enabled", "disabled", "unknown"},
        message="branch_protection must be enabled, disabled, or unknown",
    )
    _require(
        condition=mapping["billing_rounding"] in {"verified", "unknown"},
        message="billing_rounding must be verified or unknown",
    )
    return mapping


def load_jobs(path: Path | None) -> list[dict[str, object]]:
    """Load a jobs document, accepting either one response or a list of them."""
    if path is None:
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        message = f"{path} could not be read: {error}"
        raise InvalidEvidenceError(message) from error
    documents = document if isinstance(document, list) else [document]
    jobs: list[dict[str, object]] = []
    for item in documents:
        _require(
            condition=isinstance(item, dict),
            message="a jobs document must be an object",
        )
        values = typ.cast("dict[str, object]", item).get("jobs")
        _require(
            condition=isinstance(values, list),
            message="a jobs document must hold a jobs list",
        )
        jobs.extend(
            typ.cast("dict[str, object]", job)
            for job in typ.cast("list[object]", values)
            if isinstance(job, dict)
        )
    return jobs


def load_names(path: Path | None) -> dict[str, str]:
    """Load the declared job-key to display-name mapping."""
    if path is None:
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        message = f"{path} could not be read: {error}"
        raise InvalidEvidenceError(message) from error
    _require(
        condition=isinstance(document, dict),
        message="job names must be an object",
    )
    return {
        str(key): str(value)
        for key, value in typ.cast("dict[str, object]", document).items()
    }
