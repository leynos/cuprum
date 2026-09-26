"""Parse CI documents, and narrow the shapes the contracts rely on.

``tests/helpers/ci_workflows.py`` answers questions about a repository
workflow: it resolves a file name under ``.github/workflows`` and reads it
back. Answering those questions needs a layer underneath that works on text
and on parsed documents instead — parse this source, narrow that job to its
steps — which is what lives here. A sweep that has already read every file
calls into this module directly, so it never has to name a file twice.

The split is the one ``workflow_shell`` and ``workflow_recipe`` already
document: operating on a thing the caller already holds, versus resolving and
reading a thing it named. It also keeps both modules within the line budget
``AGENTS.md`` sets and the lint gate enforces.

Every narrowing here validates the shape it narrows, so a malformed document
fails with a named diagnostic rather than an opaque ``TypeError`` deep in a
test. In particular, a job that declares no ``steps:`` and a job whose
``steps:`` is the wrong shape are different findings, not the same one: see
:func:`narrow_steps`.
"""

from __future__ import annotations

import typing as typ

from tests.helpers import strict_yaml

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Job, Step

__all__ = (
    "cache_paths",
    "document_jobs",
    "narrow_steps",
    "parse_document",
    "step_inputs",
)


def require(*, condition: bool, message: str) -> None:
    """Raise a contract failure when ``condition`` does not hold."""
    if not condition:
        raise AssertionError(message)


def mapping(value: object, message: str) -> dict[str, object]:
    """Narrow a parsed YAML value to a string-keyed mapping."""
    require(
        condition=isinstance(value, dict)
        and all(isinstance(key, str) for key in value),
        message=message,
    )
    return typ.cast("dict[str, object]", value)


def parse_document(source: str, workflow_name: str) -> dict[object, object]:
    """Parse workflow source text, without narrowing its top-level keys.

    Parameters
    ----------
    source : str
        The workflow's YAML text, as `read_source` returns it.
    workflow_name : str
        The name to cite in a diagnostic. Taking the text rather than the path
        lets a caller that already holds the source — a sweep, which reads
        every file before deciding what to ask of it — parse each document
        without a second read, and without resolving the name against a
        directory the caller did not supply.

    Returns
    -------
    dict[object, object]
        The parsed document, whose trigger key YAML 1.1 reads as ``True``.

    Notes
    -----
    Fails the contract when the text is not valid YAML, declares a mapping key
    twice, or does not parse to a mapping, naming the file in each case.
    """
    document = strict_yaml.load(source, workflow_name)
    # YAML 1.1 reads the `on:` trigger key as the boolean `True`, so the
    # document is genuinely not string-keyed and the return type says so.
    # Claiming `dict[str, object]` here would be a false contract that hides
    # the one key a caller cannot reach by name. Callers that need a
    # string-keyed mapping narrow one of its values through `mapping`.
    require(
        condition=isinstance(document, dict),
        message=f"{workflow_name} must parse to a mapping",
    )
    return typ.cast("dict[object, object]", document)


def document_jobs(
    document: dict[object, object], workflow_name: str
) -> dict[str, object]:
    """Load the jobs mapping from an already-parsed workflow document.

    Taking the document rather than a file name lets a caller that has already
    read and parsed the workflows — a sweep, which reads every file before
    deciding what to ask of any — narrow each one from that same document. A
    caller that re-resolved the name would read a second copy from a fixed
    directory, so a sweep handed a different one would list that directory's
    files and then ask questions of this repository's.

    Parameters
    ----------
    document : dict[object, object]
        A parsed workflow, as `parse_document` returns it.
    workflow_name : str
        The name to cite in a diagnostic.

    Returns
    -------
    dict of str to object
        The workflow's jobs, keyed by job name.
    """
    return mapping(
        document.get("jobs"),
        f"{workflow_name} must declare jobs",
    )


def narrow_steps(payload: Job, where: str) -> list[Step]:
    """Return the steps of one job, or report why it has none to give.

    A reusable-workflow call declares ``uses:`` where a step list would go, so
    it has no ``steps:`` key and legitimately yields none. A ``steps:`` key of
    the wrong shape is not the same thing: a mapping, a string, or ``null``
    where a list belongs is a malformed job, and reporting it as "no steps"
    would let every "no step does X" contract over it pass having read
    nothing. The two cases are separated here, at the one place that can tell
    them apart.

    Parameters
    ----------
    payload : Job
        The job to narrow.
    where : str
        Location to cite in a diagnostic, as ``workflow:job``.

    Returns
    -------
    list[Step]
        One narrowed mapping per declared step, in order.
    """
    declared = payload.get("steps")
    if declared is None and "steps" not in payload:
        return []
    require(
        condition=isinstance(declared, list),
        message=f"{where} must declare steps as a list",
    )
    return [
        typ.cast(
            "Step",
            mapping(step, f"{where} step {index} must be a mapping"),
        )
        for index, step in enumerate(typ.cast("list[object]", declared))
    ]


def step_inputs(step: Step, message: str) -> dict[str, object]:
    """Return the ``with`` mapping declared by one workflow step."""
    return mapping(step.get("with"), message)


def cache_paths(step: Step, message: str) -> list[str]:
    """Return the paths a cache step owns, one per line."""
    declared = step_inputs(step, message).get("path")
    require(
        condition=isinstance(declared, str),
        message=f"{message}: paths must be a newline-delimited string",
    )
    paths = [line.strip() for line in str(declared).splitlines() if line.strip()]
    # A cache step with no paths owns nothing, so every ownership assertion
    # over it would hold vacuously. Fail here instead of reporting a clean run.
    require(
        condition=bool(paths),
        message=f"{message}: a cache step must declare at least one path",
    )
    return paths
