"""Read and compare the two-arm expressions the workflows branch on.

An expression such as ``<condition> && 'local' || 'gha'`` selects one of two
literals. `tests/helpers/ci_placement.py` reads the canonical runner form by
position; this module reads any two-arm form so a workflow contract can
compare the backend and credentials predicates with the runner's own.

Examples
--------
>>> arm_condition("(fork || author == 'bot') && 'local' || 'gha'", "local", "gha")
"(fork || author == 'bot')"
>>> normalized("${{  fork  &&  'a'  }}")
"fork && 'a'"
"""

from __future__ import annotations

from tests.helpers.ci_expressions import (
    ExpressionError,
    Token,
    _tokens,
    _top_level,
    unwrap,
)


def _literal_at(tokens: list[Token], index: int, expected: str) -> None:
    """Require one token to be the single-quoted ``expected`` literal."""
    kind, spelling = tokens[index]
    if kind != "string" or spelling != f"'{expected}'":
        message = f"the final arm must be {expected!r}, got {spelling!r}"
        raise ExpressionError(message)


def _arm_split(tokens: list[Token], operator: str, arm: str, expression: str) -> int:
    """Return the index of the top-level ``operator`` that introduces ``arm``."""
    splits = _top_level(tokens, operator)
    if not splits:
        message = f"expected `{operator} '{arm}'`, got {expression!r}"
        raise ExpressionError(message)
    split = splits[-1]
    if len(tokens) != split + 2:
        message = f"extra tokens after the `{arm}` arm: {expression!r}"
        raise ExpressionError(message)
    _literal_at(tokens, split + 1, arm)
    return split


def arm_condition(expression: str, hosted: str, owned: str) -> str:
    """Return the condition that selects between two string arms.

    Parameters
    ----------
    expression : str
        An expression body of the form
        ``<condition> && '<hosted>' || '<owned>'``.
    hosted : str
        The literal the condition selects when it is true.
    owned : str
        The literal it selects otherwise.

    Returns
    -------
    str
        The condition text, whitespace-normalized, so two expressions can be
        compared without depending on their layout.

    Raises
    ------
    ExpressionError
        When the expression is not that two-arm shape. The arms are read from
        the right, past every group, so a condition that itself contains `||`
        is not split at its own operator.
    """
    tokens = _tokens(expression)
    split = _arm_split(tokens, "||", owned, expression)
    left = tokens[:split]
    and_index = _arm_split(left, "&&", hosted, expression)
    if _top_level(left[:and_index], "||"):
        message = f"the condition before the {hosted!r} arm is an ungrouped alternation"
        raise ExpressionError(message)
    return _condition_text(left[:and_index], expression)


def _condition_text(tokens: list[Token], expression: str) -> str:
    """Return the tokens as one whitespace-normalized condition."""
    condition = " ".join(spelling for _, spelling in tokens)
    if not condition:
        message = f"empty condition in {expression!r}"
        raise ExpressionError(message)
    return condition


def normalized(expression: str) -> str:
    """Return an expression with its whitespace collapsed.

    Parameters
    ----------
    expression : str
        An expression body, with or without the ``${{ }}`` wrapper.

    Returns
    -------
    str
        The body with every run of whitespace collapsed to one space.
    """
    try:
        body = unwrap(expression)
    except ExpressionError:
        body = expression
    return " ".join(body.split())
