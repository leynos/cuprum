"""Unit and property tests for optional child-resource accounting."""

from __future__ import annotations

import types

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum import _rusage


def _snapshot(
    max_rss_bytes: int = 0,
    user_cpu_seconds: float = 0.0,
    system_cpu_seconds: float = 0.0,
) -> _rusage._ChildRusageSnapshot:
    """Build a child-resource snapshot with concise test defaults."""
    return _rusage._ChildRusageSnapshot(
        max_rss_bytes=max_rss_bytes,
        user_cpu_seconds=user_cpu_seconds,
        system_cpu_seconds=system_cpu_seconds,
    )


def test_measurement_is_unavailable_without_resource_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platforms without ``resource`` report unavailable child accounting."""
    monkeypatch.setattr(_rusage, "resource", None)

    assert _rusage.child_resource_measurement_available() is False, (
        "a missing resource module must not count as child accounting"
    )
    assert _rusage.capture_child_rusage() is None, (
        "a missing resource module must yield no snapshot"
    )


@pytest.mark.parametrize("missing_attribute", ["RUSAGE_CHILDREN", "getrusage"])
def test_measurement_is_unavailable_without_required_api(
    monkeypatch: pytest.MonkeyPatch,
    missing_attribute: str,
) -> None:
    """Partial resource APIs do not enable child-resource accounting."""
    attributes: dict[str, object] = {
        "RUSAGE_CHILDREN": object(),
        "getrusage": lambda _: None,
    }
    del attributes[missing_attribute]
    monkeypatch.setattr(_rusage, "resource", types.SimpleNamespace(**attributes))

    assert _rusage.child_resource_measurement_available() is False, (
        f"a resource module without {missing_attribute} must not enable accounting"
    )
    assert _rusage.capture_child_rusage() is None, (
        f"a resource module without {missing_attribute} must yield no snapshot"
    )


def test_capture_returns_none_when_getrusage_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient child-resource failures leave measurement unavailable."""

    def raise_os_error(_: object) -> None:
        """Raise the platform error exposed by ``getrusage``."""
        raise OSError

    monkeypatch.setattr(
        _rusage,
        "resource",
        types.SimpleNamespace(RUSAGE_CHILDREN=object(), getrusage=raise_os_error),
    )

    assert _rusage.capture_child_rusage() is None, (
        "a getrusage failure must be reported as no measurement, not raised"
    )


@pytest.mark.parametrize(
    ("platform", "expected_rss_bytes"),
    [("linux", 4096), ("darwin", 4)],
)
def test_capture_normalizes_platform_rss_units(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    expected_rss_bytes: int,
) -> None:
    """Linux RSS KiB become bytes while Darwin's byte value stays raw."""
    usage = types.SimpleNamespace(ru_maxrss=4, ru_utime=1.25, ru_stime=2.5)
    monkeypatch.setattr(
        _rusage,
        "resource",
        types.SimpleNamespace(RUSAGE_CHILDREN=object(), getrusage=lambda _: usage),
    )
    monkeypatch.setattr(_rusage.sys, "platform", platform)

    snapshot = _rusage.capture_child_rusage()

    assert snapshot == _snapshot(
        max_rss_bytes=expected_rss_bytes,
        user_cpu_seconds=1.25,
        system_cpu_seconds=2.5,
    ), (
        f"{platform} RSS of 4 must normalize to {expected_rss_bytes} bytes, "
        f"got {snapshot!r}"
    )


@pytest.mark.parametrize(
    ("platform", "expected_rss_bytes"),
    [("linux", 4096), ("darwin", 4)],
)
def test_wait4_usage_normalizes_platform_rss_units(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    expected_rss_bytes: int,
) -> None:
    """Child-specific wait4 usage preserves the platform RSS unit contract."""
    usage = types.SimpleNamespace(ru_maxrss=4, ru_utime=1.25, ru_stime=2.5)
    monkeypatch.setattr(_rusage.sys, "platform", platform)

    direct = _rusage.resource_usage_from_wait4(usage)

    assert direct == _rusage.ChildResourceUsage(
        max_rss_bytes=expected_rss_bytes,
        user_cpu_seconds=1.25,
        system_cpu_seconds=2.5,
    ), (
        f"{platform} wait4 RSS of 4 must normalize to {expected_rss_bytes} bytes, "
        f"got {direct!r}"
    )


