"""Sweep every ``run:`` script out of the CI workflows.

``tests/helpers/ci_workflows.py`` answers questions about a workflow, a job, or
a step that the caller already knows how to name. Some contracts ask a question
of all of them at once: the selector guard asks which step runs ``make
test-python``, and the same shape answers any "does a step do X anywhere".
Answering it means walking workflows, then jobs, then steps — the nesting a
single contract should not have to spell out, and the nesting CodeScene's
complexity rules flag.

So the walk lives here, once, and returns the location with each script.

The split follows ``workflow_shell`` and ``workflow_recipe``: reading a thing a
caller named, versus sweeping for the things nobody named. It also keeps both
modules within the line budget ``AGENTS.md`` sets and the lint gate enforces.
"""

from __future__ import annotations

import typing as typ

from tests.helpers.ci_documents import document_jobs, narrow_steps, parse_document
from tests.helpers.ci_workflows import WORKFLOW_DIR, workflow_sources

if typ.TYPE_CHECKING:
    import pathlib as pth

    from tests.helpers.workflow_types import Job


def _job_run_scripts(job_payload: object, where: str) -> list[tuple[str, str]]:
    """Return one job's ``(step index, script)`` pairs for its ``run:`` steps.

    A reusable-workflow call declares ``uses:`` instead of ``steps:``, so it
    holds no ``run:`` script and contributes nothing; a job whose ``steps:``
    is declared but malformed is reported by `narrow_steps` rather than
    passed over as empty.

    Parameters
    ----------
    job_payload : object
        One job mapping, as `document_jobs` values it.
    where : str
        Location to cite in a diagnostic, as ``workflow:job``.

    Returns
    -------
    list[tuple[str, str]]
        One pair per ``run:`` step, in declaration order, with the index
        rendered as a string so a location reads the same in a message as it
        does in the YAML.
    """
    narrow = typ.cast("Job", job_payload)
    return [
        (str(index), script)
        for index, step in enumerate(narrow_steps(narrow, where))
        if isinstance(script := step.get("run"), str)
    ]


def run_scripts(
    directory: pth.Path = WORKFLOW_DIR,
) -> list[tuple[str, str, str, str]]:
    """Return every ``run:`` script, flattened with the location that holds it.

    Each workflow is read and parsed once, in the sweep, and every question is
    then asked of that parsed document. Resolving the file name again per job
    would look the workflow up under a fixed directory instead of the one the
    caller supplied, so a sweep of a temporary directory would check this
    repository's workflows and report the wrong estate.

    Parameters
    ----------
    directory : pathlib.Path
        The directory to sweep, as `workflow_sources` takes it. The loader
        tests pass a temporary one to show that an unreadable file fails by
        name.

    Returns
    -------
    list[tuple[str, str, str, str]]
        One ``(workflow, job, step index, script)`` tuple per ``run:`` step,
        in file order. Steps that declare no ``run:`` — ``uses:`` steps, and
        any step keyed by something else — contribute nothing.

    Raises
    ------
    AssertionError
        If the directory holds no workflow, or if a workflow, job, or step is
        not the shape `ci_documents` narrows it to. An empty sweep would
        satisfy every "no step does X" contract vacuously, so the sweep
        reports its own emptiness rather than returning an empty list.
    """  # ruff: ignore[docstring-extraneous-exception] - raised by the ci_documents accessors this composes
    found: list[tuple[str, str, str, str]] = []
    for workflow_name, source in workflow_sources(directory):
        document = parse_document(source, workflow_name)
        for job_name, job_payload in document_jobs(document, workflow_name).items():
            where = f"{workflow_name}:{job_name}"
            found.extend(
                (workflow_name, job_name, index, script)
                for index, script in _job_run_scripts(job_payload, where)
            )
    return found
