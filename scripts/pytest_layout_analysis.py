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
        "control_outcomes": _outcomes(control),
        "candidate_outcomes": _outcomes(candidate),
        "control_skips": _node_map(control, "skip_reasons"),
        "candidate_skips": _node_map(candidate, "skip_reasons"),
        "control_xfails": _node_map(control, "xfail_reasons"),
        "candidate_xfails": _node_map(candidate, "xfail_reasons"),
        "identical": _same_suite(control, candidate, control_nodes, candidate_nodes),
    }


def _outcomes(runs: cabc.Sequence[RunRecord]) -> dict[str, str]:
    """Return one side's node outcomes, across its runs.

    Parameters
    ----------
    runs : Sequence of RunRecord
        That side's runs.

    Returns
    -------
    dict of str to str
        Node ID mapped to the outcome pytest reported for it.
    """
    return {node: value for run in runs for node, value in run.outcomes}


def _node_map(runs: cabc.Sequence[RunRecord], field: str) -> dict[str, str]:
    """Return one recorded node-to-reason mapping across a side's runs.

    Parameters
    ----------
    runs : Sequence of RunRecord
        That side's runs.
    field : str
        ``skip_reasons`` or ``xfail_reasons``.

    Returns
    -------
    dict of str to str
        Node ID mapped to its recorded reason.
    """
    return {node: value for run in runs for node, value in getattr(run, field)}


def _same_suite(
    control: cabc.Sequence[RunRecord],
    candidate: cabc.Sequence[RunRecord],
    control_nodes: Counter[str],
    candidate_nodes: Counter[str],
) -> bool:
    """Return whether two sides ran the same suite *and* reached the same results.

    Collected names alone are not fidelity. Two lanes can collect the same
    nodes and still disagree about what executed: a skip that became a pass, or
    an xfail that became a failure, is a change in the suite even when every
    name matches.

    Parameters
    ----------
    control : Sequence of RunRecord
        The control side's runs.
    candidate : Sequence of RunRecord
        The candidate side's runs.
    control_nodes : collections.Counter of str
        The control's collected node multiset.
    candidate_nodes : collections.Counter of str
        The candidate's collected node multiset.

    Returns
    -------
    bool
        Whether the multisets, outcomes, skips and xfails all agree.
    """
    return (
        control_nodes == candidate_nodes
        and _outcomes(control) == _outcomes(candidate)
        and _node_map(control, "skip_reasons") == _node_map(candidate, "skip_reasons")
        and _node_map(control, "xfail_reasons") == _node_map(candidate, "xfail_reasons")
    )


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
