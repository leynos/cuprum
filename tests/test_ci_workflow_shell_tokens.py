"""Tests for the shell tokenizer behind the workflow contract assertions.

`tests/helpers/workflow_shell.py` decides what a workflow's `run:` block
*means*, and every contract test that asks "does this job run X" trusts it. A
mistake here does not raise: it returns `False` for a script that does run the
command, or `True` for one that does not, and the assertion built on it then
reports the opposite of the truth. The cases below are the ones that have
actually produced a wrong answer.
"""

from __future__ import annotations

import pytest

from tests.helpers.workflow_shell import script_runs_command

#: A quoted `<<` is a literal, not a here-document operator. `act`'s own
#: scripts and any `echo "<<"`-style diagnostic produce it, and mistaking it
#: for an operator makes the parser wait for a delimiter that never arrives —
#: swallowing every following line, including the command under assertion.
_QUOTED_OPERATOR_SCRIPTS = [
    pytest.param(
        'payload="$(mktemp)"; echo "<<"&&true\ndocker info',
        id="command-substitution-beside-quoted-operator",
    ),
    pytest.param(
        'echo "<<"&&true\ndocker info',
        id="quoted-operator-adjacent-to-andand",
    ),
    pytest.param(
        'echo "<<" && true\ndocker info',
        id="quoted-operator-spaced-from-andand",
    ),
    pytest.param(
        'echo "<<"\ndocker info',
        id="quoted-operator-alone-on-a-line",
    ),
    pytest.param(
        'if [ -n "${X}" ]; then echo "<<"&&true; fi\ndocker info',
        id="quoted-operator-inside-a-conditional",
    ),
]

#: A command substitution inside a double-quoted assignment. `shlex` in
#: non-posix mode with `punctuation_chars` is the only configuration that
#: splits shell operators the way the comparison needs, and this is the shape
#: it cannot lex. The tokenizer must degrade rather than raise, because a
#: workflow step that assigns a `mktemp` path is unremarkable — `ci.yml` has
#: three of them — and a `ValueError` there takes out every contract test that
#: scans the job.
_COMMAND_SUBSTITUTION_SCRIPTS = [
    pytest.param('payload="$(mktemp)"\ndocker info', id="mktemp-assignment"),
    pytest.param(
        'major_minor="$(echo "${version}" | cut -d. -f1,2)"\ndocker info',
        id="pipeline-inside-substitution",
    ),
    pytest.param(
        'pytag="cp$(echo "${major_minor}" | tr -d \'.\')"\ndocker info',
        id="substitution-with-quoted-literal",
    ),
]


@pytest.mark.parametrize("script", _QUOTED_OPERATOR_SCRIPTS)
def test_a_quoted_heredoc_operator_does_not_swallow_following_lines(
    script: str,
) -> None:
    """A literal ``<<`` must not start a here-document.

    The command on the line *after* the quoted operator is the one that
    decides whether the assertion passes, so it is the one asserted on.
    """
    assert script_runs_command(script, "docker info"), (
        f"a quoted `<<` in {script!r} must not be read as a here-document "
        f"operator, which would hide every later line from the scan"
    )


@pytest.mark.parametrize("script", _COMMAND_SUBSTITUTION_SCRIPTS)
def test_a_command_substitution_in_an_assignment_is_tokenizable(
    script: str,
) -> None:
    """A ``$(...)`` inside double quotes must not raise.

    A workflow step assigning a temporary path is ordinary, and the tokenizer
    runs over every step of a job. Raising here would fail a contract test for
    a reason unrelated to the contract it asserts.
    """
    assert script_runs_command(script, "docker info"), (
        f"the tokenizer must read {script!r} with unclosed-quotation "
        f"tolerance rather than raising, and must still find the command"
    )


def test_an_unquoted_heredoc_operator_still_hides_its_body() -> None:
    """The tolerance above must not disable here-document skipping.

    This is the negative control for the two cases above: if the fix were to
    stop treating ``<<`` as an operator altogether, the body of a real
    here-document would be scanned as script text and the following assertion
    would pass for the wrong reason. The body here contains a command that the
    step does not run, and it must stay invisible.
    """
    script = "python - <<'PY'\ndocker info\nPY\ntrue"

    assert not script_runs_command(script, "docker info"), (
        "a here-document body is data, not script; the command inside it is "
        "not executed by the step and must not be reported as running"
    )


def test_a_heredoc_operator_after_a_quoted_one_is_still_recognized() -> None:
    """A real here-document must still be found beside a quoted ``<<``.

    The two spellings differ by one character and by which pass sees them, so
    a fix that classifies by position rather than by quoting could hide the
    genuine operator as well. Here the quoted literal is incidental and the
    here-document is real, so the body must still be skipped and the trailing
    command still found.
    """
    script = 'echo "<<" && cat <<EOF\ndocker info\nEOF\ntrue'

    assert not script_runs_command(script, "docker info"), (
        "the here-document body must still be skipped when the same line "
        "also carries a quoted `<<`"
    )


def test_a_real_heredoc_beside_a_command_substitution_hides_its_body() -> None:
    """Keep real here-document data hidden when quote analysis needs a fallback."""
    script = 'payload="$(mktemp)"; cat <<EOF\ndocker info\nEOF\ntrue'
    assert not script_runs_command(script, "docker info"), (
        "a command substitution must not disable real here-document detection"
    )


def test_ambiguous_redirect_quoting_is_refused() -> None:
    """Report unsupported quote concatenation instead of inventing a redirect."""
    with pytest.raises(ValueError, match="cannot classify here-document quoting"):
        script_runs_command('echo "a"b "<<"\ndocker info', "docker info")
