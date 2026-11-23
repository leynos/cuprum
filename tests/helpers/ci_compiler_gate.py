"""Scripted observations for the compiler-cache gate contracts.

The gate tests need records with chosen timings, hits, and job results, and a
summary built through the real validator. Keeping the scripted shape here means
the arithmetic tests and the classification tests feed the reader the same
objects rather than two drifting fixtures.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from scripts import compiler_cache_records as records_reader
from scripts import compiler_cache_schema as schema
from scripts import compiler_cache_summary as summary_reader
from tests.helpers.ci_compiler_evidence import record_fixture

if typ.TYPE_CHECKING:
    import collections.abc as cabc

JOB_NAME = "Extension-gated tests"
NAMES: typ.Final = {"extension-tests": JOB_NAME}


@dc.dataclass(frozen=True, slots=True)
class Observation:
    """One scripted record with the fields the gate arithmetic reads."""

    run_id: str
    backend: str
    hits: int = 4
    restore: float | None = 20.0
    save: float | None = 20.0
    bracket: str = "extension"
    leg: str = "none"
    job: str = "extension-tests"
    state: str = "ok"
    restore_hit: bool | None = True
    pr_number: str = "42"
    event_class: str = "pull_request"
    toolchain: str = "nightly-2026-08-23"
    family: str = "py3.13-debug"

    def as_record(self) -> dict[str, object]:
        """Return the observation as a record the validator accepts."""
        counters = dict.fromkeys(schema.COUNTERS, None)
        counters["compile_requests"] = 5
        counters["cache_hits"] = self.hits
        return record_fixture(
            run_id=self.run_id,
            backend=self.backend,
            counters=counters,
            restore_hit=self.restore_hit,
            restore_seconds=self.restore,
            save_seconds=self.save,
            bracket=self.bracket,
            matrix_leg=self.leg,
            job=self.job,
            state=self.state,
            pr_number=self.pr_number,
            event_class=self.event_class,
            toolchain=self.toolchain,
            family=self.family,
        )


def jobs(
    run_ids: cabc.Iterable[int], seconds: float, *, conclusion: str = "success"
) -> list[dict[str, object]]:
    """Return one API job per run id, all lasting ``seconds``."""
    return [
        {
            "run_id": run,
            "run_attempt": 1,
            "name": JOB_NAME,
            "started_at": "2026-11-22T00:00:00Z",
            "completed_at": (
                f"2026-11-22T00:{int(seconds // 60):02d}:{int(seconds % 60):02d}Z"
            ),
            "conclusion": conclusion,
        }
        for run in run_ids
    ]


def console(**overrides: object) -> dict[str, object]:
    """Return a console observation with the gate fields set."""
    document: dict[str, object] = {
        "schema_version": schema.SCHEMA_VERSION,
        "observed_at": "2026-11-22",
        "branch_protection": "enabled",
        "storage_gb": 30,
        "eviction_days": 7,
        "billing_rounding": "unknown",
        "test_fidelity": "unknown",
        "initial_population_seconds": 60,
        "incremental_transfer_seconds": 5,
        "incremental_cost_seconds": 0,
        "note": "test",
    }
    document.update(overrides)
    return document


def summary(
    records: cabc.Sequence[Observation],
    job_documents: cabc.Sequence[dict[str, object]],
    console_document: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a summary from scripted observations through the real validator."""
    validated = [records_reader.validate_record(item.as_record()) for item in records]
    return summary_reader.build_summary(
        validated, job_documents, NAMES, console_document
    )


def family(built: cabc.Mapping[str, object], index: int = 0) -> dict[str, object]:
    """Return one family from a summary, narrowed for subscripting."""
    families = typ.cast("list[dict[str, object]]", built["families"])
    return families[index]


def decisions(built: cabc.Mapping[str, object]) -> list[dict[str, object]]:
    """Return the gate decisions from a summary, narrowed for subscripting."""
    gate = typ.cast("dict[str, object]", built["gate"])
    return typ.cast("list[dict[str, object]]", gate["decisions"])


def observations(built: cabc.Mapping[str, object]) -> list[dict[str, object]]:
    """Return the classified observations from a summary."""
    return typ.cast("list[dict[str, object]]", built["observations"])
