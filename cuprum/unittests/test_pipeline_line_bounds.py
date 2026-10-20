"""Pipeline tests for the bounded ``max_line_chars`` line-observation option.

Pipelines resolve one run's output options into a config per stage and per
stream. This module pins the part of that resolution the line bound owns: the
caller's ``max_line_chars`` reaches the resolved pipeline config, the final
stage's stdout, and every stage's stderr, and a real pipeline that writes
unterminated output honours it on both streams. The option's construction
rules and its interaction with ``on_line`` are covered by
``test_pipeline_output_options.py``; keeping the pipeline line-bound
behaviour here keeps that module's cohesion about the options vocabulary
rather than mixing it with end-to-end stream observation.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import RunOutputOptions, ScopeConfig, scoped, sh
from cuprum._constants import DEFAULT_MAX_LINE_CHARS
from cuprum._testing import _prepare_pipeline_config
from cuprum.sh import ExecutionContext
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.lines import LineEvent


def test_pipeline_config_carries_the_line_bound_to_both_streams() -> None:
    """Pipeline resolution copies the caller's bound onto every stream config.

    The bound is one property of the run, not of a stage or a stream, so the
    resolved pipeline config and both of the stream configs it derives must
    agree with the options object the caller supplied.
    """
    config = _prepare_pipeline_config(
        output=RunOutputOptions(max_line_chars=7),
        timeout=None,
        context=ExecutionContext(),
    )

    assert config.max_line_chars == 7, (
        "pipeline resolution must carry the caller's max_line_chars"
    )
    assert config.stream_config.max_line_chars == 7, (
        "the final stage's stdout must observe the caller's bound"
    )
    assert config.stderr_stream_config.max_line_chars == 7, (
        "a stage's stderr must observe the caller's bound"
    )


def test_pipeline_config_line_bound_defaults_from_the_constant() -> None:
    """A pipeline built from default options inherits the shared bound."""
    config = _prepare_pipeline_config(
        output=RunOutputOptions(),
        timeout=None,
        context=ExecutionContext(),
    )

    assert config.max_line_chars == DEFAULT_MAX_LINE_CHARS
    assert config.stream_config.max_line_chars == DEFAULT_MAX_LINE_CHARS
    assert config.stderr_stream_config.max_line_chars == DEFAULT_MAX_LINE_CHARS


@pytest.mark.usefixtures("stream_backend")
def test_pipeline_on_line_bounds_unterminated_lines_on_every_stream() -> None:
    """The line bound reaches the final stdout and every stage's stderr.

    Each stage here writes one newline-free megabyte, tagged with a character
    of its own so the events can be attributed: the final stage writes ``o``
    to stdout and ``c`` to stderr, and the intermediate stage writes ``p`` to
    stderr. Nothing is captured or echoed, so line observation is the only
    consumer holding any of it, and each event must respect the bound while
    the pieces still reconstruct the child's complete output.
    """
    line_bound = 1024
    chunk = 1 << 20
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    producer = python(
        "-c",
        f"import sys; sys.stderr.write('p' * {chunk})",
    )
    consumer = python(
        "-c",
        "import sys\n"
        "sys.stdin.read()\n"
        f"sys.stderr.write('c' * {chunk})\n"
        f"sys.stdout.write('o' * {chunk})\n",
    )
    events: list[LineEvent] = []

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = (producer | consumer).run_sync(
            output=RunOutputOptions(
                capture=False,
                echo=False,
                on_line=events.append,
                max_line_chars=line_bound,
            ),
        )

    def joined(stream: str, filler: str) -> str:
        """Reassemble one stream's events whose text is entirely *filler*."""
        return "".join(
            event.text
            for event in events
            if event.stream == stream and set(event.text) == {filler}
        )

    assert joined("stdout", "o") == "o" * chunk, (
        "the final stage's stdout must be delivered whole, in bounded fragments"
    )
    assert joined("stderr", "c") == "c" * chunk, (
        "the final stage's stderr must also honour the bound"
    )
    assert joined("stderr", "p") == "p" * chunk, (
        "an intermediate stage's stderr must also honour the bound"
    )
    assert all(len(event.text) <= line_bound for event in events), (
        "no observed event may exceed max_line_chars, got "
        f"{max(len(event.text) for event in events)} chars"
    )
    assert result.ok is True, f"the pipeline should succeed, got {result!r}"
    assert result.final.stdout is None, (
        "capture=False must leave the final stdout unset"
    )
