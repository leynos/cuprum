"""Load and compare the JSON evidence a pytest layout run produces.

This module owns the parts of issue #607's analysis that touch files and sets:
reading run records and reporting the difference between two layouts' collected
node IDs. Pairing and judging runs live beside the records they judge, in
`scripts.pytest_layout_evidence`, so each module stays under the repository's
size limit without splitting one claim across two files.

``fidelity`` reports the difference in both directions. A node only the control
collected and a node only the candidate collected are separate lists, because
"the layouts differ" would hide which way the suite moved.
"""

from __future__ import annotations

import typing as typ
from collections import Counter

from scripts.atomic_write import write_json, write_text
from scripts.pytest_layout_loader import (
    SCHEMA_VERSION,
    EvidenceError,
    load_records,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

    from scripts.pytest_layout_records import RunRecord

__all__ = ("EvidenceError", "fidelity", "load_records", "write_reports")


def fidelity(
    control: cabc.Sequence[RunRecord],
    candidate: cabc.Sequence[RunRecord],
) -> dict[str, object]:
    """Compare two layouts' collected node IDs.

    Parameters
    ----------
    control : Sequence of RunRecord
        Runs of the baseline layout.
    candidate : Sequence of RunRecord
        Runs of the candidate layout.

    Returns
    -------
    dict
        A report with the node IDs only the control collected, only the
        candidate collected, the shared count, and per-layout skip and xfail
        totals. An empty control is reported as an error rather than as a
        vacuous match.

    Raises
    ------
    EvidenceError
        If either side collected nothing, since a comparison against an empty
        set would report every difference as an addition or a loss depending on
        which side was empty.
    """  # ruff: ignore[docstring-extraneous-exception] - EvidenceError propagates from _node_ids.
    control_nodes = _node_ids(control, "control")
    candidate_nodes = _node_ids(candidate, "candidate")
    return _fidelity_report(control, candidate, control_nodes, candidate_nodes)


def _empty_side_message(side: str) -> str:
    """Return the refusal text for a side that collected no node IDs."""
    return f"the {side} side collected no node IDs"


def _node_ids(runs: cabc.Sequence[RunRecord], side: str) -> Counter[str]:
    """Return one side's collected node IDs, refusing an empty collection.

    Parameters
    ----------
    runs : Sequence of RunRecord
        That side's runs.
    side : str
        ``control`` or ``candidate``, used in the refusal text.

    Returns
    -------
    collections.Counter of str
        Every node ID that side collected, with its multiplicity. A multiset
        is what detects a module collected twice, which a set would erase.

    Raises
    ------
    EvidenceError
        If the side collected nothing. A comparison against an empty set would
        report every difference as an addition or a loss depending on which
        side was empty, which reads as a result rather than as a failure.
    """
    nodes = Counter(node for run in runs for node in run.node_ids)
    if not nodes:
        raise EvidenceError(_empty_side_message(side))
    return nodes


def _fidelity_report(
    control: cabc.Sequence[RunRecord],
    candidate: cabc.Sequence[RunRecord],
    control_nodes: Counter[str],
    candidate_nodes: Counter[str],
) -> dict[str, object]:
    """Assemble the fidelity report from both sides' node multisets."""
    return {
        "schema_version": SCHEMA_VERSION,
        "control_runs": [run.run_id for run in control],
        "candidate_runs": [run.run_id for run in candidate],
        "control_only": sorted((control_nodes - candidate_nodes).elements()),
        "candidate_only": sorted((candidate_nodes - control_nodes).elements()),
        "shared": sum((control_nodes & candidate_nodes).values()),
        "control_total": sum(control_nodes.values()),
        "candidate_total": sum(candidate_nodes.values()),
        "control_duplicates": sorted(
            node for node, count in control_nodes.items() if count > 1
        ),
        "candidate_duplicates": sorted(
            node for node, count in candidate_nodes.items() if count > 1
        ),
        "identical": control_nodes == candidate_nodes,
    }


def write_reports(destination: pth.Path, report: cabc.Mapping[str, object]) -> None:
    """Write a report as both JSON and a compact CSV summary.

    Parameters
    ----------
    destination : pathlib.Path
        Directory to write into. Created if absent.
    report : collections.abc.Mapping of str to object
        The report to persist. The CSV carries the scalar fields only, because
        a CSV of nested lists is not reviewable.
    """
    destination.mkdir(parents=True, exist_ok=True)
    write_json(destination / "report.json", report)
    rows = ["field,value"]
    rows.extend(
        f"{key},{value}"
        for key, value in report.items()
        if isinstance(value, str | int | float | bool | type(None))
    )
    write_text(destination / "report.csv", "\n".join(rows) + "\n")
