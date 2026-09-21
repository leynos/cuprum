"""Property tests for the parser that decides what a CI run actually reported.

`ActRun.outputs()` promises *last value wins*. `act`'s stream is cumulative, so
a name set more than once appears more than once with the stale values ahead of
the live one. A reader that took the first match would report an output a later
step had already replaced — a wrong gate decision, silently. The same rule
governs `step_results`, and through it `failed_steps`, which is what a
scenario's failure message is built from.

The example-based tests in `tests/integration/test_act_stream_parsing.py` pin
the recorded streams and the shapes that have gone wrong before. These
properties cover the *orderings* those examples sample — which is where the
bug lives.

Every generator here *guarantees* the shape its property is about, and every
property asserts that shape before asserting the behaviour. A generator that
stopped producing the interesting case therefore fails its property rather than
leaving it true and empty. The assertions are the anti-vacuity witnesses; they
are deliberately not sampled `.example()` calls in companion tests, which would
establish the same thing only probabilistically.

The shell analyser's properties live beside these in
`tests/test_ci_workflow_shell_properties.py`; the two subjects share only the
idea of a guaranteed witness, not any input.
"""

from __future__ import annotations

import json
import typing as typ

from hypothesis import given
from hypothesis import strategies as st

from tests.helpers.act_stream import ActRun

if typ.TYPE_CHECKING:
    from hypothesis.strategies import DrawFn

#: Names the writer plausibly sets. A name written twice is what makes the
#: ordering observable, so the pool is deliberately small.
_OBSERVED_NAMES = ("bench", "event_class", "detector_status", "decision")

#: The two values the guaranteed repeat writes, stale first. Fixed strings
#: rather than generated text, because the pair has to be *distinguishable*:
#: "a reader that took the first match would be wrong here" is only assertable
#: if the two values differ, and only useful if the difference is something the
#: generator could not have produced by chance.
_STALE_VALUE = "superseded"
_LIVE_VALUE = "live"

#: Step names the workflow plausibly gives its steps, and the two verdicts the
#: guaranteed repeat reports. `step_results` keys by step name with the same
#: last-wins rule as `outputs`, and a run reports each step more than once when
#: `act` streams progress, so the same staleness bug is reachable there.
_STEP_NAMES = ("Build", "Bench", "Persist", "Detector")
_STALE_VERDICT = "failure"
_LIVE_VERDICT = "success"

#: A line the parser must skip: JSON that is not a `set-output` event, JSON
#: that is not an object at all, and text that is not JSON.
_NOISE = (
    {"command": "summary", "content": "irrelevant"},
    {"step": "s", "stepResult": "success"},
    [1, 2, 3],
    "a bare JSON string",
    12345,
)
_NON_JSON = ("", "   ", "not json at all")


def _json_line(payload: object) -> str:
    """Render one stream line as `act` would.

    Returns
    -------
    str
        A single JSON value, without a trailing newline.
    """
    return json.dumps(payload, separators=(",", ":"))


def _writes(stdout: str) -> list[tuple[str, str]]:
    """Return every `set-output` write in the stream, in emission order.

    A plain loop over the same text as the parser reads, so a bug in either
    shows up as a disagreement between two implementations rather than as
    agreement with a restatement of one.

    Returns
    -------
    list of tuple of str and str
        Each output name paired with the value written for it, in order.
    """
    writes: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("command") != "set-output":
            continue
        name, argument = event.get("name"), event.get("arg")
        if isinstance(name, str) and isinstance(argument, str):
            writes.append((name, argument))
    return writes


def _first_match(writes: list[tuple[str, str]]) -> dict[str, str]:
    """Return what a reader that kept the *first* value for each name would.

    Both `outputs` and `step_results` resolve by last value, so the reference
    implementation is shared: the two properties differ only in what they feed
    it, and a divergence between the two readers stays visible as a difference
    between two dicts rather than as a restatement of one reader.

    Returns
    -------
    dict of str to str
        Each name mapped to the value it was first given.
    """
    earliest: dict[str, str] = {}
    for name, value in writes:
        earliest.setdefault(name, value)
    return earliest


def _last_match(writes: list[tuple[str, str]]) -> dict[str, str]:
    """Return what a reader that kept the *last* value for each name would.

    Returns
    -------
    dict of str to str
        Each name mapped to the value it was last given.
    """
    # Rebuilding the pairs in order is the operation itself here: a repeated
    # name overwrites its earlier value, which is exactly the resolution rule
    # under test rather than an incidental detail. A larger dict would be a
    # mistake, not a stronger reference, because only the last write to a name
    # survives in `act`'s own stream.
    latest: dict[str, str] = {}
    latest.update(writes)
    return latest


@st.composite
def _act_streams(draw: DrawFn) -> str:
    """Generate a cumulative `set-output` stream interleaved with noise.

    Two shapes are guaranteed rather than left to chance, because each is the
    case its property is *about*:

    - one name is written twice, stale value first and live value last, with
      the two values differing, so a reader that kept the first match is
      observably wrong;
    - at least one line is not JSON, so the "drop only non-JSON lines" clause
      has something to drop.

    Leaving either to a small probability would make the properties true and
    occasionally empty.

    Returns
    -------
    str
        Stdout text carrying the generated stream, one entry per line.
    """
    # Drawn first, because the random writes must not touch it: if one did, the
    # first value for a name could equal the last and the witness below would
    # hold only by luck.
    repeated = draw(st.sampled_from(_OBSERVED_NAMES))
    others = tuple(name for name in _OBSERVED_NAMES if name != repeated)
    writes = draw(
        st.lists(
            st.tuples(st.sampled_from(others), st.text(max_size=12)),
            max_size=10,
        )
    )
    noise = draw(st.lists(st.sampled_from(_NOISE), max_size=6))
    non_json = draw(st.lists(st.sampled_from(_NON_JSON), min_size=1, max_size=4))
    lines = [
        _json_line({"command": "set-output", "name": name, "arg": argument})
        for name, argument in writes
    ]
    lines.extend(_json_line(item) for item in noise)
    # The live write is last and unconditional, so a first-match reader and a
    # last-match reader must disagree about `repeated`.
    lines.extend((
        _json_line({"command": "set-output", "name": repeated, "arg": _STALE_VALUE}),
        _json_line({"command": "set-output", "name": repeated, "arg": _LIVE_VALUE}),
    ))
    lines.extend(non_json)
    return "\n".join(lines) + "\n"


