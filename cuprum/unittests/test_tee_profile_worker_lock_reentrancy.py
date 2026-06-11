"""Tests that ``_BACKEND_LOCK`` is an ``RLock`` and supports same-thread reacquisition."""  # noqa: E501

from __future__ import annotations

from benchmarks import tee_profile_worker


def test_backend_lock_is_reentrant() -> None:
    """The backend lock can be acquired twice by the same thread."""
    with tee_profile_worker._BACKEND_LOCK:
        # A plain Lock would deadlock on this same-thread second acquisition;
        # RLock tracks ownership and recursion depth, so it succeeds here.
        acquired = tee_profile_worker._BACKEND_LOCK.acquire(
            blocking=True,
            timeout=0.5,
        )
        assert acquired, "expected same-thread backend lock acquisition to succeed"
        tee_profile_worker._BACKEND_LOCK.release()
