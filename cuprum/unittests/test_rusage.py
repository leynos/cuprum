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

    assert _rusage.child_resource_measurement_available() is False
    assert _rusage.capture_child_rusage() is None


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

    assert _rusage.child_resource_measurement_available() is False
    assert _rusage.capture_child_rusage() is None


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

    assert _rusage.capture_child_rusage() is None


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

    assert _rusage.capture_child_rusage() == _snapshot(
        max_rss_bytes=expected_rss_bytes,
        user_cpu_seconds=1.25,
        system_cpu_seconds=2.5,
    )


def test_delta_requires_two_snapshots() -> None:
    """Missing accounting boundaries cannot produce a resource delta."""
    snapshot = _snapshot()

    assert _rusage.child_rusage_delta(None, snapshot) is None
    assert _rusage.child_rusage_delta(snapshot, None) is None


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
    )


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
    )


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


@given(before=st.none() | _SNAPSHOTS, after=st.none() | _SNAPSHOTS)
def test_delta_property_preserves_cpu_and_rss_invariants(
    before: _rusage._ChildRusageSnapshot | None,
    after: _rusage._ChildRusageSnapshot | None,
) -> None:
    """CPU deltas stay non-negative and RSS remains unavailable per command."""
    result = _rusage.child_rusage_delta(before, after)

    if before is None or after is None:
        assert result is None
        return

    assert result is not None
    assert result.max_rss_bytes is None
    assert result.user_cpu_seconds == max(
        0.0,
        after.user_cpu_seconds - before.user_cpu_seconds,
    )
    assert result.system_cpu_seconds == max(
        0.0,
        after.system_cpu_seconds - before.system_cpu_seconds,
    )
    assert result.user_cpu_seconds >= 0.0
    assert result.system_cpu_seconds >= 0.0
