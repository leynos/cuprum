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

import json
import typing as typ
from collections import Counter

from scripts.atomic_write import write_json, write_text
from scripts.pytest_layout_evidence import RunRecord

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

__all__ = ("EvidenceError", "fidelity", "load_records", "write_reports")

#: Bumped when a report's shape changes meaning, so an old artefact is not
#: compared against a new one.
SCHEMA_VERSION = 1


class EvidenceError(ValueError):
    """Evidence that cannot be interpreted.

    Parameters
    ----------
    reason : str
        What is wrong with the inputs.
    """

    def __init__(self, reason: str) -> None:
        """Record why the evidence was refused."""
        super().__init__(reason)
        self.reason = reason


def _missing_message(path: pth.Path) -> str:
    """Return the refusal text for a path that does not exist."""
    return f"{path} does not exist"


def _unreadable_message(path: pth.Path, detail: str) -> str:
    """Return the refusal text for a file that is not the JSON expected."""
    return f"{path} is not valid JSON: {detail}"


def _shape_message(path: pth.Path) -> str:
    """Return the refusal text for a document with neither a run nor a list."""
    return f"{path} holds neither a run nor a list of runs"


def _runs_message(path: pth.Path) -> str:
    """Return the refusal text for a ``runs`` key that is not a list."""
    return f"{path}: 'runs' must be a list"


def _entry_message(path: pth.Path, index: int) -> str:
    """Return the refusal text for a run entry that is not an object."""
    return f"{path}: run {index} is not an object"


def _missing_keys_message(path: pth.Path, index: int, missing: list[str]) -> str:
    """Return the refusal text for a run entry that omits a pairing key."""
    return f"{path}: run {index} is missing {missing}"


def _nodes_message(path: pth.Path, index: int) -> str:
    """Return the refusal text for node IDs that are not strings."""
    return f"{path}: run {index} node_ids must be a list of strings"


def _empty_side_message(side: str) -> str:
    """Return the refusal text for a side that collected no node IDs."""
    return f"the {side} side collected no node IDs"


def load_records(path: pth.Path) -> tuple[RunRecord, ...]:
    """Load run records from one JSON file.

    The accepted shape is deliberately small: either a single run object or an
    object with a ``runs`` array. Anything else is refused by name, because a
    silently ignored input would make a comparison look better-supported than
    it is.

    Parameters
    ----------
    path : pathlib.Path
        The JSON file to read.

    Returns
    -------
    tuple of RunRecord
        The runs it describes.

    Raises
    ------
    EvidenceError
        If the file is missing, is not JSON, or does not have the expected
        shape. A missing file is reported as such rather than as an empty
        comparison.
    """  # ruff: ignore[docstring-extraneous-exception] - EvidenceError propagates from the helpers.
    entries = _entries(path, _payload(path))
    return tuple(_record(path, index, entry) for index, entry in enumerate(entries))


def _payload(path: pth.Path) -> object:
    """Return one artefact's decoded JSON, naming the path on refusal.

    Parameters
    ----------
    path : pathlib.Path
        The file to read.

    Returns
    -------
    object
        The decoded document.

    Raises
    ------
    EvidenceError
        If the file is missing or is not JSON. A missing file is reported as
        such rather than as an empty comparison.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise EvidenceError(_missing_message(path)) from error
    except json.JSONDecodeError as error:
        raise EvidenceError(_unreadable_message(path, str(error))) from error


def _entries(path: pth.Path, payload: object) -> list[object]:
    """Return the run entries a document carries.

    A document may hold one run, a ``runs`` array, or a bare array. Anything
    else is refused by name: a silently ignored input would make a comparison
    look better-supported than it is.

    Parameters
    ----------
    path : pathlib.Path
        The artefact being read, named in the refusal.
    payload : object
        The decoded document.

    Returns
    -------
    list of object
        The run entries, still undecoded.

    Raises
    ------
    EvidenceError
        If the document holds neither a run nor a list of runs, or its
        ``runs`` value is not a list.
    """
    match payload:
        case {"runs": runs} if isinstance(runs, list):
            return runs
        case {"runs": _}:
            raise EvidenceError(_runs_message(path))
        case dict():
            return [payload]
        case list():
            return payload
        case _:
            raise EvidenceError(_shape_message(path))


def _record(path: pth.Path, index: int, entry: object) -> RunRecord:
    """Build one record, refusing an entry that omits a pairing key."""
    fields = _require_entry(path, index, entry)
    nodes = _require_nodes(path, index, fields)
    return RunRecord(
        run_id=typ.cast("str", fields["run_id"]),
        layout=typ.cast("str", fields["layout"]),
        python_version=typ.cast("str", fields["python_version"]),
        cache_state=typ.cast("str", fields["cache_state"]),
        step_seconds=_number(fields.get("step_seconds")),
        job_seconds=_number(fields.get("job_seconds")),
        node_ids=tuple(nodes),
        exit_status=int(typ.cast("int", fields.get("exit_status", 0))),
        failed=bool(fields.get("failed", False)),
    )


def _require_entry(path: pth.Path, index: int, entry: object) -> dict[str, object]:
    """Require one run entry to be an object carrying every pairing key.

    Parameters
    ----------
    path : pathlib.Path
        The artefact being read, named in the refusal.
    index : int
        The entry's position in the document.
    entry : object
        The decoded entry.

    Returns
    -------
    dict of str to object
        The same entry, narrowed to a mapping for the caller.

    Raises
    ------
    EvidenceError
        If the entry is not an object, or omits a key that pairing needs. A run
        without an interpreter or cache state cannot be paired, so accepting it
        would silently shrink the sample.
    """
    if not isinstance(entry, dict):
        raise EvidenceError(_entry_message(path, index))
    missing = [
        key
        for key in ("run_id", "layout", "python_version", "cache_state")
        if not isinstance(entry.get(key), str)
    ]
    if missing:
        raise EvidenceError(_missing_keys_message(path, index, missing))
    return entry


def _require_nodes(path: pth.Path, index: int, entry: dict[str, object]) -> list[str]:
    """Return the entry's node IDs, requiring a list of strings.

    Parameters
    ----------
    path : pathlib.Path
        The artefact being read, named in the refusal.
    index : int
        The entry's position in the document.
    entry : dict of str to object
        The decoded entry.

    Returns
    -------
    list of str
        The collected node IDs, empty when the run recorded none.

    Raises
    ------
    EvidenceError
        If ``node_ids`` is present but is not a list of strings.
    """
    nodes = entry.get("node_ids", [])
    if not isinstance(nodes, list) or not all(isinstance(n, str) for n in nodes):
        raise EvidenceError(_nodes_message(path, index))
    return nodes


def _number(value: object) -> float | None:
    """Return a finite float, or ``None`` when the value is absent or unusable."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


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
