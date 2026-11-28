"""Offline entry points for judging a pytest layout experiment.

Issue #607's evidence is produced by CI and analysed later, so the analysis
needs a command a reviewer can run against downloaded artefacts rather than a
function they have to call from a notebook. This module is that command.

Two subcommands mirror the two questions the experiment asks:

``fidelity`` answers "did the candidate run the same suite?" by comparing the
collected node multisets of a control set of records and a candidate set.
``compare`` answers "was it cheaper, by the predeclared rule?" by pairing the
same records and writing a verdict.

Both read plain JSON and write both JSON and a flat CSV, because a reviewer
should be able to diff the numbers and re-derive every one of them. Nothing
here touches the network: fetching run and job timestamps is a separate,
explicit step, and this tool only interprets what it is given.
"""

from __future__ import annotations

import json
import pathlib as pth
import sys
import typing as typ

from scripts.pytest_layout_analysis import (
    EvidenceError,
    fidelity,
    load_records,
    write_reports,
)
from scripts.pytest_layout_evidence import compare
from scripts.pytest_layout_session_adapter import (
    SessionIdentity,
    run_record_from_session,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from scripts.pytest_layout_records import RunRecord

__all__ = ("main",)

#: Exit status for a refused command line, matching the shell convention.
EXIT_USAGE = 2

#: Exit status for evidence that could not be interpreted.
EXIT_EVIDENCE = 1


def _flag(arguments: cabc.Sequence[str], name: str) -> str:
    """Return the value given for ``name``.

    Parameters
    ----------
    arguments : Sequence of str
        The subcommand's arguments.
    name : str
        The flag to look up, including its leading dashes.

    Returns
    -------
    str
        The value that follows the flag.

    Raises
    ------
    EvidenceError
        If the flag is absent or has no value.
    """
    try:
        index = list(arguments).index(name)
    except ValueError as error:
        raise EvidenceError(_missing_flag_message(name)) from error
    if index + 1 >= len(arguments):
        raise EvidenceError(_empty_flag_message(name))
    return arguments[index + 1]


def _session_directory(
    arguments: cabc.Sequence[str], directory: pth.Path, layout: str
) -> tuple[RunRecord, ...]:
    """Translate every session document in a directory into run records.

    The plugin writes one document per pytest session while a run starts many
    sessions, so a complete lane is a directory of documents, not one file.
    Combining them here is what makes a produced run analysable.

    Parameters
    ----------
    arguments : Sequence of str
        The subcommand's arguments, for the interpreter and cache-state flags.
    directory : pathlib.Path
        Directory holding session documents.
    layout : str
        The layout to attribute these sessions to.

    Returns
    -------
    tuple of RunRecord
        One record per document, in sorted path order.

    Raises
    ------
    EvidenceError
        If the directory holds no JSON document. An empty lane is not a clean
        lane, and silently returning nothing would make it look like one.
    """
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise EvidenceError(_empty_lane(directory))
    records: list[RunRecord] = []
    for path in paths:
        records.extend(_records_for(arguments, path, layout))
    return tuple(records)


def _records_for(
    arguments: cabc.Sequence[str], path: pth.Path, layout: str
) -> tuple[RunRecord, ...]:
    """Translate one session document, or load it as run records."""
    document = _decode(path)
    if not (isinstance(document, dict) and "collected" in document):
        return load_records(path)
    return (
        run_record_from_session(
            document,
            SessionIdentity(
                run_id=str(document.get("invocation_id", path.stem)),
                layout=layout,
                python_version=_optional(arguments, "--python-version", "unknown"),
                cache_state=_optional(arguments, "--cache-state", "warm"),
            ),
        ),
    )


def _decode(path: pth.Path) -> object:
    """Return one artefact's decoded JSON, as an evidence refusal on failure.

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
        If the file is missing, unreadable or not JSON.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise EvidenceError(_missing_file(path)) from error
    except OSError as error:
        raise EvidenceError(_unreadable_file(path, error)) from error
    except json.JSONDecodeError as error:
        raise EvidenceError(_invalid_json(path, error)) from error


def _optional(arguments: cabc.Sequence[str], name: str, default: str) -> str:
    """Return a flag's value, or ``default`` when it is absent."""
    return _flag(arguments, name) if name in arguments else default


def _empty_lane(directory: pth.Path) -> str:
    """Return the refusal text for a directory holding no session documents."""
    return f"{directory} holds no session documents"


def _missing_file(path: pth.Path) -> str:
    """Return the refusal text for an absent input file."""
    return f"{path} does not exist"


def _unreadable_file(path: pth.Path, error: OSError) -> str:
    """Return the refusal text for an unreadable input file."""
    return f"{path} could not be read: {error}"


def _invalid_json(path: pth.Path, error: json.JSONDecodeError) -> str:
    """Return the refusal text for an input that is not JSON."""
    return f"{path} is not valid JSON: {error}"


def _missing_flag_message(name: str) -> str:
    """Return the refusal text for an absent flag."""
    return f"{name} is required"


def _empty_flag_message(name: str) -> str:
    """Return the refusal text for a flag given without its value."""
    return f"{name} needs a value"


def _fidelity(arguments: cabc.Sequence[str]) -> dict[str, object]:
    """Build the fidelity report for one control/candidate pair of files.

    Parameters
    ----------
    arguments : Sequence of str
        ``--control``, ``--candidate`` and ``--output`` values.

    Returns
    -------
    dict
        The report, ready to persist.
    """
    control = _records(arguments, "--control", "baseline")
    candidate = _records(arguments, "--candidate", "consolidated")
    return fidelity(control, candidate)


def _records(
    arguments: cabc.Sequence[str], flag: str, layout: str
) -> tuple[RunRecord, ...]:
    """Load the records named by ``flag``, accepting a session artefact.

    A session document is what the pytest plugin writes, and a run record is
    what the analysis reads. Accepting both here is the executable conversion
    path: a reviewer points the tool at whichever artefact CI produced, and the
    adapter translates the session shape.

    Parameters
    ----------
    arguments : Sequence of str
        The subcommand's arguments.
    flag : str
        ``--control`` or ``--candidate``.
    layout : str
        The layout to attribute a session document to.

    Returns
    -------
    tuple of RunRecord
        The records the file describes.

    Raises
    ------
    EvidenceError
        If the file is missing, unreadable or not JSON. Reporting it here keeps
        `main` able to answer with its evidence status rather than a traceback.
    """  # ruff: ignore[docstring-extraneous-exception] - EvidenceError propagates from _records_for.
    path = pth.Path(_flag(arguments, flag))
    if path.is_dir():
        return _session_directory(arguments, path, layout)
    return _records_for(arguments, path, layout)


def _compare(arguments: cabc.Sequence[str]) -> dict[str, object]:
    """Build the comparison report for one evidence file.

    Parameters
    ----------
    arguments : Sequence of str
        ``--input`` and ``--output`` values.

    Returns
    -------
    dict
        The report, ready to persist.
    """
    records = load_records(pth.Path(_flag(arguments, "--input")))
    return compare(records).as_report()


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Run the evidence command line.

    Parameters
    ----------
    argv : Sequence of str or None
        Arguments after the program name. Defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        Zero on success, ``1`` when the evidence was unusable, ``2`` when the
        command line was refused.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print(
            "usage: pytest_layout_evidence_cli.py {fidelity|compare} ...",
            file=sys.stderr,
        )
        return EXIT_USAGE
    command, rest = arguments[0], arguments[1:]
    builders = {"fidelity": _fidelity, "compare": _compare}
    builder = builders.get(command)
    if builder is None:
        print(f"unknown command {command!r}", file=sys.stderr)
        return EXIT_USAGE
    try:
        report = builder(rest)
        write_reports(pth.Path(_flag(rest, "--output")), report)
    except EvidenceError as error:
        print(f"pytest layout evidence: {error}", file=sys.stderr)
        return EXIT_EVIDENCE
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a script
    sys.exit(main())
