"""Property and snapshot tests for the canonical adapter event projection.

The tracing, metrics, and logging adapters previously each re-implemented the
"include the field only when not ``None``" projection of an ``ExecEvent`` and
disagreed on the key prefix. ``cuprum.adapters._support._event_common_fields``
is now the single source of truth (#114); these tests pin its contract:

- the projection yields exactly the non-``None`` optional fields;
- all three adapters agree on the common key set, modulo the parameterized
  prefix; and
- the projected dictionaries for a representative event in each phase are
  locked with syrupy snapshots (volatile fields redacted).
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cuprum.adapters._support import _event_common_fields
from cuprum.adapters.logging_adapter import _build_extra
from cuprum.adapters.metrics_adapter import MetricsHook
from cuprum.adapters.tracing_adapter import TracingHook
from cuprum.events import ExecEvent, ExecPhase
from cuprum.program import Program

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

_OPTIONAL_FIELDS = ("pid", "cwd", "exit_code", "duration_s", "line")
_PHASES = typ.get_args(ExecPhase.__value__)
_REDACTED_FIELDS = frozenset({"pid", "duration_s", "cwd"})


@st.composite
def _events(draw: st.DrawFn) -> ExecEvent:
    """Generate events with every optional field independently present/absent."""
    return ExecEvent(
        phase=draw(st.sampled_from(_PHASES)),
        program=Program("echo"),
        argv=("echo", "hello"),
        cwd=draw(st.none() | st.just(Path("/srv/work"))),
        env=None,
        pid=draw(st.none() | st.integers(min_value=1, max_value=99_999)),
        timestamp=0.0,
        line=draw(st.none() | st.just("a line")),
        exit_code=draw(st.none() | st.integers(min_value=0, max_value=255)),
        duration_s=draw(st.none() | st.just(0.125)),
        tags=draw(
            st.dictionaries(
                st.sampled_from(
                    ("project", "pipeline_stage_index", "pipeline_stages"),
                ),
                st.none()
                | st.text(max_size=20)
                | st.integers(min_value=0, max_value=5),
                max_size=3,
            ),
        ),
    )


def _expected_projection(event: ExecEvent) -> dict[str, object]:
    """Mirror the projection contract: ``None`` omitted, ``cwd`` stringified."""
    expected: dict[str, object] = {
        "program": str(event.program),
        "argv": event.argv,
    }
    present = (
        (field, getattr(event, field))
        for field in _OPTIONAL_FIELDS
        if getattr(event, field) is not None
    )
    for field, value in present:
        expected[field] = str(value) if field == "cwd" else value
    return expected


@settings(
    deadline=None,
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(event=_events())
def test_projection_includes_exactly_the_non_none_fields(event: ExecEvent) -> None:
    """Property: the canonical projection omits exactly the ``None`` fields.

    Parameters
    ----------
    event : ExecEvent
        Generated event with optional fields independently present or absent.
    """
    projected = dict(_event_common_fields(event, lambda field: field))

    assert projected == _expected_projection(event), (
        "projection must carry program, argv, and exactly the non-None "
        "optional fields (cwd stringified)"
    )


@settings(
    deadline=None,
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(event=_events())
def test_adapters_agree_on_common_keys_modulo_prefix(event: ExecEvent) -> None:
    """Property: the three adapters expose the same common key set.

    The logging extras (``cuprum_`` prefix) and tracing attributes
    (``cuprum.`` prefix) must carry the same canonical fields; the metrics
    labels are the deliberate low-cardinality subset (``program`` plus
    ``project``).

    Parameters
    ----------
    event : ExecEvent
        Generated event with optional fields independently present or absent.
    """
    canonical = {key for key, _ in _event_common_fields(event, lambda field: field)}

    extra_keys = {
        key.removeprefix("cuprum_")
        for key in _build_extra(event)
        if key not in {"cuprum_phase", "cuprum_tags"}
    }
    assert extra_keys == canonical, (
        "logging extras must expose exactly the canonical common fields after "
        "removing their backend prefix"
    )
    assert _build_extra(event)["cuprum_argv"] == event.argv, (
        "logging extras must preserve argv as a tuple"
    )

    attr_keys = {
        key.removeprefix("cuprum.")
        for key in TracingHook._build_attributes(event)
        if key
        not in {
            "cuprum.project",
            "cuprum.pipeline_stage_index",
            "cuprum.pipeline_stages",
        }
    }
    assert attr_keys == canonical, (
        "tracing attributes must expose exactly the canonical common fields "
        "after removing their backend prefix"
    )
    assert TracingHook._build_attributes(event)["cuprum.argv"] == list(event.argv), (
        "tracing attributes must render argv as a list"
    )

    labels = MetricsHook._extract_labels(event)
    assert set(labels) == {"program", "project"}, (
        "metrics labels must stay limited to the low-cardinality program and "
        "project fields"
    )
    assert labels["program"] == str(event.program), (
        "metrics labels must stringify the event program when it is present"
    )
    project = event.tags.get("project")
    assert labels["project"] == ("unknown" if project is None else str(project)), (
        "metrics labels must stringify a real project tag and fall back to "
        "'unknown' only when the tag is absent or None"
    )


def _representative_event(phase: str) -> ExecEvent:
    """Build a deterministic, fully populated event for *phase*."""
    is_exit = phase == "exit"
    is_output = phase in {"stdout", "stderr"}
    return ExecEvent(
        phase=typ.cast("typ.Any", phase),
        program=Program("echo"),
        argv=("echo", "hello"),
        cwd=Path("/srv/work"),
        env=None,
        pid=None if phase == "plan" else 4321,
        timestamp=0.0,
        line="a line" if is_output else None,
        exit_code=0 if is_exit else None,
        duration_s=0.125 if is_exit else None,
        tags={"project": "proj", "pipeline_stage_index": 0, "pipeline_stages": 2},
    )


def _redact(mapping: dict[str, object]) -> dict[str, object]:
    """Replace volatile fields (pid, duration, cwd) with stable tokens."""
    redacted: dict[str, object] = {}
    for key, value in mapping.items():
        field = key.removeprefix("cuprum.").removeprefix("cuprum_")
        if field in _REDACTED_FIELDS:
            redacted[key] = f"<{field}>"
        else:
            redacted[key] = value
    return redacted


@pytest.mark.parametrize("phase", _PHASES)
def test_projection_snapshots_lock_the_wire_contract(
    phase: str,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot: the per-phase projected dictionaries are stable.

    Locks the multivariant output format across the three adapters for a
    representative event in each phase. Volatile fields (pid, duration, cwd)
    are redacted with stable tokens; the surrounding property tests assert
    their semantics.
    """
    event = _representative_event(phase)
    projections = {
        "logging_extra": _redact(_build_extra(event)),
        "tracing_attributes": _redact(TracingHook._build_attributes(event)),
        "metrics_labels": _redact(dict(MetricsHook._extract_labels(event))),
    }
    assert projections == snapshot, (
        "per-phase adapter projections must match the redacted wire-contract snapshot"
    )
