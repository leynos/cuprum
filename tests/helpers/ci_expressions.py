"""Evaluate the small subset of GitHub expressions the workflows branch on.

A contract that pattern-matches a conditional accepts it as soon as the text
looks right, and the failure that matters is a well-formed expression that
means the wrong thing. `tests/helpers/ci_placement.py` refuses a swapped arm
for `runs-on`; the backend and credentials expressions that depend on the same
predicate need their arms resolved too, and resolving them by substring would
repeat the mistake that reader exists to avoid.

This module therefore evaluates the expression instead of reading it. It
supports exactly what these workflows use: parentheses, `!`, `&&`, `||`, `==`,
`!=`, single-quoted string literals, the booleans `true` and `false`, and
dotted identifiers resolved from a mapping. Anything else raises
:class:`ExpressionError`, so an expression this reader cannot model fails a
contract rather than being guessed at.

GitHub's own semantics are kept where they are observable: `&&` and `||`
return one of their operands rather than a coerced boolean, which is why
`condition && 'a' || 'b'` renders as one of the two strings, and falsy values
are `false`, `null`, the empty string, and numeric zero.

Examples
--------
>>> evaluate(
...     "(fork || author == 'bot') && 'local' || 'gha'",
...     {"fork": True, "author": ""},
... )
'local'
>>> evaluate("!(fork || author == 'bot')", {"fork": False, "author": "bot"})
False
>>> evaluate("fork == 'yes'", {"fork": "yes"})
True
>>> arm_condition("(fork || author == 'bot') && 'local' || 'gha'", "local", "gha")
"(fork || author == 'bot')"
"""

from __future__ import annotations

import re
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("ExpressionError", "arm_condition", "evaluate", "referenced_names", "unwrap")

