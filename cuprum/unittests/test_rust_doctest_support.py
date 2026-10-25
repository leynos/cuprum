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
    compile_fail_count,
    compile_fail_defects,
    doc_comment_groups,
    executable_code,
    imported_names,
    strip_comments,
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


@pytest.mark.parametrize(
    ("opening", "closing"),
    [
        pytest.param("````compile_fail,E0308", "````", id="four-backticks"),
        pytest.param("`````compile_fail,E0308", "``````", id="closing-longer"),
        pytest.param("~~~compile_fail,E0308", "~~~", id="tildes"),
    ],
)
def test_longer_and_tilde_fences_are_scanned(opening: str, closing: str) -> None:
    """A fence rustdoc accepts must not hide a doctest from the contract."""
    source = (
        f'{_CONTROL}/// {opening}\n/// let _: u8 = "";\n/// {closing}\nfn item() {{}}\n'
    )
    assert compile_fail_count(source) == 1, (
        "a longer or tilde fence must be counted as a compile-fail doctest"
    )
    assert compile_fail_defects(source) == [], (
        "a controlled negative case in a longer fence has no defect"
    )


def test_a_longer_fence_is_not_closed_by_a_shorter_or_labelled_line() -> None:
    """Fence-like lines inside a block are content, as CommonMark specifies."""
    source = (
        "/// ````compile_fail,E0308\n"
        "/// ```rust\n"
        "/// ```\n"
        '/// let _: u8 = "";\n'
        "/// ````\n"
        "fn item() {}\n"
    )
    (group,) = doc_comment_groups(source)
    (block,) = code_blocks(group)
    assert block.body == (" ```rust", " ```", ' let _: u8 = "";'), (
        "only a bare fence at least as long as the opening one closes a block"
    )


def test_a_backtick_fence_cannot_carry_a_backtick_in_its_info_string() -> None:
    """Inline code that starts a line is prose, not a fence."""
    (group,) = doc_comment_groups("/// ```inline``` code\nfn item() {}\n")
    assert code_blocks(group) == [], "a backtick in the info string forbids a fence"


@pytest.mark.parametrize(
    "info",
    [
        pytest.param("compile_fail,E0308", id="comma"),
        pytest.param("compile_fail E0308", id="space"),
        pytest.param("compile_fail , E0308", id="space-and-comma"),
        pytest.param("compile_fail\tE0308", id="tab"),
    ],
)
def test_attributes_split_on_commas_and_whitespace(info: str) -> None:
    """Rustdoc accepts either separator, so the scanner must as well."""
    source = _CONTROL + f'/// ```{info}\n/// let _: u8 = "";\n/// ```\nfn item() {{}}\n'
    assert compile_fail_count(source) == 1, f"{info!r} declares a compile-fail doctest"
    assert compile_fail_defects(source) == [], f"{info!r} names its error code"


@pytest.mark.parametrize(
    ("commented", "negative_line"),
    [
        pytest.param("// use krate::b;", 5, id="line-comment"),
        pytest.param("/* use krate::b; */", 5, id="block-comment"),
        pytest.param("/*\n use krate::b;\n*/", 7, id="multiline-block-comment"),
    ],
)
def test_a_commented_out_import_does_not_back_a_negative_case(
    commented: str, negative_line: int
) -> None:
    """Only executable imports count towards the control's coverage."""
    source = _doc(
        ("", f"use krate::a;\n{commented}"),
        ("compile_fail,E0308", "use krate::b;"),
    )
    assert compile_fail_defects(source) == [_unbacked(negative_line, "krate::b")], (
        "a commented-out import must not count as one the control makes"
    )


def test_imports_may_be_split_across_controls() -> None:
    """A rename breaks whichever control imports the name, so a union suffices."""
    source = _doc(
        ("", "use krate::a;"),
        ("", "use krate::b;"),
        ("compile_fail,E0308", "use krate::{a, b};"),
    )
    assert compile_fail_defects(source) == [], (
        "each name is imported by some control, so renaming either one fails a "
        "control and the negative case cannot pass for that reason alone"
    )


