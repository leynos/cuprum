"""Property tests for the analyser that decides what a workflow `run:` block means.

`workflow_shell.script_runs_command()` decides what a workflow's `run:` block
*means*, and every contract test that asks "does this job run X" trusts it. Two
failures matter and neither raises on its own: a quoted `<<` read as a
here-document operator swallows every following line (hiding the command under
assertion), and a real here-document scanned as script text reports a command
the step does not run.

The example-based tests beside these pin the shapes that have gone wrong
before. These properties cover the *combinations* those examples sample — which
is where both bugs live.

Every generator here *guarantees* the shape its property is about, and every
property asserts that shape before asserting the behaviour. A generator that
stopped producing the interesting case therefore fails its property rather than
leaving it true and empty. The assertions are the anti-vacuity witnesses; they
are deliberately not sampled `.example()` calls in companion tests, which would
establish the same thing only probabilistically.

The `act` stream parser's properties live beside these in
`tests/test_ci_act_stream_properties.py`; the two subjects share only the idea
of a guaranteed witness, not any input.
"""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.helpers.workflow_shell import script_runs_command

if typ.TYPE_CHECKING:
    from hypothesis.strategies import DrawFn

#: The sentinel command the shell properties look for. It is the command a
#: here-document body must hide and a quoted operator must not.
_SENTINEL = "docker info"

#: Words that carry no significance to the analyser. They are filler that makes
#: a generated script more than the one operator under test, so a tokenizer
#: that handles only the minimal case still fails.
_FILLER = ("true", "false", ":")

#: Quoted spellings of a here-document operator. None of these start a
#: here-document, so every following line remains script text.
_QUOTED_OPERATORS = ('"<<"', "'<<'", '"<< "', '"a<<"', '"<<x"')

#: Suffixes that may follow the operator on the same line.
_AFTER_OPERATOR = ("", " && true", "; true", " && echo done")

#: The commands that open a here-document, and delimiters to close it with.
_HEREDOC_OPENERS = ("cat", "python3", "bash")
_DELIMITERS = ("EOF", "PY", "MARKER")

#: Bare redirect spellings that put a `<<` token in the line, where it must
#: then be classified against its quoting.
_REDIRECT_OPERATORS = ("<<", "<< EOF", "<<EOF", "<< -")

#: A quoted `<<` glued to something the second tokenizer pass reads
#: differently: a command substitution, a backquote, or an abutting quote run.
#: Each defeats positional alignment between the two passes, which is the state
#: the analyser is required to refuse rather than guess at. Every combination
#: of these with a bare operator above is a verified refusal — none is accepted
#: by accident of tokenization.
_ALIGNMENT_BREAKERS = (
    "$(true)",
    "`true`",
    '"a"b',
    '$(echo "a(b)")',
    '"x"y',
    "$( )",
)


@st.composite
def _scripts_with_a_quoted_operator(draw: DrawFn) -> str:
    """Generate a script whose first line only *mentions* ``<<``.

    Returns
    -------
    str
        A script whose second line, and no earlier line, runs the sentinel.
    """
    operator = draw(st.sampled_from(_QUOTED_OPERATORS))
    suffix = draw(st.sampled_from(_AFTER_OPERATOR))
    filler = draw(st.lists(st.sampled_from(_FILLER), max_size=3))
    first = " ".join(["echo", operator, *filler, suffix]).strip()
    return f"{first}\n{_SENTINEL}"


@st.composite
def _scripts_with_a_here_document(draw: DrawFn) -> str:
    """Generate a script carrying a real here-document with a hidden body.

    The body always names the sentinel, so every generated script is one a
    tokenizer that scanned here-document bodies would get wrong.

    Returns
    -------
    str
        A script whose here-document body names the sentinel command, which the
        step therefore does not run.
    """
    delimiter = draw(st.sampled_from(_DELIMITERS))
    opener = draw(st.sampled_from(_HEREDOC_OPENERS))
    filler = draw(st.lists(st.sampled_from(_FILLER), max_size=2))
    trailing = draw(st.sampled_from(("true", "docker version", ":")))
    body = [_SENTINEL, *filler]
    return "\n".join([f"{opener} <<{delimiter}", *body, delimiter, trailing])


