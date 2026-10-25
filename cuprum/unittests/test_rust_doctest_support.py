"""The doctest scanner refuses a negative case that nothing backs.

Stable rustdoc ignores the error code in ``compile_fail,E0308``, so a negative
case passes for any compile error there, including an import that no longer
resolves. The scanner therefore requires the control in the same doc comment to
import everything the negative case does.
"""

from __future__ import annotations

import pytest

from cuprum.unittests._rust_doctest_support import (
    Fence,
    code_blocks,
    compile_fail_defects,
    doc_comment_groups,
    imported_names,
)

_CONTROL = "/// ```\n/// let _ = 1;\n/// ```\n"
_PINNED = '/// ```compile_fail,E0308\n/// let _: u8 = "";\n/// ```\n'


def _unbacked(line: int, name: str) -> str:
    """Return the defect reported for an import that no control imports."""
    return (
        f"line {line}: imports ['{name}'] that no positive control in the same "
        "doc comment imports"
    )


def _doc(*blocks: tuple[str, str]) -> str:
    """Build one item's doc comment from ``(fence info, body)`` pairs."""
    lines = [
        f"/// ```{info}\n"
        + "".join(f"/// {line}\n" for line in body.splitlines())
        + "/// ```\n"
        for info, body in blocks
    ]
    return "".join(lines) + "fn item() {}\n"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(_CONTROL + _PINNED + "fn item() {}\n", [], id="pinned"),
        pytest.param(
            _CONTROL + _PINNED.replace(",E0308", "") + "fn item() {}\n",
            ["line 4: names no expected error code"],
            id="no-error-code",
        ),
        pytest.param(
            _PINNED + "fn item() {}\n",
            ["line 1: has no positive control"],
            id="no-control",
        ),
        pytest.param(
            _CONTROL + "fn first() {}\n" + _PINNED + "fn second() {}\n",
            ["line 5: has no positive control"],
            id="control-on-another-item",
        ),
        pytest.param(
            _CONTROL.replace("```\n/// let", "```ignore\n/// let", 1)
            + _PINNED
            + "fn item() {}\n",
            ["line 4: has no positive control"],
            id="ignored-control",
        ),
    ],
)
def test_the_scanner_reports_each_structural_defect(
    source: str, expected: list[str]
) -> None:
    """The scanner names a missing error code and a missing control."""
    assert compile_fail_defects(source) == expected, (
        "the scanner must flag a missing error code and a missing control, "
        "and must not accept a control documented on a different item"
    )


@pytest.mark.parametrize(
    ("control_use", "negative_use", "expected"),
    [
        pytest.param(
            "use krate::{a, b, c};", "use krate::{a, b};", [], id="control-superset"
        ),
        pytest.param("use krate::a;", "use krate::a;", [], id="identical"),
        pytest.param(
            "use krate::a;",
            "use krate::{a, b};",
            [_unbacked(4, "krate::b")],
            id="negative-imports-more",
        ),
        pytest.param(
            "use krate::a;",
            "use other::a;",
            [_unbacked(4, "other::a")],
            id="same-name-different-path",
        ),
        pytest.param(
            "use krate::{\n    a,\n    b,\n};",
            "use krate::b;",
            [],
            id="control-wrapped-over-lines",
        ),
        pytest.param(
            "# use krate::a;", "use krate::a;", [], id="control-import-hidden"
        ),
    ],
)
def test_the_scanner_requires_the_control_to_import_what_the_negative_does(
    control_use: str, negative_use: str, expected: list[str]
) -> None:
    """A rename of an import that only the negative case uses is caught."""
    source = _doc(("", control_use), ("compile_fail,E0308", negative_use))
    assert compile_fail_defects(source) == expected, (
        "a negative case must not import a name that no control in its doc "
        "comment imports, whatever form the import takes"
    )


def test_the_scanner_checks_each_negative_case_separately() -> None:
    """Two negative cases in one doc comment are each held to the controls."""
    source = _doc(
        ("", "use krate::a;"),
        ("compile_fail,E0308", "use krate::a;"),
        ("compile_fail,E0308", "use krate::z;"),
    )
    assert compile_fail_defects(source) == [_unbacked(7, "krate::z")], (
        "only the negative case with the unbacked import is reported"
    )


def test_a_block_without_imports_is_backed_by_any_control() -> None:
    """A snippet with no imports has nothing for a rename to break."""
    assert compile_fail_defects(_CONTROL + _PINNED + "fn item() {}\n") == [], (
        "a negative case that imports nothing needs no matching imports"
    )


def test_blocks_report_their_attributes_body_and_opening_line() -> None:
    """The scanner records where a block starts and what it contains."""
    (group,) = doc_comment_groups(_PINNED + "fn item() {}\n")
    assert code_blocks(group) == [
        Fence(1, ("compile_fail", "E0308"), (' let _: u8 = "";',))
    ], "a block keeps its opening line, attributes and trimmed body"


def test_imported_names_expand_braces_and_ignore_whitespace() -> None:
    """Braced lists expand to one full path per item."""
    fence = Fence(1, (), ("use a::b::{c,  d};", "use e::*;"))
    assert imported_names(fence) == {"a::b::c", "a::b::d", "e::*"}, (
        "each braced item must become its own whitespace-free path"
    )
