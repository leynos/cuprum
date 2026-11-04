"""Correctness tests for the 5.2.2 hot-path frame census.

The census answers one question per capture: how much sampled weight sits under
an anchor frame, and how much of it also reaches a target frame. These tests
compare the command's arithmetic with an independent brute-force recount over
the same parsed stacks, then pin each exit status and every input the command
must reject.

The generated domain always contains all five stack shapes that decide the
answer, so a passing property cannot be vacuous: the target inside the anchor,
outside it, repeated within one stack, rendered at another location, and absent
entirely.
"""

from __future__ import annotations

import json
import typing as typ

from hypothesis import example, given
from hypothesis import strategies as st

from benchmarks import summarize_hot_path_frames as census_module
from benchmarks._line_event_profile_model import (
    Capture,
    FramePattern,
    Stack,
    parse_frame,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

_ANCHOR_TEXT = "emit_line (cuprum/_line_callbacks.py:104)"
_TARGET_TEXT = "isawaitable (inspect.py:366)"
_TARGET_ELSEWHERE_TEXT = "isawaitable (vendor/inspect.py:12)"
_OTHER_TEXT = "_consume (cuprum/_stream_line_consumer.py:69)"

_ANCHOR = FramePattern(
    function="emit_line",
    location="cuprum/_line_callbacks.py",
)
_TARGET = FramePattern(
    function="isawaitable",
    location="inspect.py",
)

# The five shapes whose interaction decides every census answer.
_CORE_SHAPES = (
    (_OTHER_TEXT,),
    (_ANCHOR_TEXT, _TARGET_TEXT),
    (_TARGET_TEXT,),
    (_ANCHOR_TEXT, _TARGET_TEXT, _TARGET_TEXT),
    (_ANCHOR_TEXT, _TARGET_ELSEWHERE_TEXT),
)

_FRAME_TEXTS = st.sampled_from((
    _ANCHOR_TEXT,
    _TARGET_TEXT,
    _TARGET_ELSEWHERE_TEXT,
    _OTHER_TEXT,
))


def _stack(frames: cabc.Sequence[str], samples: int) -> Stack:
    """Build one parsed stack from rendered frame text."""
    return Stack(frames=tuple(parse_frame(text) for text in frames), samples=samples)


@st.composite
def _captures(draw: st.DrawFn) -> Capture:
    """Generate a capture that always covers every deciding stack shape."""
    stacks = [
        _stack(shape, draw(st.integers(min_value=1, max_value=100)))
        for shape in _CORE_SHAPES
    ]
    stacks.extend(
        _stack(
            draw(st.lists(_FRAME_TEXTS, min_size=1, max_size=6)),
            draw(st.integers(min_value=1, max_value=100)),
        )
        for _ in range(draw(st.integers(min_value=0, max_value=5)))
    )
    return Capture(
        stacks=tuple(stacks),
        parent_samples=sum(stack.samples for stack in stacks),
    )


def _contains(
    stack: Stack,
    function: str,
    location: str | None,
) -> bool:
    """Return whether ``stack`` holds ``function`` at ``location``."""
    return any(
        frame.function == function and (location is None or frame.location == location)
        for frame in stack.frames
    )


@example(
    capture=Capture(
        stacks=(
            _stack((_ANCHOR_TEXT, _TARGET_TEXT), 10),
            _stack((_TARGET_TEXT,), 20),
        ),
        parent_samples=30,
    )
)
@given(capture=_captures())
def test_census_matches_a_brute_force_recount(capture: Capture) -> None:
    """Every reported weight equals an independent recount over the stacks."""
    result = census_module.census(capture, _ANCHOR, _TARGET)
    anchored = [
        stack
        for stack in capture.stacks
        if _contains(stack, "emit_line", "cuprum/_line_callbacks.py")
    ]
    exact = [
        stack for stack in anchored if _contains(stack, "isawaitable", "inspect.py")
    ]
    name_only = [stack for stack in anchored if _contains(stack, "isawaitable", None)]
    outside = [
        stack
        for stack in capture.stacks
        if _contains(stack, "isawaitable", "inspect.py")
        and not _contains(stack, "emit_line", "cuprum/_line_callbacks.py")
    ]

    assert result.total_weight == capture.parent_samples, "total must be every sample"
    assert result.anchor_weight == sum(stack.samples for stack in anchored), (
        "anchor weight must be the anchored stacks"
    )
    assert result.target_weight == sum(stack.samples for stack in exact), (
        "target weight must be the anchored stacks reaching the target exactly"
    )
    assert result.target_weight_any_location == sum(
        stack.samples for stack in name_only
    ), "name-only weight must be the anchored stacks reaching the name"
    assert result.target_weight_outside_anchor == sum(
        stack.samples for stack in outside
    ), "outside weight must be the stacks reaching the target without the anchor"


def _write_capture(tmp_path: pth.Path, text: str) -> pth.Path:
    """Write a folded capture and return its path."""
    path = tmp_path / "stacks.folded"
    path.write_text(text)
    return path


def _run(
    tmp_path: pth.Path,
    text: str,
    *,
    anchor: str = "emit_line@cuprum/_line_callbacks.py",
    target: str = "isawaitable@inspect.py",
    floor: int = 1,
) -> tuple[int, dict[str, object]]:
    """Run the census over ``text`` and return its exit status and document."""
    folded = _write_capture(tmp_path, text)
    output = tmp_path / "census.json"
    status = census_module.main(
        [
            str(folded),
            "--anchor",
            anchor,
            "--target",
            target,
            "--min-anchor-weight",
            str(floor),
            "--output",
            str(output),
        ],
    )
    document = output.read_text() if output.exists() else "{}"
    return status, typ.cast("dict[str, object]", json.loads(document))


def _line(*frames: str, samples: int) -> str:
    """Render one folded-stack line."""
    return f"{';'.join(frames)} {samples}\n"


def test_absence_under_the_anchor_passes(tmp_path: pth.Path) -> None:
    """A capture with no target under the anchor proves absence and exits 0."""
    capture = _line(_OTHER_TEXT, samples=100) + _line(_ANCHOR_TEXT, samples=200)
    status, result = _run(tmp_path, capture, floor=200)

    assert result["status"] == "pass", "zero target weight must pass"
    assert result["anchor_weight"] == 200, "both anchored samples count"
    assert result["target_weight"] == 0, "the target is absent under the anchor"
    assert result["target_weight_any_location"] == 0, (
        "the target is absent under the anchor by name too"
    )
    assert status == 0, "a proven absence exits 0"


def test_presence_under_the_anchor_fails(tmp_path: pth.Path) -> None:
    """A target that survives under the anchor exits 1."""
    capture = _line(_ANCHOR_TEXT, _TARGET_TEXT, samples=200)
    status, result = _run(tmp_path, capture, floor=200)

    assert result["status"] == "fail_target_present", "a surviving target must fail"
    assert result["target_weight"] == 200, "the one stack carries all the weight"
    assert result["target_weight_any_location"] == 200, (
        "the name-only weight must agree with the exact weight"
    )
    assert status == 1, "a proven presence exits 1"


def test_target_outside_the_anchor_never_counts(tmp_path: pth.Path) -> None:
    """A target reached without the anchor is reported but not scored."""
    capture = _line(_ANCHOR_TEXT, samples=200) + _line(_TARGET_TEXT, samples=50)
    status, result = _run(tmp_path, capture, floor=200)

    assert result["target_weight"] == 0, "out-of-anchor target samples do not score"
    assert result["target_weight_any_location"] == 0, (
        "the name-only score also excludes out-of-anchor samples"
    )
    assert result["target_weight_outside_anchor"] == 50, (
        "the excluded samples are still reported"
    )
    assert status == 0, "a target outside the anchor is a proven absence"


def test_target_repeated_in_one_stack_counts_once(tmp_path: pth.Path) -> None:
    """A stack matching the target twice contributes its weight once."""
    capture = _line(_ANCHOR_TEXT, _TARGET_TEXT, _TARGET_TEXT, samples=200)
    status, result = _run(tmp_path, capture, floor=200)

    assert result["target_weight"] == 200, "one stack must be counted once"
    assert status == 1, "a repeated target still proves presence"


def test_target_at_another_location_is_only_name_visible(
    tmp_path: pth.Path,
) -> None:
    """A relocated target makes the two target weights disagree, exiting 2."""
    capture = _line(_ANCHOR_TEXT, _TARGET_ELSEWHERE_TEXT, samples=200) + _line(
        _ANCHOR_TEXT, _TARGET_TEXT, samples=100
    )
    status, result = _run(tmp_path, capture, floor=200)

    assert result["target_weight"] == 100, "only the exact location scores"
    assert result["target_weight_any_location"] == 300, (
        "the name-only weight sees both locations"
    )
    assert result["status"] == "target_weights_disagree", (
        "a location that the exact pattern misses is inconclusive"
    )
    assert status == 2, "disagreeing weights exit 2"


def test_anchor_below_the_floor_is_inconclusive(tmp_path: pth.Path) -> None:
    """Zero target weight is not evidence when the anchor was rarely sampled."""
    capture = _line(_ANCHOR_TEXT, samples=10)
    status, result = _run(tmp_path, capture, floor=14_000)

    assert result["status"] == "anchor_below_floor", "a thin anchor proves nothing"
    assert status == 2, "an unsupported claim exits 2"


def test_empty_capture_is_inconclusive(tmp_path: pth.Path) -> None:
    """A capture with no parsable stack is rejected, not scored as zero."""
    status, _ = _run(tmp_path, "")

    assert status == 2, "an empty capture cannot prove absence"


def test_spec_without_a_location_is_rejected(tmp_path: pth.Path) -> None:
    """An anchor without a location cannot identify a frame."""
    capture = _line(_ANCHOR_TEXT, samples=1)
    folded = _write_capture(tmp_path, capture)
    output = tmp_path / "census.json"
    status = census_module.main(
        [
            str(folded),
            "--anchor",
            "emit_line",
            "--target",
            "isawaitable@inspect.py",
            "--min-anchor-weight",
            "1",
            "--output",
            str(output),
        ],
    )

    assert status == 2, "a location-less anchor must be rejected"
    assert not output.exists(), "a rejected run must write no result"


def test_spec_with_two_separators_is_rejected(tmp_path: pth.Path) -> None:
    """An over-specified anchor is rejected rather than silently truncated."""
    capture = _line(_ANCHOR_TEXT, samples=1)
    folded = _write_capture(tmp_path, capture)
    output = tmp_path / "census.json"
    status = census_module.main(
        [
            str(folded),
            "--anchor",
            "emit_line@cuprum/_line_callbacks.py@extra",
            "--target",
            "isawaitable@inspect.py",
            "--min-anchor-weight",
            "1",
            "--output",
            str(output),
        ],
    )

    assert status == 2, "an over-specified anchor must be rejected"
    assert not output.exists(), "a rejected run must write no result"


def test_negative_floor_is_rejected(tmp_path: pth.Path) -> None:
    """A negative floor would make every capture vacuously pass."""
    capture = _line(_ANCHOR_TEXT, samples=1)
    status, _ = _run(tmp_path, capture, floor=-1)

    assert status == 2, "a negative floor must be rejected"