#: One token, longest operator first so `&&` is never read as two `&`.
_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<string>'[^']*')
  | (?P<operator>\|\||&&|==|!=|!|\(|\))
  | (?P<name>[A-Za-z_][\w.-]*)
    """,
    re.VERBOSE,
)
#: The `${{ ... }}` wrapper a YAML value carries around an expression.
_WRAPPER = re.compile(r"\A\s*\$\{\{(?P<body>.*)\}\}\s*\Z", re.DOTALL)
_LITERALS = {"true": True, "false": False, "null": None}
_FALSY = (False, None, "", 0)

type Token = tuple[str, str]


class ExpressionError(ValueError):
    """Raised when an expression falls outside the modelled subset."""


def unwrap(value: object) -> str:
    """Return an expression's body, refusing a value that is not one.

    Parameters
    ----------
    value : object
        A YAML value as parsed, such as ``"${{ (fork && 'a' || 'b') }}"``.

    Returns
    -------
    str
        The text inside ``${{ }}``, stripped.

    Raises
    ------
    ExpressionError
        When the value is not a string carrying exactly one ``${{ }}`` span.
    """
    if not isinstance(value, str):
        message = f"expected an expression, got {value!r}"
        raise ExpressionError(message)
    match = _WRAPPER.match(value)
    if match is None:
        message = f"expected exactly one ${{{{ }}}} expression, got {value!r}"
        raise ExpressionError(message)
    return match.group("body").strip()


def _tokens(text: str) -> list[Token]:
    """Split an expression into ``(kind, text)`` tokens, refusing the unknown."""
    found: list[Token] = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            message = f"cannot tokenize {text[position:]!r}"
            raise ExpressionError(message)
        position = match.end()
        kind = match.lastgroup
        if kind != "space":
            found.append((str(kind), match.group(0)))
    return found


def referenced_names(text: str) -> frozenset[str]:
    """Return every identifier an expression reads.

    Parameters
    ----------
    text : str
        An expression body, without the ``${{ }}`` wrapper.

    Returns
    -------
    frozenset[str]
        The dotted identifiers, excluding literals and operators. For example,
        ``"(github.a || github.b) && 'x'"`` yields ``github.a`` and ``github.b``.
    """
    return frozenset(
        spelling
        for kind, spelling in _tokens(text)
        if kind == "name" and spelling not in _LITERALS
    )


def _truthy(value: object) -> bool:
    """Report whether GitHub would treat ``value`` as true."""
    return not any(value is falsy or value == falsy for falsy in _FALSY)


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


def _coerce(value: object) -> object:
    """Coerce one operand the way GitHub compares it."""
    if value is None or value is False:
        return 0
    if value is True:
        return 1
    if isinstance(value, str):
        return 0 if not value else value.lower()
    return value


def actions_equal(left: object, right: object) -> bool:
    """Compare two values with GitHub Actions equality.

    GitHub coerces ``null`` and ``false`` to zero, compares strings without
    case, and coerces a numeric string to its number. Matching those rules is
    what keeps the evaluator from answering differently from the expression it
    validates.

    Returns
    -------
    bool
        Whether GitHub would consider the operands equal.
    """
    if isinstance(left, str) and isinstance(right, str):
        # GitHub compares two strings as strings, without numeric coercion.
        return left.lower() == right.lower()
    first, second = _coerce(left), _coerce(right)
    if isinstance(first, (int, float)) and isinstance(second, (int, float)):
        return float(first) == float(second)  # ruff: ignore[float-equality-comparison] - exact by contract.
    if isinstance(first, (int, float)) or isinstance(second, (int, float)):
        numeric, other = (
            (first, second) if isinstance(first, (int, float)) else (second, first)
        )
        try:
            return float(typ.cast("float", numeric)) == float(str(other))  # ruff: ignore[float-equality-comparison] - exact by contract.
        except ValueError:
            return False
    return first == second


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


class _Parser:
    """Recursive-descent parser over one token list."""

    def __init__(self, tokens: list[Token], values: cabc.Mapping[str, object]) -> None:
        """Bind one token list to the identifiers it may read."""
        self._tokens = tokens
        self._values = values
        self._index = 0

    def parse(self) -> object:
        """Return the value of the whole expression, refusing trailing tokens."""
        value = self._or()
        if self._index != len(self._tokens):
            message = f"unexpected token {self._tokens[self._index][1]!r}"
            raise ExpressionError(message)
        return value

    def _peek(self) -> str | None:
        """Return the next token's text without consuming it."""
        return self._tokens[self._index][1] if self._index < len(self._tokens) else None

    def _take(self) -> str:
        """Consume and return the next token's text."""
        spelling = self._peek()
        if spelling is None:
            message = "unexpected end of expression"
            raise ExpressionError(message)
        self._index += 1
        return spelling

    def _or(self) -> object:
        """Parse ``a || b`` left-associatively, returning an operand."""
        value = self._and()
        while self._peek() == "||":
            self._take()
            right = self._and()
            value = value if _truthy(value) else right
        return value

    def _and(self) -> object:
        """Parse ``a && b`` left-associatively, returning an operand."""
        value = self._equality()
        while self._peek() == "&&":
            self._take()
            right = self._equality()
            value = right if _truthy(value) else value
        return value

    def _equality(self) -> object:
        """Parse one ``==`` or ``!=`` comparison of unary operands."""
        value = self._unary()
        operator = self._peek()
        if operator not in {"==", "!="}:
            return value
        self._take()
        right = self._unary()
        equal = actions_equal(value, right)
        return equal if operator == "==" else not equal

    def _unary(self) -> object:
        """Parse a negation or a primary value."""
        if self._peek() == "!":
            self._take()
            return not _truthy(self._unary())
        return self._primary()

    def _primary(self) -> object:
        """Parse a group, a literal, or an identifier from ``values``."""
        spelling = self._take()
        if spelling == "(":
            value = self._or()
            if self._take() != ")":
                message = "unbalanced parenthesis"
                raise ExpressionError(message)
            return value
        if spelling.startswith("'"):
            return spelling[1:-1]
        if spelling in _LITERALS:
            return _LITERALS[spelling]
        if spelling in self._values:
            return self._values[spelling]
        message = f"no value supplied for {spelling!r}"
        raise ExpressionError(message)


def evaluate(expression: str, values: cabc.Mapping[str, object]) -> object:
    """Evaluate one expression body against identifier values.

    Parameters
    ----------
    expression : str
        The body of an expression, without the ``${{ }}`` wrapper.
    values : collections.abc.Mapping[str, object]
        Each identifier's value. A name the mapping omits fails rather than
        rendering empty, so a contract cannot pass by accident.

    Returns
    -------
    object
        The operand GitHub would select: a boolean, a string, or one of the
        identifier values.

    Raises
    ------
    ExpressionError
        When the expression uses anything outside the modelled subset, or
        names an identifier the mapping does not supply.
    """
    tokens = _tokens(expression)
    if not tokens:
        message = "empty expression"
        raise ExpressionError(message)
    return _Parser(tokens, values).parse()


def _literal_at(tokens: list[Token], index: int, expected: str) -> None:
    """Require one token to be the single-quoted ``expected`` literal."""
    kind, spelling = tokens[index]
    if kind != "string" or spelling != f"'{expected}'":
        message = f"the final arm must be {expected!r}, got {spelling!r}"
        raise ExpressionError(message)


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
    # A third arm such as `... || 'gha' || 's3'` leaves `'gha'` on the right of
    # an inner alternation; the conjunction check below then sees it as the
    # hosted arm and refuses, which is the intended failure.
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
    condition = " ".join(token for _, token in left[:and_index])
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