@st.composite
def _ambiguous_redirect_scripts(draw: DrawFn) -> str:
    """Generate a script whose redirect quoting cannot be classified.

    A bare `<<` and a quoted one share the line, and the quoted one is glued to
    a construction that makes the two tokenizer passes disagree on token
    positions. There is no correct answer available from the text alone: the
    analyser cannot tell whether the `<<` token came from quote-delimited text,
    so it cannot tell whether a here-document follows.

    Returns
    -------
    str
        A script that must be refused rather than read.
    """
    operator = draw(st.sampled_from(_REDIRECT_OPERATORS))
    quoted = draw(st.sampled_from(_QUOTED_OPERATORS))
    breaker = draw(st.sampled_from(_ALIGNMENT_BREAKERS))
    filler = draw(st.lists(st.sampled_from(_FILLER), max_size=2))
    first = " ".join(["echo", operator, f"{quoted}{breaker}", *filler]).strip()
    return f"{first}\n{_SENTINEL}"


@given(script=_scripts_with_a_quoted_operator())
def test_a_quoted_operator_never_hides_a_later_command(script: str) -> None:
    """A quoted ``<<`` is a literal, so later lines remain script text.

    Mistaking it for an operator makes the analyser wait for a delimiter that
    never arrives and swallow every remaining line. The contract assertion
    built on it then reports the opposite of the truth — that the job does not
    run the command it does — and nothing raises.
    """
    lines = script.splitlines()
    # Witness: the sentinel must be reachable only by scanning past a `<<`
    # that is not an operator. It sits on a later line and never on the
    # operator's own line, so a tokenizer that swallowed the rest would miss it.
    assert "<<" in lines[0], "the witness must be a script that mentions `<<`"
    assert _SENTINEL not in lines[0], (
        "the sentinel must not share the operator's line, or the swallowing bug "
        "this property targets would go unnoticed"
    )
    assert _SENTINEL in lines[1:], (
        "the sentinel must appear after the operator line, on a later line the "
        "guard is meant to keep visible"
    )
    assert script_runs_command(script, _SENTINEL), (
        f"a quoted operator in {script!r} must not be read as a here-document "
        f"operator, which would hide every later line from the scan"
    )


@given(script=_ambiguous_redirect_scripts())
def test_ambiguous_redirect_quoting_is_refused(script: str) -> None:
    """Refuse to guess when redirect quoting cannot be classified.

    The analyser's two tokenizer passes must agree positionally before it can
    say which `<<` is quoted. When they cannot, the two readings have opposite
    consequences and both are silent: guessing "unquoted" invents a
    here-document that swallows every following line, hiding the command under
    assertion; guessing "quoted" leaves a real body scanned as script text,
    reporting a command the step never runs. Raising is the only outcome that
    cannot produce a wrong answer, so it must reach the caller as an error and
    never as a boolean.

    The example-based test beside this pins the shortest instance; this covers
    the operator and breaker combinations that reach the same unclassifiable
    state by different tokenization.
    """
    lines = script.splitlines()
    # Witness: the line must actually carry a bare `<<`, or the check this
    # property targets is never engaged and the script would be accepted.
    assert "<<" in lines[0], "the witness must carry a redirect operator"
    assert lines[0].count("<<") >= 2, (
        "the witness must carry both a bare and a quoted `<<`, which is the "
        "combination whose classification is ambiguous"
    )
    with pytest.raises(ValueError, match="cannot classify here-document quoting"):
        script_runs_command(script, _SENTINEL)


@given(script=_scripts_with_a_here_document())
def test_a_here_document_body_is_never_scanned_as_script(script: str) -> None:
    """A here-document body is data, not script, and must stay invisible.

    Reporting a command that appears only inside a body would make a contract
    test assert a command the step never runs.
    """
    lines = script.splitlines()
    opener, body, delimiter, trailing = lines[0], lines[1:-2], lines[-2], lines[-1]
    # Witness: the sentinel must sit strictly inside the body — after the
    # opener and before the delimiter — so the only way to report it is to
    # scan text the shell treats as data.
    assert "<<" in opener, "the witness must open a real here-document"
    assert _SENTINEL in body, (
        "the body must name the sentinel, or this property holds for a "
        "tokenizer that scans bodies and finds nothing there"
    )
    assert _SENTINEL not in {opener, delimiter, trailing}, (
        "the sentinel must not leak outside the body, or the property would "
        "pass for a tokenizer that reports it as script text"
    )
    assert not script_runs_command(script, _SENTINEL), (
        f"the here-document body in {script!r} is data; a command inside it is "
        f"not executed by the step and must not be reported as running"
    )
