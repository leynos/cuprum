"""Tests for the offline evidence command line.

The analysis modules expose functions; a reviewer needs commands. These tests
drive `fidelity` and `compare` as a caller would, against real files, and check
the two statuses that matter: success, and unusable evidence reported as its
own failure rather than as success with an empty report.
"""

from __future__ import annotations

import json
import typing as typ

from scripts.pytest_layout_evidence_cli import main

if typ.TYPE_CHECKING:
    import pathlib as pth


def _write(tmp_path: pth.Path, name: str, nodes: tuple[str, ...]) -> pth.Path:
    """Write one artefact holding a single run."""
    path = tmp_path / name
    path.write_text(
        json.dumps({
            "runs": [
                {
                    "run_id": name,
                    "layout": "baseline" if "control" in name else "consolidated",
                    "python_version": "3.12",
                    "cache_state": "warm",
                    "node_ids": list(nodes),
                }
            ]
        }),
        encoding="utf-8",
    )
    return path


def test_the_cli_writes_a_fidelity_report(tmp_path: pth.Path) -> None:
    """Prove the promised `fidelity` subcommand exists and persists a report."""
    control = _write(tmp_path, "control.json", ("a", "b"))
    candidate = _write(tmp_path, "candidate.json", ("a", "b"))
    output = tmp_path / "out"

    status = main([
        "fidelity",
        "--control",
        str(control),
        "--candidate",
        str(candidate),
        "--output",
        str(output),
    ])

    assert status == 0, f"the command must succeed, got {status}"
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["identical"] is True, f"identical inputs must compare equal: {report}"


def test_the_cli_refuses_an_unusable_artefact(tmp_path: pth.Path) -> None:
    """Report unusable evidence as a distinct status, not as success."""
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    status = main([
        "compare",
        "--input",
        str(broken),
        "--output",
        str(tmp_path / "out"),
    ])

    assert status == 1, f"unusable evidence must exit 1, got {status}"
