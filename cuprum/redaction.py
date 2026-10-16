"""Opt-in redaction for execution-event projections.

Redaction happens on copies at export boundaries. It never changes the
arguments, environment, or captured output used by an execution.
"""

from __future__ import annotations

import dataclasses as dc
import re
import typing as typ
from types import MappingProxyType

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent, ExecHook


_DEFAULT_MASK = "[REDACTED]"


def _normalize_literals(literals: str | cabc.Iterable[str]) -> tuple[str, ...]:
    """Validate literal rules and order longer matches before their prefixes."""
    values = (literals,) if isinstance(literals, str) else tuple(literals)
    if any(not isinstance(value, str) for value in values):
        msg = "literals must contain only strings"
        raise TypeError(msg)
    if any(not value for value in values):
        msg = "literals must not contain empty strings"
        raise ValueError(msg)
    return tuple(sorted(values, key=len, reverse=True))


def _normalize_patterns(
    patterns: re.Pattern[str] | cabc.Iterable[re.Pattern[str]],
) -> tuple[re.Pattern[str], ...]:
    """Validate compiled text rules while preserving their configured order."""
    values = (patterns,) if isinstance(patterns, re.Pattern) else tuple(patterns)
    if any(
        not isinstance(pattern, re.Pattern) or not isinstance(pattern.pattern, str)
        for pattern in values
    ):
        msg = "patterns must contain compiled text regular expressions"
        raise TypeError(msg)
    return values


def _literal_match_spans(literal: str, value: str) -> tuple[tuple[int, int], ...]:
    """Find literal occurrences, including occurrences that overlap."""
    spans: list[tuple[int, int]] = []
    search_from = 0
    while search_from <= len(value) - len(literal):
        start = str.find(value, literal, search_from)
        if start < 0:
            break
        spans.append((start, start + len(literal)))
        search_from = start + 1
    return tuple(spans)


def _pattern_match_spans(
    pattern: re.Pattern[str], value: str
) -> tuple[tuple[int, int], ...]:
    """Find regular-expression matches against the original value."""
    return tuple(match.span() for match in pattern.finditer(value))


def _collect_rule_spans[Rule](
    rules: tuple[Rule, ...],
    value: str,
    matcher: cabc.Callable[[Rule, str], tuple[tuple[int, int], ...]],
    rule_kind: typ.Literal["literal", "pattern"],
) -> tuple[tuple[int, int], ...]:
    """Collect configured matches without retaining source text in failures."""
    spans: list[tuple[int, int]] = []
    for index, rule in enumerate(rules):
        error_type: str | None = None
        try:
            spans.extend(matcher(rule, value))
        except Exception as error:  # ruff: ignore[blind-except] - fail closed
            # The exception text can include the value being redacted.
            error_type = type(error).__name__
        if error_type is not None:
            raise RedactionError(
                rule_kind=rule_kind,
                rule_index=index,
                error_type=error_type,
            ) from None
    return tuple(spans)


