"""Property and snapshot tests for the canonical stage-observation builder.

The env-overlay resolution and observation tag construction previously lived
in three near-identical copies (`cuprum/sh.py`, `cuprum/_process_lifecycle.py`,
`cuprum/_pipeline_internals.py`). `cuprum._observability._resolve_env_overlay`
and `_base_stage_tags` are now the single source of truth (#111); these tests
pin the contract:

- overlay resolution matches ``merge_env_overlays`` semantics for arbitrary
  scoped/per-call layers (including ``None``, empty, and overlapping keys),
  stays overlay-only, and is immutable;
- the single-command and pipeline observation tags agree on the shared keys
  (``project``, ``capture``, ``echo``) for arbitrary inputs, with the
  pipeline grafting only its stage keys; and
- the emitted tag dictionaries are locked with syrupy snapshots.
"""

from __future__ import annotations

import types
import typing as typ

from hypothesis import given
from hypothesis import strategies as st

from cuprum import ECHO, sh
from cuprum._observability import _base_stage_tags, _resolve_env_overlay
from cuprum._pipeline_internals import (
    _build_pipeline_observations,
    _run_before_hooks,
)
from cuprum._testing import _prepare_pipeline_config
from cuprum.context import env, merge_env_overlays
from cuprum.sh import (
    ExecutionContext,
    RunOutputOptions,
    _ExecutionTracking,
    _prepare_execution_observation,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from syrupy.assertion import SnapshotAssertion

_ENV_KEYS = st.text(
    alphabet="ABCDEFGH",
    min_size=1,
    max_size=3,
).map("CUPRUM_TEST_{}".format)
_ENV_OVERLAYS = st.none() | st.dictionaries(_ENV_KEYS, st.text(max_size=5), max_size=4)
_TAG_VALUES = st.text(max_size=5) | st.integers(min_value=0, max_value=9)
_PIPELINE_STAGE_TAG_KEYS = frozenset(
    {"pipeline_stage_index", "pipeline_stages"},
)
_TAGS = st.none() | st.dictionaries(
    st.sampled_from(
        [
            "team",
            "ticket",
            "project",
            "capture",
            "echo",
            *_PIPELINE_STAGE_TAG_KEYS,
        ],
    ),
    _TAG_VALUES,
    max_size=3,
)


@given(scoped_overlay=_ENV_OVERLAYS, extra=_ENV_OVERLAYS)
def test_resolve_env_overlay_matches_merge_semantics(
    scoped_overlay: dict[str, str] | None,
    extra: dict[str, str] | None,
) -> None:
    """Property: resolution equals the documented overlay merge, frozen.

    Parameters
    ----------
    scoped_overlay : dict[str, str] | None
        Generated overlay installed on the active context via ``env()``.
    extra : dict[str, str] | None
        Generated per-call overlay layered on top.
    """
    if scoped_overlay is not None:
        with env(scoped_overlay):
            resolved = _resolve_env_overlay(extra)
            expected = merge_env_overlays(scoped_overlay, extra)
    else:
        resolved = _resolve_env_overlay(extra)
        expected = merge_env_overlays(None, extra)

    if expected is None:
        assert resolved is None
    else:
        assert resolved is not None
        assert dict(resolved) == dict(expected)
        assert isinstance(resolved, types.MappingProxyType), (
            "the resolved overlay must be immutable"
        )


def _single_command_tags(
    cmd: sh.SafeCmd,
    context: ExecutionContext,
    *,
    capture: bool,
    echo: bool,
) -> cabc.Mapping[str, object]:
    """Build the single-command observation and return its tags."""
    tracking = _ExecutionTracking(
        execution_hooks=_run_before_hooks(cmd),
        pending_tasks=[],
    )
    observation = _prepare_execution_observation(
        cmd,
        context,
        tracking,
        RunOutputOptions(capture=capture, echo=echo),
    )
    return observation.tags


def _pipeline_tags(
    parts: tuple[sh.SafeCmd, ...],
    context: ExecutionContext,
    *,
    capture: bool,
    echo: bool,
) -> list[cabc.Mapping[str, object]]:
    """Build pipeline observations and return per-stage tags."""
    config = _prepare_pipeline_config(
        capture=capture,
        echo=echo,
        timeout=None,
        context=context,
    )
    observations = _build_pipeline_observations(parts, config, pending_tasks=[])
    return [observation.tags for observation in observations]


@given(
    capture=st.booleans(),
    echo=st.booleans(),
    ctx_tags=_TAGS,
)
def test_single_and_pipeline_tags_agree_on_shared_keys(
    *,
    capture: bool,
    echo: bool,
    ctx_tags: dict[str, object] | None,
) -> None:
    """Property: both paths emit identical shared tags from one builder.

    The single-command and pipeline observation tags must agree on the shared
    schema (``project``, ``capture``, ``echo``, merged per-call tags); the
    pipeline stages add only ``pipeline_stage_index`` and ``pipeline_stages``.

    Parameters
    ----------
    capture : bool
        Generated capture flag.
    echo : bool
        Generated echo flag.
    ctx_tags : dict[str, object] | None
        Generated per-call tags merged over the base schema.
    """
    cmd = sh.make(ECHO)("hello")
    context = ExecutionContext(tags=ctx_tags)

    single = dict(
        _single_command_tags(cmd, context, capture=capture, echo=echo),
    )
    assert single == {
        **_base_stage_tags(cmd, capture=capture, echo=echo),
        **(ctx_tags or {}),
    }
    stage_tags = _pipeline_tags((cmd, cmd), context, capture=capture, echo=echo)
    shared_single = {
        key: value
        for key, value in single.items()
        if key not in _PIPELINE_STAGE_TAG_KEYS
    }

    for idx, tags in enumerate(stage_tags):
        shared = {
            key: value
            for key, value in tags.items()
            if key not in _PIPELINE_STAGE_TAG_KEYS
        }
        assert shared == shared_single, (
            "single-command and pipeline observations must agree on shared tags"
        )
        assert tags["pipeline_stage_index"] == (ctx_tags or {}).get(
            "pipeline_stage_index", idx
        ), "pipeline_stage_index must respect caller-tag precedence"
        assert tags["pipeline_stages"] == (ctx_tags or {}).get(
            "pipeline_stages",
            2,
        ), "pipeline_stages must respect caller-tag precedence"


@given(scoped_overlay=_ENV_OVERLAYS, extra=_ENV_OVERLAYS)
def test_overlay_resolution_is_order_insensitive_for_disjoint_layers(
    scoped_overlay: dict[str, str] | None,
    extra: dict[str, str] | None,
) -> None:
    """Property: disjoint scoped/per-call layers merge order-insensitively.

    Parameters
    ----------
    scoped_overlay : dict[str, str] | None
        Generated scoped overlay layer.
    extra : dict[str, str] | None
        Generated per-call overlay layer.
    """
    left = merge_env_overlays(scoped_overlay, extra)
    right = merge_env_overlays(extra, scoped_overlay)
    overlapping = set(scoped_overlay or {}) & set(extra or {})
    if not overlapping:
        assert (left is None) == (right is None)
        if left is not None and right is not None:
            assert dict(left) == dict(right)


def test_stage_tag_snapshots_lock_the_wire_contract(
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot: representative single-command and pipeline tag dictionaries.

    Locks the observation tag wire contract for a representative single
    command and a two-stage pipeline. All inputs are deterministic (no pid or
    cwd enters the tags), so no redaction is required.
    """
    cmd = sh.make(ECHO)("hello")
    context = ExecutionContext(tags={"team": "ops"})

    single = dict(_single_command_tags(cmd, context, capture=True, echo=False))
    pipeline = [
        dict(tags)
        for tags in _pipeline_tags((cmd, cmd), context, capture=True, echo=False)
    ]

    assert {"single": single, "pipeline": pipeline} == snapshot


def test_base_stage_tags_schema() -> None:
    """Example: the canonical base schema is exactly project/capture/echo."""
    cmd = sh.make(ECHO)("hello")
    tags = _base_stage_tags(cmd, capture=True, echo=False)
    assert tags == {
        "project": cmd.project.name,
        "capture": True,
        "echo": False,
    }
