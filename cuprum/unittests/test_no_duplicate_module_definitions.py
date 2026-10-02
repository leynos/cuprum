"""Guard against a definition silently shadowing a sibling of the same name.

A block that defines the same name twice keeps only the second binding, so a
duplicated helper is silent at import time, silent at call time, and visible
only as the wrong body running. Nothing else catches it here: Ruff's F811 and
Pylint's E0102 both decline to report redefinitions of underscore-prefixed
names, and every private helper in this codebase is underscore-prefixed, so
the whole gate suite is blind to the defect on exactly the names it would
matter for. Verified against Ruff 0.16.4 and Pylint 4.0.9; upstream Pyflakes
does report it, so this is a deviation in the two linters actually installed.

The unit of the check is one statement list, not one module. Two definitions
are compared only when they are siblings in the same body, which is precisely
when the earlier one becomes unreachable. That leaves the branch idiom alone
without needing to special-case it::

    if sys.platform == "win32":
        def _probe() -> str: ...
    else:
        def _probe() -> str: ...

Each arm is its own statement list, so neither shadows the other and both
remain reachable. Same-named definitions in one arm would still be reported.
The rule also applies inside classes and functions, where a repeated sibling
is just as dead as at module scope.

Example
-------
pytest cuprum/unittests/test_no_duplicate_module_definitions.py
"""

from __future__ import annotations

import ast
import dataclasses as dc
import pathlib
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: Directories scanned, relative to the repository root: the shipped package
#: and the two test tiers that run under ``make test``. The ``benchmarks``
#: tree is helper code rather than shipped behaviour and is not gated.
_SCANNED_ROOTS: typ.Final[tuple[str, ...]] = (
    "cuprum",
    "scripts",
    "tests",
)

#: ``__pycache__`` holds no source, and a stale bytecode tree would only slow
#: the walk down.
_EXCLUDED_DIRECTORIES: typ.Final[frozenset[str]] = frozenset({"__pycache__"})

#: A definition site: the name it binds, the line it starts on, and the kind
#: of statement binding it.
_DefinitionSite = tuple[str, int, str]


class _DuplicateScanError(Exception):
    """Raised when a module cannot be read or parsed by the guard.

    Attributes
    ----------
    path : pathlib.Path
        The module the scan could not inspect.

    """

    def __init__(self, path: pathlib.Path) -> None:
        """Describe the module that could not be inspected."""
        message = f"cannot inspect {path} for shadowed sibling definitions"
        super().__init__(message)
        self.path = path


def _repository_root() -> pathlib.Path:
    """Return the repository root, resolved from this test module's location."""
    # ``cuprum/unittests/test_...py`` -> the root is three parents up.
    return pathlib.Path(__file__).resolve().parents[2]


def _module_sources() -> cabc.Iterator[pathlib.Path]:
    """Yield every Python module the guard scans.

    Yields
    ------
    pathlib.Path
        The next module to inspect.

    """
    root = _repository_root()
    for scanned_root in _SCANNED_ROOTS:
        for path in sorted((root / scanned_root).rglob("*.py")):
            if not _EXCLUDED_DIRECTORIES.intersection(path.parts):
                yield path


def _definition_kind(node: ast.stmt) -> str:
    """Describe a definition statement's kind for the failure message.

    Parameters
    ----------
    node : ast.stmt
        A statement that binds a name.

    Returns
    -------
    str
        The kind, ahead of the line number in the failure message.

    """
    if isinstance(node, ast.ClassDef):
        return "class"
    if isinstance(node, ast.AsyncFunctionDef):
        return "async def"
    return "def"


def _statement_lists(node: ast.AST) -> cabc.Iterator[list[ast.stmt]]:
    """Yield every statement list *node* directly owns.

    A branch node owns more than one list — ``orelse`` for an ``if``, the
    ``handler.body`` of each ``except`` — and each is a separate scope for the
    purposes of sibling shadowing, so all of them are visited.

    Parameters
    ----------
    node : ast.AST
        The node whose statement lists are wanted.

    Yields
    ------
    list[ast.stmt]
        The next list of statements directly inside *node*.

    """
    for field_name in ("body", "orelse", "finalbody"):
        value = getattr(node, field_name, None)
        if isinstance(value, list):
            yield value
    for handler in getattr(node, "handlers", ()):
        yield handler.body
    for case in getattr(node, "cases", ()):
        yield case.body


def _shadowed_in(body: list[ast.stmt]) -> list[tuple[str, tuple[_DefinitionSite, ...]]]:
    """Return the names *body* defines more than once as siblings.

    Parameters
    ----------
    body : list[ast.stmt]
        One statement list.

    Returns
    -------
    list[tuple[str, tuple[_DefinitionSite, ...]]]
        The shadowed names, each with every sibling site defining it.

    """
    grouped: dict[str, list[_DefinitionSite]] = {}
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            grouped.setdefault(node.name, []).append((
                node.name,
                node.lineno,
                _definition_kind(node),
            ))
    return [(name, tuple(sites)) for name, sites in grouped.items() if len(sites) > 1]


@dc.dataclass(frozen=True, slots=True)
class _Shadowing:
    """A name a single statement list binds more than once.

    Attributes
    ----------
    scope : str
        The enclosing construct, for the failure message.
    name : str
        The rebind name.
    sites : tuple[_DefinitionSite, ...]
        Every sibling site defining it, in source order.

    """

    scope: str
    name: str
    sites: tuple[_DefinitionSite, ...]

    def describe(self) -> str:
        """Render the shadowing as one failure-message line.

        Returns
        -------
        str
            The name, its enclosing construct, and each defining site.

        """
        rendered = ", ".join(
            f"{kind} at line {lineno}" for _, lineno, kind in self.sites
        )
        return f"{self.name} in {self.scope}: {rendered}"


