"""Shared fixtures for the 5.2.1 construction-share classifier tests.

The classifier's interesting failure modes are all about *identification*: a
generated ``__init__`` renders as ``__init__ (<string>:N)`` because
:func:`dataclasses.dataclass` builds it with :func:`exec`, so every generated
constructor in a capture looks alike. Both the scoring tests and the parsing
tests need to build captures that exercise that disambiguation, so the frame
constants and the synthetic-capture helpers live here rather than being
duplicated.

Synthetic captures are built through the same parser the real path uses, so a
test cannot pass by constructing internally inconsistent input.
"""

from __future__ import annotations

import json
import typing as typ

from benchmarks import summarize_line_event_profile as classifier

if typ.TYPE_CHECKING:
    import pathlib as pth

# The consume symbol whose presence forms the gate's denominator.
CONSUME = "_consume_stream_with_lines (cuprum/_stream_line_consumer.py:69)"
# A generated constructor: indistinguishable across every dataclass.
GENERATED_INIT = "__init__ (<string>:3)"
# The line-callback frame that reaches the constructors.
EMIT_LINE = "emit_line (cuprum/_line_callbacks.py:104)"
# The generic observation emit, reached by the per-line hook closure and the
# non-line phases alike. It is a *sibling* of `emit_line`, not a dispatcher.
OBSERVATION_EMIT = "emit (cuprum/_pipeline_types.py:120)"
# The observe-hook dispatch seam, in call order. A construction reached
# through either of these is hook-owned work, not event emission.
EMIT_EVENT = "_emit_event (cuprum/_pipeline_types.py:203)"
EMIT_EXEC_EVENT = "_emit_exec_event (cuprum/_observability.py:90)"
# A hook body, i.e. arbitrary user code the dispatcher invoked.
HOOK_BODY = "observe_line (benchmarks/_tee_profile_worker_execution.py:100)"
# The stream consumer's per-line helper. py-spy renders its leading
# underscore intact, so this is a *different* function name -- the capture
# proves that, with `_emit_line (cuprum/_stream_line_consumer.py:...)` and
# `emit_line (cuprum/_line_callbacks.py:...)` both present and distinct.
CONSUMER_EMIT_LINE = "_emit_line (cuprum/_stream_line_consumer.py:39)"
# A frame carrying the dispatcher's *name* from another module: a vendored
# or relocated copy. A boundary keyed on the bare name would mistake it for
# the real seam and drop everything constructed beneath it.
FOREIGN_EMIT_EXEC_EVENT = "_emit_exec_event (vendor/observability.py:64)"
# An unrelated caller that must never resolve a generated frame.
UNRELATED = "build (cuprum/context.py:41)"

# The repository's committed rules file, relative to the repository root.
SHIPPED_RULES = "docs/profiling/5-2-1-line-event-emission/classifier-rules.json"


def _write(tmp_path: pth.Path, name: str, text: str) -> pth.Path:
    """Write ``text`` to ``name`` under ``tmp_path`` and return the path."""
    path = tmp_path / name
    path.write_text(text)
    return path


def _rules_file(tmp_path: pth.Path) -> pth.Path:
    """Write a minimal valid rules file exercising caller disambiguation."""
    payload = {
        "consume_frames": [
            {
                "function": "_consume_stream_with_lines",
                "location": "cuprum/_stream_line_consumer.py",
            },
        ],
        "construction_rules": [
            {
                "name": "ExecEvent via observation emit",
                "frame": {"function": "__init__", "location": "<string>"},
                "callers": [
                    {"function": "emit", "location": "cuprum/_pipeline_types.py"},
                ],
            },
            {
                "name": "per-line payload",
                "frame": {"function": "__init__", "location": "<string>"},
                "callers": [
                    {
                        "function": "emit_line",
                        "location": "cuprum/_line_callbacks.py",
                    },
                ],
            },
        ],
    }
    return _write(tmp_path, "rules.json", json.dumps(payload))


def _run(
    tmp_path: pth.Path,
    capture_text: str,
    *,
    rules_path: pth.Path | None = None,
) -> tuple[int, dict[str, object]]:
    """Run the classifier over ``capture_text`` and return its exit and result."""
    folded = _write(tmp_path, "stacks.folded", capture_text)
    output = tmp_path / "result.json"
    status = classifier.main(
        [
            str(folded),
            "--rules",
            str(_rules_file(tmp_path) if rules_path is None else rules_path),
            "--output",
            str(output),
        ],
    )
    return status, typ.cast("dict[str, object]", json.loads(output.read_text()))


def _line(frames: str, samples: int) -> str:
    """Render one folded-stack line."""
    return f"{frames} {samples}\n"


def _shipped_rules_path() -> pth.Path:
    """Return the repository's committed classifier rules file."""
    import pathlib as pth

    return pth.Path(__file__).resolve().parents[2] / SHIPPED_RULES
