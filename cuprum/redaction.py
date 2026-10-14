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


def _substitute_pattern(pattern: re.Pattern[str], value: str, mask: str) -> str:
    """Substitute matches with *mask* as literal text."""
    return pattern.sub(lambda _match: mask, value)


class RedactionError(RuntimeError):
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
        is coerced to an immutable tuple, ordered longest first to avoid
        partially masking a longer literal through its shorter prefix. Empty
        strings are rejected because replacing them would insert a mask
        between every character.
    patterns:
        Compiled text regular expressions to replace. A single pattern or any
        iterable of patterns is coerced to an immutable tuple.
    mask:
        Text substituted for each match. Defaults to ``"[REDACTED]"``.

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
        literals = (
            (self.literals,) if isinstance(self.literals, str) else tuple(self.literals)
        )
        if any(not isinstance(value, str) for value in literals):
            msg = "literals must contain only strings"
            raise TypeError(msg)
        if any(not value for value in literals):
            msg = "literals must not contain empty strings"
            raise ValueError(msg)

        patterns = (
            (self.patterns,)
            if isinstance(self.patterns, re.Pattern)
            else tuple(self.patterns)
        )
        if any(
            not isinstance(pattern, re.Pattern) or not isinstance(pattern.pattern, str)
            for pattern in patterns
        ):
            msg = "patterns must contain compiled text regular expressions"
            raise TypeError(msg)
        if not isinstance(self.mask, str):
            msg = "mask must be a string"
            raise TypeError(msg)

        object.__setattr__(
            self, "literals", tuple(sorted(literals, key=len, reverse=True))
        )
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
        RedactionError
            If a regular-expression substitution fails. The error identifies
            the rule without retaining or formatting the source text.
        """
        if not isinstance(value, str):
            msg = "value must be a string"
            raise TypeError(msg)
        if not self.literals and not self.patterns:
            return value

        redacted = value
        for literal in self.literals:
            redacted = str.replace(redacted, literal, self.mask)
        for index, pattern in enumerate(self.patterns):
            error_type: str | None = None
            try:
                redacted = _substitute_pattern(pattern, redacted, self.mask)
            except Exception as error:  # ruff: ignore[blind-except] - any rule failure must stop export
                # Keep only safe type metadata; the exception text can include
                # the value being redacted.
                error_type = type(error).__name__
            if error_type is not None:
                raise RedactionError(
                    rule_kind="pattern",
                    rule_index=index,
                    error_type=error_type,
                ) from None
        return redacted

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
        env = (
            None
            if event.env is None
            else MappingProxyType(
                {
                    name: self.apply_text(value) if isinstance(value, str) else value
                    for name, value in event.env.items()
                },
            )
        )
        tags = MappingProxyType(
            {
                key: self.apply_text(value) if isinstance(value, str) else value
                for key, value in event.tags.items()
            },
        )
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