@st.composite
def _act_step_streams(draw: DrawFn) -> str:
    """Generate a stream of step verdicts, one step reported twice.

    The same construction as :func:`_act_streams`, applied to the other field
    that resolves by last value. The repeated step is reported `failure` and
    then `success`, which is the ordering that matters: `failed_steps` is what
    a scenario's failure message is built from, so a first-match reader would
    name a step that went on to pass — reporting a failure that did not happen,
    which is indistinguishable from a real one.

    Returns
    -------
    str
        Stdout text carrying the generated step results, one entry per line.
    """
    # Drawn first, and excluded from the random results below, so that the
    # repeated step's first and last verdict are the ones fixed here and the
    # guarantee does not depend on which names the random draws happened to
    # pick.
    repeated = draw(st.sampled_from(_STEP_NAMES))
    others = tuple(name for name in _STEP_NAMES if name != repeated)
    results = draw(st.lists(st.sampled_from(others), max_size=8))
    lines = [
        _json_line({"step": name, "stepResult": _LIVE_VERDICT}) for name in results
    ]
    # Reported twice, failing then passing, so first-match and last-match
    # readers must disagree about `repeated`.
    lines.extend((
        _json_line({"step": repeated, "stepResult": _STALE_VERDICT}),
        _json_line({"step": repeated, "stepResult": _LIVE_VERDICT}),
    ))
    return "\n".join(lines) + "\n"


def _run(stdout: str) -> ActRun:
    """Wrap recorded stdout in an `ActRun`.

    Returns
    -------
    ActRun
        The recording, with no exit status or diagnostics of its own.
    """
    return ActRun(exit_code=0, stdout=stdout, stderr="", argv=("act", "--json"))


@given(stdout=_act_streams())
def test_the_last_value_wins_for_every_output_name(stdout: str) -> None:
    """A name written more than once must resolve to its final value.

    The cumulative stream is the whole reason this parser exists, and the
    failure is silent: taking the first match yields a plausible value that a
    later step had already replaced, so the gate reads a stale detector verdict
    and nothing about the run looks wrong.
    """
    writes = _writes(stdout)
    # Witness: the stream must overwrite a value, so that a reader which kept
    # the first match would disagree with one which kept the last.
    assert _first_match(writes) != _last_match(writes), (
        "the generated stream must overwrite a value with a different one, or "
        "this property holds for a reader that takes the first match"
    )
    assert _run(stdout).outputs() == _last_match(writes), (
        "outputs() must resolve each name to its last valid value; a reader "
        "that takes the first match reports an output a later step replaced"
    )


@given(stdout=_act_streams())
def test_parsing_preserves_order_and_drops_only_non_json_lines(stdout: str) -> None:
    """Keep the stream's order, and skip only the lines that are not JSON.

    Order is what makes "last value wins" meaningful in the first place. A
    parser that reordered, deduplicated, or silently dropped a valid object
    would change which value is last, and the damage would attach to whichever
    name happened to collide.
    """
    parsed = _run(stdout).events
    expected = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            expected.append(event)
    assert parsed == expected, (
        "every JSON object must survive parsing, in emission order; only "
        "non-JSON lines may be dropped"
    )
    # Witness: the generator must supply a line that is not JSON, so the
    # "only non-JSON lines are dropped" clause has something to act on.
    assert len(parsed) < len(stdout.splitlines()), (
        "the generated stream must contain a line that is not JSON, or the "
        "drop-nothing alternative would satisfy this property too"
    )


@given(stdout=_act_step_streams())
def test_the_last_verdict_wins_for_every_step_name(stdout: str) -> None:
    """A step reported more than once must resolve to its final verdict.

    `step_results` feeds `failed_steps`, which is what a scenario's failure
    message is built from. A reader that kept the first verdict would name a
    step that went on to pass, and a spurious entry in a failure message looks
    exactly like a real one — the scenario would be chased for a failure that
    never happened.
    """
    verdicts: list[tuple[str, str]] = []
    for event in _run(stdout).events:
        step, verdict = event.get("step"), event.get("stepResult")
        if isinstance(step, str) and isinstance(verdict, str):
            verdicts.append((step, verdict))
    first, last = _first_match(verdicts), _last_match(verdicts)
    # Witness: the stream must overwrite a verdict, or a first-match reader
    # would agree with a last-match one and the property would assert nothing.
    assert first != last, (
        "the generated stream must overwrite a verdict with a different one, "
        "or this property holds for a reader that takes the first match"
    )
    assert _run(stdout).step_results == last, (
        "step_results must resolve each step to its last verdict; a reader "
        "that takes the first reports a step that went on to pass"
    )
    failing = {step for step, verdict in last.items() if verdict != "success"}
    assert set(_run(stdout).failed_steps) == failing, (
        "failed_steps must be exactly the steps whose final verdict is not "
        "`success`, so a stale failure never reaches a failure message"
    )