def _merge_nonzero_spans(
    spans: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    """Merge overlapping non-empty source matches."""
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _standalone_zero_width_spans(
    positions: set[int],
    nonzero_spans: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    """Retain zero-width matches outside non-empty match boundaries."""
    return tuple(
        (position, position)
        for position in positions
        if not any(start <= position <= end for start, end in nonzero_spans)
    )


def _merge_overlapping_spans(
    spans: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    """Merge overlapping source matches and retain standalone zero-width ones."""
    ordered_spans = tuple(sorted(spans))
    nonzero_spans = _merge_nonzero_spans(
        tuple((start, end) for start, end in ordered_spans if start != end)
    )
    zero_width_positions = {start for start, end in ordered_spans if start == end}
    standalone_zero_width_spans = _standalone_zero_width_spans(
        zero_width_positions,
        nonzero_spans,
    )
    return tuple(sorted((*nonzero_spans, *standalone_zero_width_spans)))


class RedactionError(Exception):
    """A configured redaction rule failed before an export could be built.

    Attributes
    ----------
    rule_kind:
        Whether the failing rule was a literal or regular expression.
    rule_index:
        Zero-based position of the rule in its configured tuple.
    error_type:
        Name of the underlying exception class. The exception text and source
        value are omitted so diagnostics cannot echo the value being redacted.
    """

    def __init__(
        self,
        *,
        rule_kind: typ.Literal["literal", "pattern"],
        rule_index: int,
        error_type: str,
    ) -> None:
        """Store rule metadata without retaining the value being redacted."""
        self.rule_kind = rule_kind
        self.rule_index = rule_index
        self.error_type = error_type
        super().__init__(
            f"Redaction {rule_kind} rule {rule_index} failed ({error_type})",
        )


@dc.dataclass(frozen=True, slots=True, init=False)
class RedactionPolicy:
    """Apply configured literal and regular-expression masks to exports.

    The default policy is inert. Rules are supplied by the caller; this class
    does not attempt to identify secrets automatically.

    Parameters
    ----------
    literals:
        Literal strings to replace. A single string or any iterable of strings
        is coerced to an immutable tuple, ordered longest first. Empty strings
        are rejected because they would match every position in the input.
    patterns:
        Compiled text regular expressions to replace. A single pattern or any
        iterable of patterns is coerced to an immutable tuple.
    mask:
        Opaque text substituted for each merged match. Defaults to
        ``"[REDACTED]"``.

    Examples
    --------
    >>> policy = RedactionPolicy(literals=("token-123",))
    >>> policy.apply_text("--token=token-123")
    '--token=[REDACTED]'
    """

    literals: tuple[str, ...] = ()
    patterns: tuple[re.Pattern[str], ...] = ()
    mask: str = _DEFAULT_MASK

    def __init__(
        self,
        literals: str | cabc.Iterable[str] = (),
        patterns: re.Pattern[str] | cabc.Iterable[re.Pattern[str]] = (),
        mask: str = _DEFAULT_MASK,
    ) -> None:
        """Normalize supported rule inputs into immutable field values."""
        object.__setattr__(self, "literals", literals)
        object.__setattr__(self, "patterns", patterns)
        object.__setattr__(self, "mask", mask)
        # init=False keeps constructor inputs broader than stored field types;
        # dataclasses therefore leaves post-initialization to this constructor.
        self.__post_init__()

    def __post_init__(self) -> None:
        """Coerce rule collections and validate their element types."""
        literals = _normalize_literals(self.literals)
        patterns = _normalize_patterns(self.patterns)
        if not isinstance(self.mask, str):
            msg = "mask must be a string"
            raise TypeError(msg)

        object.__setattr__(self, "literals", literals)
        object.__setattr__(self, "patterns", patterns)

    def apply_text(self, value: str) -> str:
        """Return *value* with every configured rule replaced by ``mask``.

        Parameters
        ----------
        value:
            Text projected to an opted-in destination.

        Returns
        -------
        str
            Sanitized text, or the original text when no rule matches.

        Raises
        ------
        TypeError
            If *value* is not a string.

        Notes
        -----
        Regular-expression substitution failures propagate as
        :class:`RedactionError`. The error identifies the rule without
        retaining or formatting the source text. Literal and pattern matches
        are collected against the original value; overlapping spans are
        replaced together, and inserted mask text is never rescanned.
        """
        if not isinstance(value, str):
            msg = "value must be a string"
            raise TypeError(msg)
        if not self.literals and not self.patterns:
            return value

        spans = _collect_rule_spans(
            self.literals,
            value,
            _literal_match_spans,
            "literal",
        ) + _collect_rule_spans(
            self.patterns,
            value,
            _pattern_match_spans,
            "pattern",
        )
        merged_spans = _merge_overlapping_spans(spans)
        if not merged_spans:
            return value

        pieces: list[str] = []
        cursor = 0
        for start, end in merged_spans:
            pieces.extend((value[cursor:start], self.mask))
            cursor = end
        pieces.append(value[cursor:])
        return "".join(pieces)

    def _redact_mapping_values[NonStringValue](
        self,
        values: cabc.Mapping[str, str | NonStringValue],
    ) -> cabc.Mapping[str, str | NonStringValue]:
        """Redact string values while preserving non-string markers."""
        return MappingProxyType(
            {
                key: self.apply_text(value) if isinstance(value, str) else value
                for key, value in values.items()
            },
        )

    def redact_event(self, event: ExecEvent) -> ExecEvent:
        """Return a sanitized event copy for an opted-in consumer.

        String values in ``argv``, ``env``, ``line``, ``note``, and ``tags``
        pass through :meth:`apply_text`. The program, trusted project, paths
        (``cwd`` and ``resolved_path``), tag keys, non-string tag values, and
        typed numeric and enum fields remain unchanged. An inert policy
        returns *event* itself.

        Parameters
        ----------
        event:
            Raw execution observation. It is never modified.

        Returns
        -------
        ExecEvent
            The original event for an inert policy, otherwise a copy whose
            selected text fields have been redacted.
        """
        if not self.literals and not self.patterns:
            return event

        argv = tuple(self.apply_text(value) for value in event.argv)
        # Non-string overlay values such as UNSET are deletion markers, not text.
        env = None if event.env is None else self._redact_mapping_values(event.env)
        tags = self._redact_mapping_values(event.tags)
        line = None if event.line is None else self.apply_text(event.line)
        note = None if event.note is None else self.apply_text(event.note)
        return dc.replace(
            event,
            argv=argv,
            env=env,
            line=line,
            note=note,
            tags=tags,
        )


def redacting_hook(consumer: ExecHook, policy: RedactionPolicy) -> ExecHook:
    """Wrap an observation consumer so it receives sanitized event copies.

    Parameters
    ----------
    consumer:
        The hook that exports or stores execution observations.
    policy:
        The explicit policy applied before the consumer receives an event.

    Returns
    -------
    ExecHook
        A hook suitable for registration with ``observe()``. The wrapper
        returns the consumer's result unchanged, including an awaitable result.
    """

    def consume(event: ExecEvent) -> cabc.Awaitable[None] | None:
        """Pass only the sanitized copy to the configured consumer."""
        return consumer(policy.redact_event(event))

    return consume


__all__ = ["RedactionError", "RedactionPolicy", "redacting_hook"]
