#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Render and write the compiler-cache experiment summary.

The reader in ``scripts.compiler_cache_summary`` computes the summary; this
module owns the command line and the Markdown view, so each file stays within
the repository's module budget.

The command fails closed: a malformed artefact exits non-zero rather than
producing a plausible report. It writes a JSON summary and, with
``--markdown``, a compact table for the pull request or the issue.

Inputs
------
``--records``
    One or more JSONL files, or directories containing them.
``--jobs``
    A ``gh api .../jobs`` document, or a file holding a list of them.
``--job-names``
    A JSON object mapping a record's ``job`` key (``github.job``) to a jobs API
    display-name substring, optionally with a ``{leg}`` placeholder. The API
    does not expose the job key, so the mapping is declared rather than
    guessed.
``--console``
    A dated console-observation JSON: storage, eviction, branch protection, and
    billing rounding.

Examples
--------
>>> render_markdown({"generated_at": "x", "cost": {"billing": "proxy"},
...                  "families": [], "gate": {"decisions": []}}).splitlines()[0]
'# Compiler-cache evidence summary'
"""

from __future__ import annotations

import argparse
import json
import sys
import typing as typ
from pathlib import Path

from scripts import compiler_cache_records as records_reader
from scripts import compiler_cache_schema as schema
from scripts import compiler_cache_summary as summary_reader

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def render_markdown(summary: cabc.Mapping[str, object]) -> str:
    """Render a compact Markdown report beside the JSON summary."""
    cost = typ.cast("dict[str, object]", summary["cost"])
    lines = [
        "# Compiler-cache evidence summary",
        "",
        (
            f"Generated at {summary.get('generated_at', 'unknown')}. Paid time is a "
            f"{cost['billing']} label; see docs/ci-cache-ownership.md."
        ),
        "",
        "| family | pairs | failures | median saving (s) | verdict |",
        "| --- | --- | --- | --- | --- |",
    ]
    families = {
        str(family["family"]): family
        for family in typ.cast("list[dict[str, object]]", summary["families"])
    }
    gate = typ.cast("dict[str, object]", summary["gate"])
    decisions = typ.cast("list[dict[str, object]]", gate["decisions"])
    for decision in decisions:
        family = families[str(decision["family"])]
        lines.append(
            f"| {decision['family']} | {decision['pairs']} | "
            f"{family['failures']} | {decision['median_saving_seconds']} | "
            f"{decision['verdict']} |"
        )
    if not decisions:
        lines.append("| _no observations_ | 0 | 0 | unknown | insufficient-evidence |")
    return "\n".join(lines) + "\n"


def _parse(argv: cabc.Sequence[str]) -> argparse.Namespace:
    """Parse the reader's command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--records", nargs="+", type=Path, required=True)
    parser.add_argument("--jobs", type=Path)
    parser.add_argument("--job-names", type=Path)
    parser.add_argument("--console", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    return parser.parse_args(list(argv))


def main(argv: cabc.Sequence[str]) -> int:
    """Run the reader; return non-zero on any contract failure."""
    arguments = _parse(argv)
    try:
        records = records_reader.load_records(arguments.records)
        summary = summary_reader.build_summary(
            records,
            records_reader.load_jobs(arguments.jobs),
            records_reader.load_names(arguments.job_names),
            records_reader.load_console(arguments.console),
        )
        # The clock is an edge concern: the summary is otherwise a pure
        # function of the evidence, the joins, and the console observation.
        summary["generated_at"] = schema.now()
    except records_reader.InvalidEvidenceError as error:
        print(f"compiler-cache evidence: {error}", file=sys.stderr)
        return 1
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if arguments.markdown is not None:
        arguments.markdown.parent.mkdir(parents=True, exist_ok=True)
        arguments.markdown.write_text(render_markdown(summary), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