def _scope_label(node: ast.AST) -> str:
    """Name the construct owning a statement list, for a failure message.

    Parameters
    ----------
    node : ast.AST
        The node owning one or more statement lists.

    Returns
    -------
    str
        A human-readable label for the construct.

    """
    if isinstance(node, ast.Module):
        return "module scope"
    # Every node owning a statement list binds its name as a plain string:
    # ``If`` and ``Try`` carry none, functions and classes carry ``str``.
    # ``TypeAlias`` names are AST nodes, but it owns no statement list.
    name = getattr(node, "name", None)
    if name is None:
        return type(node).__name__
    return f"{type(node).__name__} {name!r}"


def _find_shadowing(source: str, *, filename: str) -> tuple[_Shadowing, ...]:
    """Return every sibling definition shadowed by a later one in *source*.

    Parameters
    ----------
    source : str
        The module's text.
    filename : str
        The path used in syntax-error messages.

    Returns
    -------
    tuple[_Shadowing, ...]
        The shadowed definitions, one entry per name per statement list.

    """
    tree = ast.parse(source, filename=filename)
    return tuple(
        _Shadowing(_scope_label(node), name, sites)
        # Each node owns its statement lists outright, so visiting every node
        # reaches each list exactly once.
        for node in ast.walk(tree)
        for body in _statement_lists(node)
        for name, sites in _shadowed_in(body)
    )


@pytest.mark.parametrize(
    ("malformed_source", "expected_scope", "expected_lines"),
    [
        (
            "def _helper():\n    return 1\n\n\ndef _helper():\n    return 2\n",
            "module scope",
            [1, 5],
        ),
        (
            "class _Record:\n    pass\n\n\nclass _Record:\n    pass\n",
            "module scope",
            [1, 5],
        ),
        (
            "def helper():\n    return 1\n\n\ndef helper():\n    return 2\n",
            "module scope",
            [1, 5],
        ),
        (
            (
                "class Holder:\n"
                "    def _step(self):\n"
                "        return 1\n"
                "\n"
                "    def _step(self):\n"
                "        return 2\n"
            ),
            "ClassDef 'Holder'",
            [2, 5],
        ),
    ],
    ids=[
        "private-function",
        "private-class",
        "public-function",
        "method-shadowed-in-class",
    ],
)
def test_guard_reports_shadowed_siblings(
    malformed_source: str,
    expected_scope: str,
    expected_lines: list[int],
) -> None:
    """The guard sees the redefinitions the installed linters miss.

    Without this the guard could pass for the wrong reason: a scan that had
    quietly stopped descending into class bodies, or stopped matching
    underscore-prefixed names, would still report a clean tree. Only positive
    controls distinguish the two.
    """
    found = _find_shadowing(malformed_source, filename="probe.py")

    assert len(found) == 1, f"expected exactly one shadowed name, got {found}"
    assert found[0].scope == expected_scope, (
        f"expected scope {expected_scope!r}, got {found[0].scope!r}"
    )
    assert [site[1] for site in found[0].sites] == expected_lines, (
        f"expected defining lines {expected_lines}, got "
        f"{[site[1] for site in found[0].sites]}"
    )


def test_guard_allows_a_definition_in_each_branch_arm() -> None:
    """One definition per branch arm is two scopes, not a shadowed sibling.

    This is the false-positive boundary the guard is drawn around, and the
    reason the unit of the check is a statement list rather than a module.
    """
    source = (
        "import sys\n"
        "\n"
        "if sys.platform == 'win32':\n"
        "    def _probe():\n"
        "        return 'win'\n"
        "else:\n"
        "    def _probe():\n"
        "        return 'posix'\n"
    )

    assert not _find_shadowing(source, filename="probe.py"), (
        "one definition per branch arm is two scopes, not a shadowed sibling"
    )


def test_guard_allows_a_nested_definition_shadowing_its_outer_name() -> None:
    """A nested definition of an outer name rebinds a different scope.

    Both remain reachable — the outer from anywhere, the inner from its own
    enclosing function — so this is not the defect the guard exists to find.
    """
    source = (
        "def _helper():\n    def _helper():\n        return 2\n    return _helper()\n"
    )

    assert not _find_shadowing(source, filename="probe.py"), (
        "a nested definition rebinds a different scope and both stay reachable"
    )


def test_no_source_binds_a_name_twice_in_one_scope() -> None:
    """No scanned statement list defines the same name more than once.

    A block that does keeps only the last definition, so the earlier body is
    unreachable and the failure surfaces as wrong behaviour rather than a
    crash. See the module docstring for why no existing gate reports it.

    Raises
    ------
    _DuplicateScanError
        If a scanned module cannot be read or parsed.

    """
    offenders: list[str] = []
    for path in _module_sources():
        try:
            source = path.read_text(encoding="utf-8")
            found = _find_shadowing(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            raise _DuplicateScanError(path) from exc
        relative = path.relative_to(_repository_root())
        offenders.extend(f"  {relative}: {shadowing.describe()}" for shadowing in found)

    assert not offenders, "sibling definitions shadowed by a later one:\n" + "\n".join(
        offenders
    )