def test_wait4_measurement_is_unavailable_without_wait4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platforms without a child-specific reaper do not claim direct usage."""
    monkeypatch.delattr(_rusage.os, "wait4", raising=False)

    assert _rusage.wait4_resource_measurement_available() is False, (
        "a platform without os.wait4 must not claim direct-child measurement"
    )


def test_delta_requires_two_snapshots() -> None:
    """Missing accounting boundaries cannot produce a resource delta."""
    snapshot = _snapshot()

    assert _rusage.child_rusage_delta(None, snapshot) is None, (
        "a missing pre-spawn snapshot must yield no delta"
    )
    assert _rusage.child_rusage_delta(snapshot, None) is None, (
        "a missing post-exit snapshot must yield no delta"
    )


def test_delta_returns_cpu_usage_without_attributable_rss() -> None:
    """CPU counters accumulate, unlike the RSS high-water mark."""
    result = _rusage.child_rusage_delta(
        _snapshot(max_rss_bytes=4_096, user_cpu_seconds=1.0, system_cpu_seconds=2.0),
        _snapshot(max_rss_bytes=8_192, user_cpu_seconds=4.0, system_cpu_seconds=7.0),
    )

    assert result == _rusage.ChildResourceUsage(
        max_rss_bytes=None,
        user_cpu_seconds=3.0,
        system_cpu_seconds=5.0,
    ), f"an aggregate delta must carry CPU only, got {result!r}"


def test_delta_clamps_decreasing_cpu_counters() -> None:
    """Accounting regressions do not publish negative CPU usage."""
    result = _rusage.child_rusage_delta(
        _snapshot(user_cpu_seconds=3.0, system_cpu_seconds=4.0),
        _snapshot(user_cpu_seconds=1.0, system_cpu_seconds=2.0),
    )

    assert result == _rusage.ChildResourceUsage(
        max_rss_bytes=None,
        user_cpu_seconds=0.0,
        system_cpu_seconds=0.0,
    ), f"a regressing counter must clamp to zero, got {result!r}"


_SNAPSHOTS = st.builds(
    _snapshot,
    max_rss_bytes=st.integers(min_value=0, max_value=2**32),
    user_cpu_seconds=st.floats(
        min_value=0.0,
        max_value=10_000.0,
        allow_nan=False,
        allow_infinity=False,
    ),
    system_cpu_seconds=st.floats(
        min_value=0.0,
        max_value=10_000.0,
        allow_nan=False,
        allow_infinity=False,
    ),
)


def test_mode_is_unavailable_without_a_usage_record() -> None:
    """No source produced a record, so the mode says exactly that."""
    assert _rusage.resource_usage_mode_for(None) == "unavailable", (
        "no usage record must classify as unavailable"
    )


def test_mode_names_the_source_of_each_producer() -> None:
    """Each producer's own output is classified as the mode it is."""
    wait4 = _rusage.ChildResourceUsage(
        max_rss_bytes=4_194_304,
        user_cpu_seconds=0.5,
        system_cpu_seconds=0.25,
    )
    aggregate = _rusage.child_rusage_delta(_snapshot(), _snapshot())

    assert _rusage.resource_usage_mode_for(wait4) == "wait4_child", (
        "an attributable measurement must classify as wait4_child"
    )
    assert aggregate is not None, "two snapshots must produce an aggregate record"
    assert _rusage.resource_usage_mode_for(aggregate) == "aggregate_cpu_delta", (
        "a CPU-only delta must classify as aggregate_cpu_delta"
    )


@given(snapshots=st.tuples(_SNAPSHOTS, _SNAPSHOTS))
def test_mode_reads_the_invariant_that_separates_the_producers(
    snapshots: tuple[_rusage._ChildRusageSnapshot, _rusage._ChildRusageSnapshot],
) -> None:
    """The mode never contradicts the RSS figure it accompanies.

    The classifier distinguishes the two producers by exactly one thing: the
    attributable path always publishes an RSS figure and the aggregate path
    never does. This pins that correspondence, so a future producer that
    reported neither, or reported RSS without being attributable, would not
    silently be mislabelled as one of the two.
    """
    before, after = snapshots
    aggregate = _rusage.child_rusage_delta(before, after)
    assert aggregate is not None, "two snapshots must produce an aggregate record"

    assert _rusage.resource_usage_mode_for(aggregate) == "aggregate_cpu_delta", (
        f"the aggregate path must classify as aggregate_cpu_delta, got "
        f"{_rusage.resource_usage_mode_for(aggregate)!r}"
    )
    assert aggregate.max_rss_bytes is None, (
        "the aggregate path must never claim an attributable RSS figure"
    )


@given(before=st.none() | _SNAPSHOTS, after=st.none() | _SNAPSHOTS)
def test_delta_property_preserves_cpu_and_rss_invariants(
    before: _rusage._ChildRusageSnapshot | None,
    after: _rusage._ChildRusageSnapshot | None,
) -> None:
    """CPU deltas stay non-negative and RSS remains unavailable per command."""
    result = _rusage.child_rusage_delta(before, after)

    if before is None or after is None:
        assert result is None, (
            f"a missing boundary must yield no delta, got {result!r} for "
            f"before={before!r}, after={after!r}"
        )
        return

    assert result is not None, (
        f"two snapshots must produce a delta for before={before!r}, after={after!r}"
    )
    assert result.max_rss_bytes is None, (
        f"the aggregate path must never carry RSS, got {result.max_rss_bytes!r}"
    )
    assert result.user_cpu_seconds == max(
        0.0,
        after.user_cpu_seconds - before.user_cpu_seconds,
    ), (
        f"the user-CPU delta must be the clamped difference, got "
        f"{result.user_cpu_seconds!r} for before={before!r}, after={after!r}"
    )
    assert result.system_cpu_seconds == max(
        0.0,
        after.system_cpu_seconds - before.system_cpu_seconds,
    ), (
        f"the system-CPU delta must be the clamped difference, got "
        f"{result.system_cpu_seconds!r} for before={before!r}, after={after!r}"
    )
    assert result.user_cpu_seconds >= 0.0, (
        f"the user-CPU delta must never go negative, got {result.user_cpu_seconds!r}"
    )
    assert result.system_cpu_seconds >= 0.0, (
        "the system-CPU delta must never go negative, got "
        f"{result.system_cpu_seconds!r}"
    )
