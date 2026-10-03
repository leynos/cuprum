"""Parse a Makefile with the pinned `makeutil` binary.

`tests/helpers/makefile.py` answers what a Makefile *says*: what a variable
expands to, what a target's recipe is. Answering either means first getting a
parsed document, and that is a claim about the outside world rather than about
`make`. This module owns it.

The parse is not hand-rolled. `makeutil`, the pinned parser the repository
already depends on, reports each assignment's `raw_value` with its
continuations and each rule's recipe text. A regex over the source
under-reports the selector in ways that look like a clean result: a missed
continuation, a comment mistaken for an assignment, or a `$(VAR)` reference
returned as its own literal text all shrink the set, and a set that is too
small makes every "nothing is uncovered" assertion pass for the wrong reason.

The process boundary is explicit rather than ambient. `root` names the
directory to parse in and `runner` is the callable that runs the parser, so a
test substitutes both and drives the parser's own behaviour — a non-zero exit,
malformed JSON, a missing binary, a timeout — without installing `makeutil`,
which is what makes those cases testable at all. A caller that reached for the
ambient binary would also be unable to exercise the one failure that says the
toolchain is broken rather than the Makefile.

The split from `makefile.py` follows `workflow_shell` and `workflow_recipe`:
reaching for a thing the caller named, versus deriving from a thing it already
holds. It also keeps both modules within the line budget `AGENTS.md` sets and
the lint gate enforces.
"""

from __future__ import annotations

import json
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv
import typing as typ

from tests.helpers.ci_documents import require
from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

__all__ = (
    "DEFAULT_RUNNER",
    "MAKEFILE",
    "MAKEUTIL_TIMEOUT_SECONDS",
    "Runner",
    "makeutil_document",
)

#: The Makefile this family reads. One definition, so a test that needs to name
#: it and a helper that needs to read it cannot disagree.
MAKEFILE = "Makefile"

#: How long the parser may take before the read is abandoned. `makeutil` parses
#: one file in well under a second, so this only fires on a wedged process; the
#: bound exists to turn "the suite hangs" into a named contract failure.
MAKEUTIL_TIMEOUT_SECONDS: typ.Final = 60

#: The process boundary, as a type rather than as an import. The parser is the
#: one thing here that reads the outside world, so it is the one thing worth
#: substituting: a test that has to install `makeutil` to exercise a malformed
#: document is testing the toolchain, and cannot exercise a missing binary or a
#: timeout at all. The parameter list is `subprocess.run`'s, narrowed to the
#: keywords `_parse_with` passes.
type Runner = cabc.Callable[..., subprocess.CompletedProcess[str]]

#: The runner a read uses unless the caller supplies one. Exported so
#: `tests/helpers/makefile.py` can default to it without importing
#: `subprocess` itself: the process is this module's subject, not that one's,
#: and a reader deriving from an already-parsed document never touches it.
DEFAULT_RUNNER: Runner = subprocess.run


def _parse_with(
    runner: Runner,
    *,
    makefile: str,
    root: pth.Path,
) -> subprocess.CompletedProcess[str]:
    """Run the parser, reporting a process that never started.

    `makeutil` is installed by CI and by `make`, so an environment without it
    is a genuine failure the caller must see. `subprocess.run` reports it as
    `FileNotFoundError`, which is a *different* type from the `AssertionError`
    the read API documents — so a caller catching the documented error would
    miss it, and one catching everything would not know which tool was absent.
    Translating here keeps the module's contract honest: every way the read can
    fail arrives as the documented failure, naming the binary and the directory
    it was looked for in.

    A timeout is translated for the same reason, and matters more: an
    unhandled `TimeoutExpired` would escape as a traceback from a library the
    caller never invoked.

    Parameters
    ----------
    runner : Runner
        The process boundary, as :func:`makeutil_document` received it.
    makefile : str
        Path to the Makefile, relative to the working directory.
    root : pathlib.Path
        The working directory the parser is run in.

    Returns
    -------
    subprocess.CompletedProcess
        The completed process, whatever its exit status; a non-zero status is
        the caller's to report, because it carries the parser's own diagnostic.

    Raises
    ------
    AssertionError
        If the binary cannot be started, or does not finish within
        :data:`MAKEUTIL_TIMEOUT_SECONDS`.
    """
    try:
        return runner(
            ["makeutil", "parse", makefile],
            capture_output=True,
            text=True,
            cwd=root,
            check=False,
            timeout=MAKEUTIL_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        message = (
            f"makeutil is not on PATH, so {makefile} could not be parsed; the "
            "selector is unreadable rather than empty. Install it the way CI "
            "does (`make` does this as a prerequisite), or pass a `runner` "
            "that supplies a parsed document"
        )
        raise AssertionError(message) from error
    except subprocess.TimeoutExpired as error:
        message = (
            f"makeutil did not parse {makefile} within "
            f"{MAKEUTIL_TIMEOUT_SECONDS}s; the process was still running, so "
            "this is a wedged parser rather than a malformed Makefile"
        )
        raise AssertionError(message) from error


def makeutil_document(
    *,
    makefile: str = MAKEFILE,
    root: pth.Path | None = None,
    runner: Runner = DEFAULT_RUNNER,
) -> dict[str, typ.Any]:
    """Parse one Makefile with the pinned `makeutil` binary.

    Parameters
    ----------
    makefile : str
        Path to the Makefile, relative to the working directory.
    root : pathlib.Path, optional
        The directory to parse in, defaulting to the repository root. Named
        rather than assumed so a caller reading a different tree does not have
        its path silently resolved against this one.
    runner : Runner, optional
        The process boundary, defaulting to `subprocess.run`. Injected so the
        parser's own behaviour — a non-zero exit, malformed JSON, a missing
        binary, a timeout — is exercised without installing `makeutil`, which
        is what makes those cases testable at all.

    Returns
    -------
    dict
        The parsed JSON document, with `variables`, `rules`, and `includes`.

    Raises
    ------
    AssertionError
        If `makeutil` cannot be started, does not finish within
        :data:`MAKEUTIL_TIMEOUT_SECONDS`, exits non-zero, or emits something
        other than a JSON object. Each means the parse did not happen, and a
        caller that read the empty result as "the selector names nothing"
        would fail later with a misleading message.
    """
    completed = _parse_with(runner, makefile=makefile, root=root or repo_root())
    require(
        condition=completed.returncode == 0,
        message=(
            f"makeutil failed to parse {makefile} with exit "
            f"{completed.returncode}: {completed.stderr.strip()}"
        ),
    )
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        message = f"makeutil did not emit JSON for {makefile}: {error}"
        raise AssertionError(message) from error
    require(
        condition=isinstance(parsed, dict),
        message=f"makeutil must emit a JSON object for {makefile}",
    )
    return typ.cast("dict[str, typ.Any]", parsed)
