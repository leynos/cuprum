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

from tests.helpers.ci_expressions import ExpressionError, Token, _tokens, unwrap


def _literal_at(tokens: list[Token], index: int, expected: str) -> None:
    """Require one token to be the single-quoted ``expected`` literal."""
    kind, spelling = tokens[index]
    if kind != "string" or spelling != f"'{expected}'":
        message = f"the final arm must be {expected!r}, got {spelling!r}"
        raise ExpressionError(message)


def _top_level(tokens: list[Token], operator: str) -> list[int]:
    """Return the indices of ``operator`` tokens outside every group."""
    found: list[int] = []
    depth = 0
    for index, (kind, spelling) in enumerate(tokens):
        if depth == 0 and (kind, spelling) == ("operator", operator):
            found.append(index)
        depth = _depth_after(depth, kind, spelling)
    if depth != 0:
        message = "unbalanced parenthesis"
        raise ExpressionError(message)
    return found


def _depth_after(depth: int, kind: str, spelling: str) -> int:
    """Return the group depth after one token, refusing an unmatched closer."""
    if kind != "operator":
        return depth
    if spelling == "(":
        return depth + 1
    if spelling != ")":
        return depth
    if depth == 0:
        message = "unbalanced parenthesis"
        raise ExpressionError(message)
    return depth - 1


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
    alternations = _top_level(tokens, "||")
    if not alternations:
        message = f"expected `|| '{owned}'`, got {expression!r}"
        raise ExpressionError(message)
    split = alternations[-1]
    if len(tokens) != split + 2:
        message = f"extra tokens after the `{owned}` arm: {expression!r}"
        raise ExpressionError(message)
    _literal_at(tokens, split + 1, owned)
    left = tokens[:split]
    conjunctions = _top_level(left, "&&")
    if not conjunctions:
        message = f"expected `&& '{hosted}'`, got {expression!r}"
        raise ExpressionError(message)
    and_index = conjunctions[-1]
    if len(left) != and_index + 2:
        message = f"extra tokens after the `{hosted}` arm: {expression!r}"
        raise ExpressionError(message)
    _literal_at(left, and_index + 1, hosted)
    if _top_level(left[:and_index], "||"):
        message = f"the condition before the {hosted!r} arm is an ungrouped alternation"
        raise ExpressionError(message)
    condition = " ".join(spelling for _, spelling in left[:and_index])
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
