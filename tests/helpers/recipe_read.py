"""Read what a target's recipe *is*, and tokenize it into shell words.

`tests/helpers/makefile.py` resolves what the Makefile's *variables* say. A
target's recipe is a different question with a different failure mode, so it
lives here: the recipe text is read back as shell words rather than as a
string, because a substring test cannot tell a live command from the same words
commented out.

The split keeps both modules inside the line budget `AGENTS.md` sets and the
lint gate enforces, and it follows the same boundary the rest of the family
uses — reaching for a thing the caller named, versus deriving from a thing it
already holds.
"""

from __future__ import annotations

import re
import shlex
import typing as typ

from tests.helpers.ci_documents import require
from tests.helpers.makeutil import (
    DEFAULT_RUNNER,
    MAKEFILE,
    Runner,
    makeutil_document,
)

if typ.TYPE_CHECKING:
    import pathlib as pth


def _join_continuations(value: str) -> str:
    r"""Collapse ``\\``-newline continuations into the words they stand for.

    Two callers share this, and they are governed by different rules.

    For an *assignment* value it is what `make` itself does: `make` replaces the
    backslash-newline and the whitespace after it with one space, so
    ``V = one \\`` / ``  two`` expands to ``one two``. Resolving it later, at
    expansion time, would make each backslash a word of its own — and a
    backslash is not a `.py` path, so the selector would look right while the
    tuple carried junk.

    For a *recipe entry* `make` does not collapse anything: it hands the
    backslash-newline to the shell, and the shell does the collapsing. Doing it
    here as well is a deliberate normalisation, and it is load-bearing rather
    than cosmetic — `shlex` implements no line continuation, so a raw ``\``
    newline arrives as a word containing the newline itself. The shell would
    never see such a word, so a token check reading the uncollapsed text would
    be reasoning about words that do not exist.

    Parameters
    ----------
    value : str
        An assignment's or a recipe entry's raw text, possibly spanning several
        source lines.

    Returns
    -------
    str
        The logical single line. The replacement is one space, so a
        space-before-backslash survives beside it and a continued entry can
        read back with two spaces; that is invisible to the tokenizer, which is
        the only thing above this that reads the result.
    """
    return re.sub(r"\\\n[ \t]*", " ", value)


def recipe_of(
    name: str,
    *,
    makefile: str = MAKEFILE,
    root: pth.Path | None = None,
    runner: Runner = DEFAULT_RUNNER,
) -> str:
    """Return one target's recipe text.

    Each recipe entry is returned on its own line, so the result preserves the
    entry boundaries `make` hands the shell: one command per line unless the
    entry itself was backslash-continued. Backslash continuations *within* an
    entry are collapsed to a space, matching what `make` does before invoking
    the shell, so a continued command reads back as one logical line.

    That distinction is load-bearing for every reader above. A comment ends at
    its entry's newline — an uncontinued ``#`` disables its own command, not
    every command after it — so a caller must tokenize the text rather than
    test it for substrings. `recipe_tokens` is that reader.

    Parameters
    ----------
    name : str
        Target name, as written before the colon.
    makefile : str
        Path to the Makefile, relative to the working directory.
    root : pathlib.Path, optional
        The directory to parse in, as :func:`makeutil_document` takes it.
    runner : Runner, optional
        The process boundary, as :func:`makeutil_document` takes it, so a test
        can drive this question without installing `makeutil`.

    Returns
    -------
    str
        The target's recipe entries joined with newlines, with `make`'s leading
        `@` silencing marker removed and backslash continuations collapsed.
        Entry structure is otherwise preserved, so a caller can still tell one
        command from the next.

    Raises
    ------
    AssertionError
        If the Makefile declares no rule for ``name``, or if the parse itself
        fails, as :func:`makeutil_document` reports.
    """
    document = makeutil_document(makefile=makefile, root=root, runner=runner)
    declared = document.get("rules")
    require(
        condition=isinstance(declared, list),
        message="the makeutil document must carry a `rules` list",
    )
    for rule in typ.cast("list[object]", declared):
        entry = typ.cast("dict[str, object]", rule)
        targets = typ.cast("list[object]", entry.get("targets") or [])
        if name not in targets:
            continue
        recipes = typ.cast("list[object]", entry.get("recipes") or [])
        return "\n".join(
            _join_continuations(
                typ.cast("str", typ.cast("dict[str, object]", step).get("text", ""))
            ).removeprefix("@")
            for step in recipes
        )
    require(condition=False, message=f"the Makefile must declare a {name} target")
    raise AssertionError


def recipe_tokens(recipe: str) -> tuple[str, ...]:
    """Tokenize a recipe into the shell words `make` would hand the shell.

    A caller asking whether a recipe *uses* a construct has to read it as a
    shell rather than as text. A substring test cannot tell a live command from
    the same words commented out, and `recipe_of` preserves each recipe entry's
    newline, so a comment disables its own command and nothing after it —
    every token check would keep passing over a command the shell never runs.

    Comment markers are honoured, which is the whole point: ``# ...``
    contributes no tokens, so dead text cannot satisfy a caller. Quoting is
    honoured too, so a literal inside a quoted argument counts as a word rather
    than as a comment opening.

    Parameters
    ----------
    recipe : str
        Recipe text, as :func:`recipe_of` returns it.

    Returns
    -------
    tuple of str
        The shell words, in order, with comments dropped.

    Examples
    --------
    >>> recipe_tokens("echo hi # $(PYTEST)")
    ('echo', 'hi')
    >>> recipe_tokens("echo '# $(PYTEST)'")
    ('echo', '# $(PYTEST)')
    """
    lexer = shlex.shlex(recipe, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    return tuple(lexer)
