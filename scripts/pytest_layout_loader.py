"""Load and validate the JSON evidence a pytest layout comparison consumes.

Reading untrusted artefacts is one responsibility, so it lives in one module:
the analysis rules beside it judge records that this module has already
validated. Each refusal names the file and the entry, because an artefact that
cannot be read is the first thing a reviewer has to fix.
"""

from __future__ import annotations

import json
import typing as typ

from scripts.pytest_layout_records import SCHEMA_VERSION, RunRecord

if typ.TYPE_CHECKING:
    import pathlib as pth

__all__ = ("SCHEMA_VERSION", "EvidenceError", "load_records")


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
