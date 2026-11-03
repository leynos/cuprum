"""Parse and validate the examples published in Cuprum's documentation.

One definition of a marked fence serves both the behaviour suite that executes
the examples and the tests that probe the parser's edge cases. Keeping it here
leaves the document gate and its unit tests as two focused modules instead of
one module that must hold both responsibilities.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = (
    "README.md",
    "docs/users-guide.md",
    "docs/v0-2-0-migration-guide.md",
    "docs/scripting-standards.md",
)
MARKER = re.compile(
    r"^\s*<!-- (tested|illustrative|shell)-example: ([a-z0-9-]+) -->\s*$"
)
# Python fences must execute; shell fences (installation commands, for
# example) are published for copying and are never run by this suite.
LANGUAGES = {"tested": "python", "shell": "shell"}
# An illustrative fence is a published example whose dependencies or side
# effects make execution unsuitable for a documentation gate. The document
# must state why each one is excluded, as a bullet that names it.
ILLUSTRATIVE = "illustrative"
# Both CommonMark fence characters open a block, so neither can hide an
# example from marker validation.
FENCE_DELIMITERS = ("```", "~~~")


def _has_outcome_assertion(code: str) -> bool:
    """Return whether ``code`` contains an executable ``assert`` statement."""
    return any(isinstance(node, ast.Assert) for node in ast.walk(ast.parse(code)))


def _dedent_body(lines: list[str], indent: str) -> str:
    """Return fenced lines with up to the fence's indentation removed."""
    width = len(indent)
    dedented = (line[min(width, len(line) - len(line.lstrip(" "))) :] for line in lines)
    return "\n".join(dedented) + "\n"


def _opening_delimiter(opening: str) -> str:
    """Return the full backtick or tilde run that opens a fence."""
    marker = opening[0]
    return marker * (len(opening) - len(opening.lstrip(marker)))


def _is_closing_fence(line: str, delimiter: str, indent: str) -> bool:
    """Return whether ``line`` closes a fence opened by ``delimiter``.

    CommonMark allows a closing fence to use more than the opening run, so
    only the fence's own marker character and a length at least that of the
    opener matter. The closer may sit up to three spaces beyond the
    indentation of its container; anything deeper is indented code.

    Returns
    -------
    bool
        Whether ``line`` terminates the fence.
    """
    stripped = line.strip()
    if len(stripped) < len(delimiter) or set(stripped) != {delimiter[0]}:
        return False
    leading = len(line) - len(line.lstrip())
    return leading <= len(indent) + 3


def _consume_fence(
    path: str, lines: list[str], index: int, marker: tuple[str, str]
) -> tuple[int, list[tuple[str, str]]]:
    """Validate the marked fence opening at ``index`` and consume it.

    Returns
    -------
    tuple[int, list[tuple[str, str]]]
        The index of the first line after the closing fence, and the tested
        example the fence holds, if any.
    """
    kind, name = marker
    raw_opening = lines[index]
    opening = raw_opening.strip()
    indent = raw_opening[: len(raw_opening) - len(raw_opening.lstrip())]
    delimiter = _opening_delimiter(opening)
    if kind == ILLUSTRATIVE:
        assert opening[len(delimiter) :].strip(), (
            f"{path}:{index + 1}: illustrative fence must declare a language"
        )
    else:
        assert opening == f"{delimiter}{LANGUAGES[kind]}", (
            f"{path}:{index + 1}: expected {LANGUAGES[kind]} fence for {kind} example"
        )
    start = index + 1
    closing = (
        number
        for number in range(start, len(lines))
        if _is_closing_fence(lines[number], delimiter, indent)
    )
    end = next(closing, None)
    assert end is not None, f"{path}:{start}: unterminated fence"
    if kind != "tested":
        return end + 1, []
    code = _dedent_body(lines[start:end], indent)
    assert _has_outcome_assertion(code), f"{path}:{start}: example has no outcome check"
    return end + 1, [(name, code)]


def _names_example(prose: str, name: str) -> bool:
    """Return whether ``prose`` mentions ``name`` as a whole token."""
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", prose) is not None


def _exclusion_bullets(prose: str) -> list[str]:
    """Return each Markdown bullet, with wrapped continuation lines joined.

    Returns
    -------
    list[str]
        One entry per bullet, in document order.
    """
    bullets: list[str] = []
    current: list[str] = []
    for line in prose.splitlines():
        stripped = line.strip()
        is_bullet = stripped.startswith(("- ", "* "))
        if is_bullet:
            if current:
                bullets.append(" ".join(current))
            current = [stripped]
        elif current and line.startswith((" ", "\t")):
            current.append(stripped)
        elif current:
            bullets.append(" ".join(current))
            current = []
    if current:
        bullets.append(" ".join(current))
    return bullets


def _exclusion_reason(bullet: str, name: str) -> str:
    """Return a bullet's text without its code spans or the example name."""
    text = re.sub(r"`[^`]*`", "", bullet[2:])
    return re.sub(rf"(?<![\w-]){re.escape(name)}(?![\w-])", "", text)


def _states_exclusion(prose: str, name: str) -> bool:
    """Return whether a bullet names ``name`` and says why it is excluded."""
    return any(
        _names_example(bullet, name)
        and re.search(r"[A-Za-z]{3,}", _exclusion_reason(bullet, name))
        for bullet in _exclusion_bullets(prose)
    )


def _parse_examples(path: str, text: str) -> list[tuple[str, str]]:
    """Return a document's tested Python examples, rejecting uncovered fences."""
    examples: list[tuple[str, str]] = []
    prose: list[str] = []
    illustrative: list[tuple[str, int]] = []
    lines = text.splitlines()
    pending: tuple[str, str] | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = MARKER.fullmatch(line)
        if marker is not None:
            assert pending is None, f"{path}:{index + 1}: marker without a fence"
            pending = (marker.group(1), marker.group(2))
            if pending[0] == ILLUSTRATIVE:
                illustrative.append((pending[1], index + 1))
        elif line.lstrip().startswith(FENCE_DELIMITERS):
            assert pending is not None, f"{path}:{index + 1}: unmarked fence"
            index, found = _consume_fence(path, lines, index, pending)
            examples.extend(found)
            pending = None
            continue
        else:
            assert pending is None or not line, (
                f"{path}:{index + 1}: marker is not beside a fence"
            )
            prose.append(line)
        index += 1
    assert pending is None, f"{path}: marker without a fence"
    prose_text = "\n".join(prose)
    for name, line_number in illustrative:
        assert _states_exclusion(prose_text, name), (
            f"{path}:{line_number}: illustrative example {name!r} has no "
            "documented exclusion reason"
        )
    return examples


def _documented_examples() -> list[tuple[str, str]]:
    """Load every tested example across the published documents."""
    examples: list[tuple[str, str]] = []
    for path in DOCUMENTS:
        text = (ROOT / path).read_text(encoding="utf-8")
        examples.extend(_parse_examples(path, text))
    names = [name for name, _ in examples]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"duplicate examples: {duplicates}"
    return examples


EXAMPLES = _documented_examples()
