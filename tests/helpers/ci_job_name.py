"""Render a job's declared check name for one matrix leg, refusing the rest.

`ci.yml`'s `typecheck-test` names its check contexts from the matrix, and one
entry appends a policy suffix rather than a value: a check name saying the job
does not run on a pull request is where a reviewer reads that leg's
pull-request behaviour, because the job's own steps are all skipped there. A
contract asserting that name has to render the template the way GitHub does,
and a renderer that quietly returned an unsupported expression unchanged would
compare the template with itself and pass for any declaration at all.

This module therefore supports exactly the two expression forms the job name
uses — a bare matrix value, and a ``matrix.<field> && '<literal>' || ''``
choice — and refuses everything else, naming the expression it could not read.
Literal text outside an expression is copied through. Legs come from
:func:`tests.helpers.ci_leg_matrix.matrix_legs`, the same expansion the leg
gates and cache families read, so the names a caller asserts and the legs those
contracts reason about cannot disagree.

Examples
--------
>>> render_template(
...     "Test (Python ${{ matrix.label }}"
...     "${{ matrix.experimental && ', PR skip' || '' }})",
...     {"label": "3.12", "experimental": False},
...     subject="example",
... )
'Test (Python 3.12)'
>>> render_template(
...     "Test (Python ${{ matrix.label }}"
...     "${{ matrix.experimental && ', PR skip' || '' }})",
...     {"label": "3.15.2", "experimental": True},
...     subject="example",
... )
'Test (Python 3.15.2, PR skip)'
"""

from __future__ import annotations

import math
import re
import typing as typ

from tests.helpers.ci_documents import require
from tests.helpers.ci_leg_matrix import matrix_legs
from tests.helpers.ci_workflows import job

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("render_name", "render_template", "rendered_names")

#: One ``${{ ... }}`` expression. The body is matched non-greedily so a name
#: carrying two expressions is split rather than read as one.
_EXPRESSION: typ.Final = re.compile(r"\$\{\{(?P<body>.*?)\}\}", re.DOTALL)
#: A bare matrix reference, the only value form a name may interpolate.
_BARE_MATRIX: typ.Final = re.compile(
    r"\A\s*matrix\.(?P<field>[a-z0-9-]+)\s*\Z", re.IGNORECASE
)
#: A matrix-conditional literal: the ``when`` text for a truthy field, the
#: ``otherwise`` text for a falsy one, and ``otherwise`` again when the field
#: is truthy but ``when`` is empty, because GitHub's ``||`` reads the empty
#: string as falsy. Either literal may be empty. Truthiness is GitHub's, not
#: Python's, so a value such as an empty array selects ``when``.
_MATRIX_CHOICE: typ.Final = re.compile(
    r"\A\s*matrix\.(?P<field>[a-z0-9-]+)\s*&&\s*'(?P<when>[^']*)'\s*"
    r"\|\|\s*'(?P<otherwise>[^']*)'\s*\Z",
    re.IGNORECASE | re.DOTALL,
)


def declared_name(workflow_name: str, job_name: str) -> str:
    """Return one job's declared ``name:`` template.

    Parameters
    ----------
    workflow_name : str
        Workflow file name, such as ``"ci.yml"``.
    job_name : str
        Job key within that workflow.

    Returns
    -------
    str
        The template as parsed, which may still contain ``${{ ... }}``.

    Notes
    -----
    An unnamed job's context is its key, which is a different question from
    the one this module renders, so a job that declares no string ``name``
    fails the contract rather than rendering as empty.
    """
    declared = job(workflow_name, job_name).get("name")
    require(
        condition=isinstance(declared, str),
        message=(
            f"{workflow_name}:{job_name} must declare a string name, got "
            f"{declared!r}; the check context is what a reviewer reads"
        ),
    )
    return typ.cast("str", declared)


def render_name(
    workflow_name: str, job_name: str, leg: cabc.Mapping[str, object]
) -> str:
    """Render one job's check name for one matrix leg.

    Parameters
    ----------
    workflow_name : str
        Workflow file name, such as ``"ci.yml"``.
    job_name : str
        Job key within that workflow.
    leg : Mapping of str to object
        One leg, as `ci_leg_matrix.matrix_legs` returns it.

    Returns
    -------
    str
        The name GitHub would display for that leg.

    Notes
    -----
    A template this module cannot read, or a leg missing the field it names,
    is reported by :func:`render_template`.
    """
    return render_template(
        declared_name(workflow_name, job_name),
        leg,
        subject=f"{workflow_name}:{job_name} name",
    )


def rendered_names(
    workflow_name: str, job_name: str
) -> list[tuple[dict[str, object], str]]:
    """Render a job's check name for every leg its matrix declares.

    Parameters
    ----------
    workflow_name : str
        Workflow file name, such as ``"ci.yml"``.
    job_name : str
        Job key within that workflow.

    Returns
    -------
    list of (dict, str)
        One ``(leg, name)`` pair per matrix entry, in declaration order, so a
        caller can assert the exact name and the leg that produces it
        together.
    """
    return [
        (leg, render_name(workflow_name, job_name, leg))
        for leg in matrix_legs(workflow_name, job_name)
    ]


