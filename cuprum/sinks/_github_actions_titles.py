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
    """Resolved titles and the destinations that may need redaction."""

    group: str
    annotation: str
    has_configured_group_title: bool
    has_explicit_title: bool


def _session_titles(start: SessionStart, title: str | None) -> _SessionTitles:
    """Resolve the configured group title and bounded annotation label."""
    if title is not None:
        return _SessionTitles(
            group=title,
            annotation=title,
            has_configured_group_title=True,
            has_explicit_title=True,
        )
    if start.argv:
        return _SessionTitles(
            group=" ".join(start.argv),
            annotation=start.label,
            has_configured_group_title=True,
            has_explicit_title=False,
        )
    return _SessionTitles(
        group=start.label,
        annotation=start.label,
        has_configured_group_title=False,
        has_explicit_title=False,
    )


def _redacted_session_titles(
    titles: _SessionTitles,
    redaction: RedactionPolicy | None,
    *,
    emit_group: bool,
    emit_annotation: bool,
) -> _SessionTitles:
    """Redact only configured titles that their output destinations use."""
    policy = redaction if redaction is not None else _INERT_REDACTION_POLICY
    group = titles.group
    annotation = titles.annotation
    if emit_group and titles.has_configured_group_title:
        group = policy.apply_text(group)
    if emit_annotation and titles.has_explicit_title:
        annotation = policy.apply_text(annotation)
    return dc.replace(titles, group=group, annotation=annotation)
