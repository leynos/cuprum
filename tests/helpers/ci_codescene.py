"""Find CodeScene contact and credential references in parsed workflows.

The CodeScene boundary contracts ask two questions of every workflow a pull
request can reach: does it contact CodeScene, and can it read the token? Both
used to be asked of named places, a step list and two ``env`` scopes, and every
hole found across the estate was a place nobody named: a ``run`` body, an
action input, a step-level ``env``, a ``secrets:`` forwarding (vk #270,
ortho-config #502). The readers here walk the whole parsed document instead,
keys and string values alike, and report where each finding sits.

The walk reads the parsed document rather than the text, so a comment is never
a finding, and it reads every key as well as every value, so a token forwarded
under ``secrets:`` or declared as an ``env`` name is found without naming either
scope.

Scope: detection over documents a caller supplies. Which documents to read is
:mod:`tests.helpers.ci_closure`'s question.
"""

from __future__ import annotations

import re
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: The name of the secret the CodeScene action authenticates with; a name, not
#: a credential. GitHub resolves secret names case-insensitively, so every
#: match below ignores case.
CREDENTIAL_NAME: typ.Final = "CS_ACCESS_TOKEN"

#: Any mention of CodeScene or its client. The shared action's path, the
#: ``codescene.io`` API host, and the ``cs-coverage`` binary all match.
_CONTACT = re.compile(r"codescene|cs-coverage", re.IGNORECASE)

#: Expressions that hand a step every secret at once.
_ALL_SECRETS = re.compile(r"toJSON\s*\(\s*secrets\s*\)", re.IGNORECASE)


def _require(*, condition: bool, message: str) -> None:
    """Raise a contract failure when ``condition`` does not hold."""
    if not condition:
        raise AssertionError(message)


def walk(value: object, where: str) -> cabc.Iterator[tuple[str, str]]:
    """Yield every mapping key and string value with its location.

    Yields
    ------
    tuple[str, str]
        A dotted location within the document and the text found there.

    Examples
    --------
    >>> list(walk({"env": {"A": "x"}}, "ci.yml"))
    [('ci.yml', 'env'), ('ci.yml.env', 'A'), ('ci.yml.env.A', 'x')]
    """
    match value:
        case dict():
            for key, child in typ.cast("dict[object, object]", value).items():
                yield where, str(key)
                yield from walk(child, f"{where}.{key}")
        case list():
            for index, child in enumerate(typ.cast("list[object]", value)):
                yield from walk(child, f"{where}[{index}]")
        case str():
            yield where, value
        case _:
            pass


def _inherits_secrets(document: object) -> list[str]:
    """Return the jobs of one workflow that forward every secret."""
    jobs = document.get("jobs") if isinstance(document, dict) else None
    if not isinstance(jobs, dict):
        return []
    return [
        str(name)
        for name, job in typ.cast("dict[object, object]", jobs).items()
        if isinstance(job, dict) and job.get("secrets") == "inherit"
    ]


def token_findings(documents: cabc.Mapping[str, object]) -> list[str]:
    """Return every place the documents name the token or forward all secrets.

    A name anywhere counts: an ``env`` key, a ``secrets:`` forwarding, a
    ``${{ secrets.CS_ACCESS_TOKEN }}`` in a ``run`` body or an input, and a
    guard reading ``env.CS_ACCESS_TOKEN``. ``secrets: inherit`` and
    ``toJSON(secrets)`` count too, since they hand over the token without
    naming it.

    Returns
    -------
    list[str]
        One description per finding; empty when the documents are clean.

    Examples
    --------
    >>> token_findings({"x.yml": {"jobs": {"j": {"secrets": "inherit"}}}})
    ['x.yml:j forwards every secret with secrets: inherit']
    """
    findings = [
        f"{where} names {text!r}"
        for name, document in documents.items()
        for where, text in walk(document, name)
        if CREDENTIAL_NAME.lower() in text.lower() or _ALL_SECRETS.search(text)
    ]
    findings.extend(
        f"{name}:{job} forwards every secret with secrets: inherit"
        for name, document in documents.items()
        for job in _inherits_secrets(document)
    )
    return findings


def contact_findings(documents: cabc.Mapping[str, object]) -> list[str]:
    """Return every place the documents mention CodeScene or its client.

    Returns
    -------
    list[str]
        One description per finding; empty when the documents are clean.

    Examples
    --------
    >>> contact_findings({"x.yml": {"run": "curl https://api.codescene.io/"}})
    ["x.yml.run mentions 'curl https://api.codescene.io/'"]
    """
    return [
        f"{where} mentions {text!r}"
        for name, document in documents.items()
        for where, text in walk(document, name)
        if _CONTACT.search(text)
    ]


#: The conjuncts the publisher's upload guard must contain, as written after
#: whitespace normalization. Extra narrowing conjuncts are permitted, which is
#: why an unquoted ``||`` must be refused outright: hidden in an extra
#: conjunct it leaves both of these whole and still makes them optional.
UPLOAD_GUARD: typ.Final = frozenset({
    "env.CS_ACCESS_TOKEN != ''",
    "github.ref == 'refs/heads/main'",
})


def missing_upload_conjuncts(condition: object) -> list[str]:
    """Return the required upload-guard conjuncts ``condition`` lacks.

    Returns
    -------
    list[str]
        The missing conjuncts, sorted; empty when the guard is complete.

    Examples
    --------
    >>> missing_upload_conjuncts("env.CS_ACCESS_TOKEN != ''")
    ["github.ref == 'refs/heads/main'"]
    """
    return sorted(UPLOAD_GUARD - guard_conjuncts(condition))


def _unquoted(condition: str) -> str:
    """Return ``condition`` with the contents of its quoted literals blanked."""
    return re.sub(r"'(?:[^']|'')*'", "''", condition)


def guard_conjuncts(condition: object) -> frozenset[str]:
    """Split a step guard into its ``&&`` conjuncts, refusing any ``||``.

    A substring check that a guard mentions the main ref passes for
    ``... && github.ref == 'refs/heads/main' || github.event_name ==
    'workflow_dispatch'``, which makes every conjunct optional. Splitting on
    ``&&`` and refusing an unquoted ``||`` is what makes each conjunct
    required. Whitespace inside a conjunct is normalized and an enclosing
    ``${{ }}`` is removed.

    Returns
    -------
    frozenset[str]
        The conjuncts, each with its whitespace normalized.

    Notes
    -----
    Fails the contract when the guard is not a string or contains ``||``
    outside a literal.

    Examples
    --------
    >>> sorted(guard_conjuncts("${{ a == 'x' &&  b }}"))
    ["a == 'x'", 'b']
    """
    _require(
        condition=isinstance(condition, str),
        message=f"the guard must be an expression, got {condition!r}",
    )
    text = " ".join(str(condition).split())
    wrapped = re.fullmatch(r"\$\{\{(?P<body>.*)\}\}", text)
    if wrapped is not None:
        text = wrapped.group("body").strip()
    _require(
        condition="||" not in _unquoted(text),
        message=f"the guard {condition!r} must not contain ||",
    )
    return frozenset(term.strip() for term in text.split("&&"))
