"""Load compiler-cache evidence files and GitHub API documents.

The validator in ``scripts.compiler_cache_validation`` owns the record schema.
This module owns the filesystem and JSON boundaries around it: reading a JSONL
artefact, a console observation, a jobs response, or the declared job-name map,
and turning each into the validated shape the summary consumes.

A names the mapping omits, a malformed line, a duplicate record, or an
unreadable file fails loudly. The reader is the fail-closed half of the
experiment; only the writer in a paid job fails open.
"""

from __future__ import annotations

import json
import typing as typ

from scripts import compiler_cache_schema as schema
from scripts.compiler_cache_validation import (
    CONSOLE_KEYS,
    InvalidEvidenceError,
    number,
    require,
    require_mapping,
    validate_record,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


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
            require(
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
    mapping = require_mapping(document, CONSOLE_KEYS, "console observation")
    require(
        condition=type(mapping["schema_version"]) is int,
        message="console schema_version must be an integer",
    )
    require(
        condition=mapping["schema_version"] == schema.SCHEMA_VERSION,
        message="unsupported console schema version",
    )
    require(
        condition=mapping["branch_protection"] in {"enabled", "disabled", "unknown"},
        message="branch_protection must be enabled, disabled, or unknown",
    )
    require(
        condition=mapping["billing_rounding"] in {"verified", "unknown"},
        message="billing_rounding must be verified or unknown",
    )
    require(
        condition=mapping["test_fidelity"] in {"matched", "mismatch", "unknown"},
        message="test_fidelity must be matched, mismatch, or unknown",
    )
    for name in (
        "storage_gb",
        "eviction_days",
        "initial_population_seconds",
        "incremental_transfer_seconds",
        "incremental_cost_seconds",
    ):
        value = mapping[name]
        require(
            condition=value is None or number(value),
            message=f"{name} must be a finite number or null",
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
        require(
            condition=isinstance(item, dict),
            message="a jobs document must be an object",
        )
        values = typ.cast("dict[str, object]", item).get("jobs")
        require(
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
    require(
        condition=isinstance(document, dict),
        message="job names must be an object",
    )
    names = typ.cast("dict[str, object]", document)
    require(
        condition=all(isinstance(value, str) for value in names.values()),
        message="job-name values must be strings",
    )
    return {str(key): typ.cast("str", value) for key, value in names.items()}
