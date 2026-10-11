"""Regression coverage for subprocess return-code publication."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum._subprocess_wait import _wait_for_exit_code
from cuprum.sh import ExecutionContext

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _PublishedExitProcess:
    """Process double whose waiter strands after publishing an exit code."""

    def __init__(self) -> None:
        """Start without a published return code."""
        self.returncode: int | None = None

    async def wait(self) -> int:
        """Publish success, then model asyncio's orphan pending wait future."""
        for _ in range(3):
            await asyncio.sleep(0)
        self.returncode = 0
        await asyncio.Event().wait()
        return 0


def test_wait_for_exit_code_accepts_published_returncode() -> None:
    """A published return code wins over an orphan pending wait future."""

    async def run_case() -> None:
        """Wait for the stranded process double without hanging."""
        process = typ.cast("asyncio.subprocess.Process", _PublishedExitProcess())

        exit_code, _ = await asyncio.wait_for(
            _wait_for_exit_code(process, ExecutionContext(), owns_group=False),
            timeout=0.5,
        )

        assert exit_code == 0, f"published exit code must be returned, got {exit_code}"

    asyncio.run(run_case())


def test_wait_for_exit_code_requires_ownership_to_be_stated() -> None:
    """Omitting ``owns_group`` is a call-site error, not a silent downgrade."""

    async def run_case() -> None:
        """Call the helper through a signature that does not state ownership."""
        process = typ.cast("asyncio.subprocess.Process", _PublishedExitProcess())
        # The keyword is deliberately absent: a caller that forgets it must be
        # told so. Widening the signature through ``cast`` keeps the call
        # honest to the checker while the runtime contract stays under test.
        omitting = typ.cast(
            "cabc.Callable[..., cabc.Coroutine[typ.Any, typ.Any, tuple[int, float]]]",
            _wait_for_exit_code,
        )

        with pytest.raises(TypeError, match=r"owns_group"):
            await omitting(process, ExecutionContext())

    asyncio.run(run_case())
