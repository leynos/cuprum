"""Resolve and redact the titles used by GitHub Actions workflow commands."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum.redaction import RedactionPolicy

if typ.TYPE_CHECKING:
    from cuprum.sinks.base import SessionStart


_INERT_REDACTION_POLICY = RedactionPolicy()


@dc.dataclass(frozen=True, slots=True)
class _SessionTitles:
    """Resolved group and annotation titles."""

    group: str
    annotation: str


def _session_titles(start: SessionStart, title: str | None) -> _SessionTitles:
    """Resolve the configured group title and bounded annotation label."""
    if title is not None:
        return _SessionTitles(
            group=title,
            annotation=title,
        )
    if start.argv:
        return _SessionTitles(
            group=" ".join(start.argv),
            annotation=start.label,
        )
    return _SessionTitles(
        group=start.label,
        annotation=start.label,
    )


def _redacted_session_titles(
    titles: _SessionTitles,
    redaction: RedactionPolicy | None,
    *,
    emit_group: bool,
    emit_annotation: bool,
) -> _SessionTitles:
    """Redact every title that its output destination emits."""
    policy = redaction if redaction is not None else _INERT_REDACTION_POLICY
    group = titles.group
    annotation = titles.annotation
    if emit_group:
        group = policy.apply_text(group)
    if emit_annotation:
        annotation = policy.apply_text(annotation)
    return dc.replace(titles, group=group, annotation=annotation)