def test_a_nested_block_comment_hides_an_import_after_its_inner_close() -> None:
    """Rust block comments nest, so the first ``*/`` does not end the outer one."""
    commented = "/* outer\n   /* inner */\n   use krate::b;\n*/"
    source = _doc(
        ("", f"use krate::a;\n{commented}"),
        ("compile_fail,E0308", "use krate::b;"),
    )
    assert compile_fail_defects(source) == [_unbacked(8, "krate::b")], (
        "an import inside a nested block comment must not back a negative case"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("a // b\nc", "a \nc", id="line-comment-keeps-the-newline"),
        pytest.param("a /* b */ c", "a  c", id="block-comment"),
        pytest.param("a /* x /* y */ z */ c", "a  c", id="nested-block-comment"),
        pytest.param("a /* never closed", "a ", id="unterminated-block-comment"),
        pytest.param('a "// b" c', 'a "// b" c', id="line-marker-in-string"),
        pytest.param('a "/* b" c', 'a "/* b" c', id="block-marker-in-string"),
        pytest.param('a "q\\" // b" c', 'a "q\\" // b" c', id="escaped-quote"),
        pytest.param('r#"/* b "# c', 'r#"/* b "# c', id="raw-string"),
        pytest.param("a '\"' // b", "a '\"' ", id="quote-character-literal"),
    ],
)
def test_comments_are_stripped_and_literals_are_kept(text: str, expected: str) -> None:
    """A comment marker inside another token must not change what is read."""
    assert strip_comments(text) == expected, (
        "comments must be removed with nesting, and string, raw string and "
        "quote-character literals must be left whole"
    )


def test_an_import_after_a_string_holding_a_comment_marker_is_still_read() -> None:
    """A ``/*`` inside a string must not swallow the import that follows it."""
    fence = Fence(1, (), ('let _s = "/*";', "use krate::a;", "// closes */ here"))
    assert imported_names(fence) == {"krate::a"}, (
        "the string literal must be kept whole so the import after it is read"
    )


@pytest.mark.parametrize(
    "literal",
    [
        pytest.param('"use krate::b;"', id="string"),
        pytest.param('r#"use krate::b;"#', id="raw-string"),
        pytest.param('"say \\" use krate::b;"', id="string-with-escaped-quote"),
    ],
)
def test_an_import_inside_a_string_does_not_back_a_negative_case(literal: str) -> None:
    """Text in a string literal is data, so it cannot import anything."""
    source = _doc(
        ("", f"use krate::a;\nlet _s = {literal};"),
        ("compile_fail,E0308", "use krate::b;"),
    )
    assert compile_fail_defects(source) == [_unbacked(5, "krate::b")], (
        "an import-shaped string in a control must not count as an import"
    )


def test_imported_names_ignore_strings_but_keep_the_imports_around_them() -> None:
    """Only executable code is read, whatever literals surround an import."""
    fence = Fence(
        1,
        (),
        ('let _s = "use krate::b;";', "use krate::a;", "let _t = '\"'; use krate::c;"),
    )
    assert imported_names(fence) == {"krate::a", "krate::c"}, (
        "imports outside strings must be read and those inside must not"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param('a "x" // c\nb', 'a "" \nb', id="string-and-line-comment"),
        pytest.param("a /* c */ b", "a  b", id="block-comment"),
        pytest.param('a r#"x"# b', 'a "" b', id="raw-string"),
        pytest.param("a '\"' b", 'a "" b', id="quote-character-literal"),
        pytest.param("plain code;", "plain code;", id="code-is-unchanged"),
    ],
)
def test_executable_code_removes_comments_and_empties_strings(
    text: str, expected: str
) -> None:
    """Comments vanish and string contents are replaced by an empty string."""
    assert executable_code(text) == expected, (
        "only code may remain: comments removed, string literals emptied"
    )
