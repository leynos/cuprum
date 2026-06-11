"""Property-based tests for ``ProgramCatalogue`` indexing.

These tests exercise catalogue construction over randomly generated
ownership graphs instead of the fixed examples in ``test_catalogue.py``.
Catalogue construction is pure data shuffling, so Hypothesis can explore
the input domain cheaply.

The invariants checked here are:

- For any set of projects with unique names and disjoint program sets,
  construction succeeds, the allowlist is exactly the union of all
  project programs, and ``lookup``/``project_for``/``is_allowed`` agree
  with the generated ownership graph.
- Programs outside the generated universe are rejected uniformly:
  ``is_allowed`` returns False and ``lookup`` raises
  ``UnknownProgramError``.
- A repeated project name is rejected with ``DuplicateProjectError``
  carrying the duplicated name, regardless of where the duplicate sits
  in the iterable.
- A program owned by two projects is rejected with
  ``DuplicateProgramError`` carrying the contested program and the name
  of the project that registered it first.
"""

from __future__ import annotations

import typing as typ
from itertools import starmap

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum.catalogue import (
    DuplicateProgramError,
    DuplicateProjectError,
    ProgramCatalogue,
    ProjectSettings,
    UnknownProgramError,
)
from cuprum.program import Program

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# Small alphabets keep the namespace bounded so generated graphs overlap
# and shrink well, while still covering multi-character names.
_NAME_ALPHABET = "abcdef-"
_PROJECT_NAMES = st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=8)
_PROGRAM_NAMES = st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=8)


def _make_project(name: str, programs: cabc.Sequence[str]) -> ProjectSettings:
    """Build minimal ``ProjectSettings`` owning the given program names."""
    return ProjectSettings(
        name=name,
        programs=tuple(Program(p) for p in programs),
        documentation_locations=(f"https://example.test/{name}",),
        noise_rules=(rf"^{name}:",),
    )


@st.composite
def _ownership_graphs(
    draw: st.DrawFn,
) -> tuple[ProjectSettings, ...]:
    """Generate projects with unique names and disjoint program sets."""
    names = draw(
        st.lists(_PROJECT_NAMES, min_size=1, max_size=5, unique=True),
    )
    programs = draw(
        st.lists(
            _PROGRAM_NAMES,
            max_size=12,
            unique=True,
        ),
    )
    # Partition the program pool across projects; some projects may end
    # up empty, which is a valid (if useless) configuration.
    assignments: dict[str, list[str]] = {name: [] for name in names}
    for program in programs:
        owner = draw(st.sampled_from(names))
        assignments[owner].append(program)
    return tuple(starmap(_make_project, assignments.items()))


@settings(max_examples=200)
@given(projects=_ownership_graphs())
def test_catalogue_indexes_match_generated_ownership(
    projects: tuple[ProjectSettings, ...],
) -> None:
    """Construction over a valid graph reproduces the ownership exactly."""
    catalogue = ProgramCatalogue(projects=projects)
    expected = {
        program: project for project in projects for program in project.programs
    }
    assert catalogue.allowlist == frozenset(expected), (
        "Allowlist must equal the union of project programs"
    )
    for program, project in expected.items():
        assert catalogue.is_allowed(program), "Owned program must be allowed"
        entry = catalogue.lookup(program)
        assert entry.program == program, "Lookup must return the queried program"
        assert entry.project is project, "Lookup must attach the owning project"
        assert catalogue.project_for(program) is project, (
            "project_for must agree with lookup"
        )
    visible = catalogue.visible_settings()
    assert dict(visible) == {project.name: project for project in projects}, (
        "visible_settings must expose every project by name"
    )


@settings(max_examples=200)
@given(projects=_ownership_graphs(), data=st.data())
def test_programs_outside_graph_are_rejected(
    projects: tuple[ProjectSettings, ...],
    data: st.DataObject,
) -> None:
    """Programs not in the generated universe are uniformly blocked."""
    catalogue = ProgramCatalogue(projects=projects)
    owned = {str(p) for project in projects for p in project.programs}
    unknown = data.draw(
        _PROGRAM_NAMES.filter(lambda name: name not in owned),
        label="unknown program",
    )
    assert not catalogue.is_allowed(unknown), "Unknown program must not be allowed"
    with pytest.raises(UnknownProgramError):
        catalogue.lookup(unknown)


@settings(max_examples=200)
@given(projects=_ownership_graphs(), data=st.data())
def test_duplicate_project_name_raises_structured_error(
    projects: tuple[ProjectSettings, ...],
    data: st.DataObject,
) -> None:
    """Re-registering any project name fails with the duplicated name."""
    duplicated = data.draw(
        st.sampled_from([project.name for project in projects]),
        label="duplicated project",
    )
    clone = _make_project(duplicated, [])
    position = data.draw(
        st.integers(min_value=0, max_value=len(projects)),
        label="insertion position",
    )
    sequence = [*projects[:position], clone, *projects[position:]]
    with pytest.raises(DuplicateProjectError) as exc:
        ProgramCatalogue(projects=sequence)
    assert exc.value.project_name == duplicated, (
        "Error must carry the duplicated project name"
    )


@settings(max_examples=200)
@given(projects=_ownership_graphs(), data=st.data())
def test_duplicate_program_ownership_raises_structured_error(
    projects: tuple[ProjectSettings, ...],
    data: st.DataObject,
) -> None:
    """A program owned by two projects fails, naming the first owner."""
    owned = [
        (program, project.name) for project in projects for program in project.programs
    ]
    if not owned:
        return  # Graph generated no programs; nothing to contest.
    contested, owner_name = data.draw(
        st.sampled_from(owned),
        label="contested program",
    )
    rival = _make_project("rival-project", [str(contested)])
    with pytest.raises(DuplicateProgramError) as exc:
        ProgramCatalogue(projects=(*projects, rival))
    assert exc.value.program == contested, "Error must carry the contested program"
    assert exc.value.owner == owner_name, (
        "Error must name the project that registered the program first"
    )
