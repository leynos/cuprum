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
    """
    path = pth.Path(_flag(arguments, flag))
    document = json.loads(path.read_text(encoding="utf-8"))
    if not (isinstance(document, dict) and "collected" in document):
        return load_records(path)
    return (
        run_record_from_session(
            document,
            SessionIdentity(
                run_id=str(document.get("invocation_id", path.stem)),
                layout=layout,
                python_version=_flag(arguments, "--python-version")
                if "--python-version" in arguments
                else "unknown",
                cache_state=_flag(arguments, "--cache-state")
                if "--cache-state" in arguments
                else "warm",
            ),
        ),
    )


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
