"""Build the initial attributes exported on a tracing span."""

from __future__ import annotations

import typing as typ

from cuprum.adapters._support import _event_common_fields, _prefixed, _project_tag

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent


def _build_tracing_attributes(
    event: ExecEvent,
    *,
    original_project_tag: str | None = None,
) -> dict[str, object]:
    """Project common fields and the phase-specific tracing tags."""
    attrs = dict(_event_common_fields(event, _prefixed("cuprum."), argv=list))

    project_tag = _project_tag(event)
    project = project_tag
    if original_project_tag is not None and project_tag != original_project_tag:
        # Keep legacy tag precedence unless redaction changed that tag.
        project = event.project if event.project is not None else project_tag
    if project is not None:
        attrs["cuprum.project"] = project
    if "pipeline_stage_index" in event.tags:
        attrs["cuprum.pipeline_stage_index"] = event.tags["pipeline_stage_index"]
    if "pipeline_stages" in event.tags:
        attrs["cuprum.pipeline_stages"] = event.tags["pipeline_stages"]

    return attrs
