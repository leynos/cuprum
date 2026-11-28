"""Assemble one compiler-cache record from an observation bundle.

The writer's edge reads snapshots, markers, and probes; this module owns the
pure part. ``build_record`` receives an ``ObservationBundle`` and performs no
process or filesystem read, so a test can supply fixed observations and a
record's shape is decided by data alone. The ok/degraded/noncompliant policy
lives in ``scripts.compiler_cache_schema`` with the rest of the vocabulary.
"""

from __future__ import annotations

import typing as typ

from scripts import compiler_cache_schema as schema

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def identity_fields(
    environ: cabc.Mapping[str, str],
    bracket: str,
    identity: tuple[str, str, str],
    post: cabc.Mapping[str, object],
) -> dict[str, object]:
    """Return the record's identity and provenance fields."""
    run_id, run_attempt, job = identity
    return {
        "schema_version": schema.SCHEMA_VERSION,
        "record_kind": schema.RECORD_KIND,
        "bracket": bracket,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "job": job,
        "workflow": schema.workflow_name(environ.get("GITHUB_WORKFLOW_REF")),
        "event_class": schema.event_class(environ.get("GITHUB_EVENT_NAME")),
        "pr_number": schema.bounded(environ.get("EVIDENCE_PR_NUMBER"), schema.DECIMAL),
        "setup_status": schema.bounded(environ.get("EVIDENCE_SETUP_STATUS")),
        "backend": schema.bounded(post.get("cache_backend")),
        "selected_backend": schema.bounded(environ.get("EVIDENCE_SELECTED_BACKEND")),
        "wrapper_present": bool(environ.get("RUSTC_WRAPPER")),
        "cache_service_v2": bool(post.get("cache_service_v2")),
        "cache_host_class": schema.bounded(post.get("cache_host_class")),
        "sccache_version": schema.bounded(post.get("sccache_version")),
        "family": schema.bounded(environ.get("EVIDENCE_FAMILY")),
        "runner_class": schema.bounded(environ.get("EVIDENCE_RUNNER_CLASS")),
        "matrix_leg": schema.bounded(environ.get("EVIDENCE_MATRIX_LEG")),
        "compiler_shape": schema.bounded(environ.get("EVIDENCE_COMPILER_SHAPE")),
        "python_version": schema.bounded(
            environ.get("EVIDENCE_PYTHON_VERSION"), schema.PYTHON_VERSION
        ),
        "toolchain": schema.bounded(environ.get("EVIDENCE_TOOLCHAIN")),
        "dev_fast": schema.boolean(environ.get("EVIDENCE_DEV_FAST")),
        "checkout_sha": schema.sha(environ.get("EVIDENCE_CHECKOUT_SHA")),
        "head_sha": schema.sha(environ.get("EVIDENCE_HEAD_SHA")),
        "base_sha": schema.sha(environ.get("EVIDENCE_BASE_SHA")),
        "matched_key": schema.bounded(environ.get("EVIDENCE_MATCHED_KEY")),
        "restore_hit": schema.boolean(environ.get("EVIDENCE_RESTORE_HIT")),
    }


def run_identity(environ: cabc.Mapping[str, str]) -> tuple[str, str, str]:
    """Return the run, attempt, and job, refusing a malformed identity."""
    run_id = environ.get("GITHUB_RUN_ID", "")
    run_attempt = environ.get("GITHUB_RUN_ATTEMPT", "")
    job = schema.bounded(environ.get("GITHUB_JOB"))
    if not schema.is_decimal(run_id) or not schema.is_decimal(run_attempt):
        msg = "invalid run identity"
        raise ValueError(msg)
    if job == "unknown":
        msg = "invalid job"
        raise ValueError(msg)
    return run_id, run_attempt, job


def not_cached_delta(
    pre: cabc.Mapping[str, object], post: cabc.Mapping[str, object]
) -> dict[str, int]:
    """Return per-reason deltas, treating an absent reason as zero."""
    if not pre.get("available") or not post.get("available"):
        return {}
    before = typ.cast("dict[str, object]", pre.get("not_cached", {}))
    after = typ.cast("dict[str, object]", post.get("not_cached", {}))
    deltas: dict[str, int] = {}
    for reason in set(before) | set(after):
        value = schema.delta_value(after.get(reason, 0), before.get(reason, 0))
        # A fallen reason means a restart; omit it rather than go negative.
        if value is not None:
            deltas[str(reason)] = int(value)
    return deltas


def observed_fields(bundle: ObservationBundle) -> dict[str, object]:
    """Return the counts, timings, and zero-request classification."""
    counters = schema.delta_map(
        typ.cast("dict[str, object]", bundle.post["counters"]),
        typ.cast("dict[str, object]", bundle.pre["counters"]),
    )
    requests = counters.get("compile_requests")
    return {
        "restore_seconds": bundle.pre.get("restore_seconds"),
        "restore_seconds_detail": bundle.pre.get("restore_seconds_detail", "unknown"),
        "restore_bytes": bundle.pre.get("restore_bytes"),
        "restore_bytes_detail": bundle.pre.get("restore_bytes_detail", "unknown"),
        "save_seconds": bundle.save_seconds,
        "save_seconds_detail": bundle.save_seconds_detail,
        "save_bytes": bundle.save_bytes,
        "save_bytes_detail": bundle.save_bytes_detail,
        "counters": counters,
        "durations": schema.delta_map(
            typ.cast("dict[str, object]", bundle.post["durations"]),
            typ.cast("dict[str, object]", bundle.pre["durations"]),
        ),
        "not_cached": not_cached_delta(bundle.pre, bundle.post),
        "zero_requests": schema.zero_requests_kind(
            requests if isinstance(requests, int) else None,
            expects_rustc=bundle.expects_rustc,
            target_present=bundle.target_present,
        ),
    }


class ObservationBundle(typ.NamedTuple):
    """The already-read values one record is assembled from."""

    fields: dict[str, object]
    pre: cabc.Mapping[str, object]
    post: cabc.Mapping[str, object]
    save_seconds: float | None
    save_seconds_detail: str
    save_bytes: int | None
    save_bytes_detail: str
    expects_rustc: bool | None
    target_present: bool | None


def build_record(bundle: ObservationBundle) -> dict[str, object]:
    """Assemble one record from an already-read observation bundle.

    Pure: it performs no process or filesystem read, and its only variation is
    the bundle it is given. ``gather_observation`` owns every read.

    Returns
    -------
    dict[str, object]
        The schema-version-1 record, ready to encode as one JSON line.
    """
    record = dict(bundle.fields)
    record.update(observed_fields(bundle))
    normalize(record)
    record["state"] = schema.record_state(record)
    return record


def normalize(record: dict[str, object]) -> None:
    """Replace an out-of-vocabulary label with its declared fallback."""
    fallbacks = {
        "setup_status": ("absent", schema.SETUP_STATUSES),
        "compiler_shape": ("unknown", schema.COMPILER_SHAPES),
        "backend": ("unknown", schema.BACKENDS),
        "selected_backend": ("unknown", schema.BACKENDS),
        "cache_host_class": ("absent", schema.CACHE_HOSTS),
    }
    for name, (fallback, vocabulary) in fallbacks.items():
        if record[name] not in vocabulary:
            record[name] = fallback
