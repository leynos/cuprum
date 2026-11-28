"""Translate produced session evidence into the records the analysis reads.

The pytest plugin and the offline analysis were written against different
shapes: the plugin writes ``collected``, ``outcomes`` and reason mappings, while
a run record needs ``node_ids`` and the workflow facts a session cannot know.
This module is the single place that translation happens, so the two shapes
cannot drift apart unnoticed.

``collected`` is read as a list, so a node a session collected twice stays
twice; that multiplicity is what the fidelity comparison uses to detect a
duplicate collection.
"""

from __future__ import annotations

import typing as typ

from scripts.pytest_layout_loader import (
    EvidenceError,
    _is_string_map,
    _session_field_message,
)
from scripts.pytest_layout_records import RunRecord

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("SessionIdentity", "run_record_from_session")


class SessionIdentity(typ.NamedTuple):
    """The workflow facts a session-evidence document does not record.

    Attributes
    ----------
    run_id : str
        The workflow run the session belongs to.
    layout : str
        ``baseline`` or ``consolidated``.
    python_version : str
        The interpreter leg.
    cache_state : str
        ``warm``, ``cold`` or ``scratch-cold``.
    step_seconds : float or None
        The `Run tests` step's wall time.
    job_seconds : float or None
        The whole job's wall time.
    runner_label : str or None
        The runner the job executed on.
    toolchain : str or None
        A fingerprint of the toolchain and lockfile.
    """

    run_id: str
    layout: str
    python_version: str
    cache_state: str
    step_seconds: float | None = None
    job_seconds: float | None = None
    runner_label: str | None = None
    toolchain: str | None = None


def run_record_from_session(
    session: cabc.Mapping[str, object],
    identity: SessionIdentity,
) -> RunRecord:
    """Build a run record from one session-evidence document.

    The plugin that produces these documents and the loader that reads them
    were written against different shapes: the plugin writes ``collected``,
    ``outcomes``, ``skip_reasons`` and ``xfail_reasons``, while a run record
    needs ``node_ids`` and the pairing keys. This adapter is the single place
    that translation happens, so the two cannot drift apart unnoticed.

    ``collected`` is read as a list, so a node a session collected twice stays
    twice. That multiplicity is what the fidelity comparison uses to detect a
    duplicate collection.

    Parameters
    ----------
    session : collections.abc.Mapping of str to object
        One decoded session-evidence document.
    identity : SessionIdentity
        The workflow facts a session document does not carry: which run it
        belongs to, which layout and interpreter leg produced it, the cache
        state, the job timings and the runner and toolchain fingerprints.

    Returns
    -------
    RunRecord
        The translated record.

    Raises
    ------
    EvidenceError
        If the session's collected or reason fields are not the shapes the
        plugin writes. Reporting the field is more useful than a later
        comparison that silently treats a mapping as empty.
    """
    collected = session.get("collected", [])
    if not isinstance(collected, list) or not all(
        isinstance(node, str) for node in collected
    ):
        raise EvidenceError(_session_field_message("collected"))
    return RunRecord(
        run_id=identity.run_id,
        layout=identity.layout,
        python_version=identity.python_version,
        cache_state=identity.cache_state,
        step_seconds=identity.step_seconds,
        job_seconds=identity.job_seconds,
        node_ids=tuple(collected),
        exit_status=int(typ.cast("int", session.get("exit_status", 0))),
        failed=int(typ.cast("int", session.get("exit_status", 0))) != 0,
        outcomes=_session_pairs(session, "outcomes"),
        skip_reasons=_session_pairs(session, "skip_reasons"),
        xfail_reasons=_session_pairs(session, "xfail_reasons"),
        runner_label=identity.runner_label,
        toolchain=identity.toolchain,
    )


def _session_pairs(
    session: cabc.Mapping[str, object], key: str
) -> tuple[tuple[str, str], ...]:
    """Return one session mapping field as sorted pairs."""
    raw = session.get(key, {})
    if not _is_string_map(raw):
        raise EvidenceError(_session_field_message(key))
    return tuple(sorted(typ.cast("dict[str, str]", raw).items()))