def render_template(
    template: object, leg: cabc.Mapping[str, object], *, subject: str
) -> str:
    """Render one name template against one leg.

    Parameters
    ----------
    template : object
        The declared ``name:`` value as parsed.
    leg : Mapping of str to object
        One leg, as `ci_leg_matrix.matrix_legs` returns it.
    subject : str
        What the template belongs to, named in every failure so a reader is
        sent to the declaration rather than to this helper.

    Returns
    -------
    str
        Literal text with each supported expression replaced by its value.

    Notes
    -----
    The contract fails when the template is not a string, contains a line
    break, leaves an unreadable ``${{ ... }}`` behind, or reads a field the
    leg does not declare. A name that still carried an expression would
    compare unequal to every rendered expectation while saying nothing about
    the declaration, and a missing field would render as the Python ``None``
    where GitHub renders null as empty text.
    """
    require(
        condition=isinstance(template, str),
        message=f"{subject} must be a string template, got {template!r}",
    )
    text = typ.cast("str", template)
    require(
        condition="\n" not in text and "\r" not in text,
        message=(
            f"{subject} carries a line break: a folded scalar's more-indented "
            "continuation keeps its break and puts a newline inside the "
            "expression, which GitHub reads anyway and no assertion here can"
        ),
    )
    result = "".join(_segments(text, leg, subject=subject))
    require(
        condition="${{" not in result and "}}" not in result,
        message=(
            f"{subject} leaves an unbalanced expression in {result!r}; only "
            "whole `${{ ... }}` expressions are rendered, and a stray delimiter "
            "would pass through as text"
        ),
    )
    return result


def _segments(
    text: str, leg: cabc.Mapping[str, object], *, subject: str
) -> cabc.Iterator[str]:
    """Yield the literal and rendered pieces of one template, in order."""
    cursor = 0
    for match in _EXPRESSION.finditer(text):
        yield text[cursor : match.start()]
        yield _render_expression(match.group("body"), leg, subject=subject)
        cursor = match.end()
    yield text[cursor:]


def _render_expression(
    body: str, leg: cabc.Mapping[str, object], *, subject: str
) -> str:
    """Render one expression body, refusing the shapes this reader cannot model."""
    require(
        condition="github." not in body.lower(),
        message=(
            f"{subject} reads {body!r}, which names the event; a check context "
            "that follows the event has no single value across a fork's pull "
            "request and an internal one, so no required context exists"
        ),
    )
    choice = _MATRIX_CHOICE.match(body)
    if choice is not None:
        if _truthy(_leg_value(leg, choice.group("field"), subject=subject)):
            # `x && '' || otherwise` falls through: GitHub's `||` reads the
            # empty first operand as falsy and returns the second.
            return choice.group("when") or choice.group("otherwise")
        return choice.group("otherwise")
    bare = _BARE_MATRIX.match(body)
    if bare is not None:
        value = _leg_value(leg, bare.group("field"), subject=subject)
        require(
            condition=isinstance(value, str),
            message=(
                f"{subject} interpolates matrix.{bare.group('field')} as text, "
                f"but the leg declares {value!r}; refuse the value rather than "
                "render Python's spelling of it"
            ),
        )
        return typ.cast("str", value)
    message = (
        f"{subject} reads {body!r}, which this renderer cannot resolve; it "
        "renders only `matrix.<field>` and "
        "`matrix.<field> && '<literal>' || ''`, and copying an unread "
        "expression into the result would compare a name with its own template"
    )
    raise AssertionError(message)


def _truthy(value: object) -> bool:
    """Return GitHub's boolean coercion of one leg value.

    GitHub's expressions treat ``false``, ``0``, ``-0``, ``NaN``, ``null`` and
    the empty string as falsy, and every other value — including an empty
    array or object — as truthy. Python's own truthiness differs for ``NaN``
    and for empty containers, and a divergence here would render the branch
    GitHub does not select.

    Returns
    -------
    bool
        Whether GitHub's expressions would coerce the value to true.
    """
    match value:
        case bool():
            return value
        case float() if math.isnan(value):
            return False
        case int() | float():
            return value != 0
        case str():
            return bool(value)
        case None:
            return False
        case _:
            return True


def _leg_value(leg: cabc.Mapping[str, object], field: str, *, subject: str) -> object:
    """Return one leg's value for a field, refusing a field it does not carry."""
    require(
        condition=field in leg,
        message=(
            f"{subject} reads matrix.{field}, which the leg does not declare. "
            "GitHub reads an absent property as null, so rendering it as a "
            "value would invent text the runner never shows"
        ),
    )
    return leg[field]
