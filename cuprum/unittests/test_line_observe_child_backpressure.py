"""Child-write backpressure, distinct from a reader-side delivery pause.

The blocked-hook matrix proves that delivery stops at saturation. That alone
could mean the parent stopped dispatching while the child kept writing into an
ever-growing buffer. This module proves the stronger claim: the child itself
stops writing, because the parent stops reading and the pipe fills.

The child appends a progress marker to a file as it writes, and the output is
deliberately larger than the operating system's pipe buffers. A marker that
stops advancing while the observer is blocked is evidence the child is blocked
in ``write`` rather than merely slow to produce.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum import sh
from cuprum._observe_task_retention import _OBSERVE_TASK_CEILING
from cuprum.context import ScopeConfig, scoped
from cuprum.unittests.test_line_observe_support import (
    _GatedHook,
    _output_options,
    _RetentionProbe,
)
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from cuprum.catalogue import ProgramCatalogue
    from cuprum.sh import SafeCmd

# Enough 1 KiB lines to exceed any plausible pipe buffer, plus the parent-side
# line queue, while leaving the child far short of finishing before it blocks.
_CHILD_WRITE_LINES = 4_000
_PROGRESS_EVERY = 50
_PAYLOAD = "x" * 1023
_STALL_TIMEOUT = 5.0


def _progress_command(progress_path: Path) -> tuple[SafeCmd, ProgramCatalogue]:
    """Build a child that floods stdout and records its own write progress."""
    catalogue, program = python_catalogue()
    build = sh.make(program, catalogue=catalogue)
    script = "\n".join([
        "import sys",
        f"progress = open({str(progress_path)!r}, 'w', buffering=1)",
        f"payload = {_PAYLOAD!r}",
        f"for i in range({_CHILD_WRITE_LINES}):",
        "    sys.stdout.write(f'{i:05d}{payload}\\n')",
        "    sys.stdout.flush()",
        f"    if i % {_PROGRESS_EVERY} == 0:",
        "        progress.write(f'{i}\\n')",
        "        progress.flush()",
        "progress.write('done\\n')",
        "progress.flush()",
    ])
    return build("-c", script), catalogue


def _progress_lines(path: Path) -> list[str]:
    """Return the progress markers the child has flushed so far."""
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


async def _await_progress_stall(path: Path) -> list[str]:
    """Wait until the child's progress file stops changing."""
    previous = _progress_lines(path)
    try:
        async with asyncio.timeout(_STALL_TIMEOUT):
            while True:
                await asyncio.sleep(0.05)
                current = _progress_lines(path)
                if current and current == previous:
                    return current
                previous = current
    except TimeoutError as exc:
        msg = f"the child's progress never stalled within {_STALL_TIMEOUT}s"
        raise AssertionError(msg) from exc


def _assert_child_delivery(seen: list[str], probe: _RetentionProbe) -> None:
    """Assert every numbered line arrived complete, in order, and bounded."""
    assert len(seen) == _CHILD_WRITE_LINES, (
        f"every line must arrive; got {len(seen)} of {_CHILD_WRITE_LINES}"
    )
    assert [line[:5] for line in seen] == [
        f"{index:05d}" for index in range(_CHILD_WRITE_LINES)
    ], "every line must arrive in order"
    assert all(line[5:] == _PAYLOAD for line in seen), "every line must arrive complete"
    assert probe.emissions >= _CHILD_WRITE_LINES, "every line event must be emitted"
    assert probe.high_water <= _OBSERVE_TASK_CEILING, (
        f"retained tasks reached {probe.high_water}, above the ceiling "
        f"{_OBSERVE_TASK_CEILING}"
    )
    assert probe.high_water < _CHILD_WRITE_LINES // 4, (
        "the retained list must stay far below the line count; "
        f"got {probe.high_water} for {_CHILD_WRITE_LINES} lines"
    )


def test_blocked_observer_stops_the_child_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A blocked observer must block the child's writes, not only delivery."""
    asyncio.run(_child_write_case(monkeypatch, tmp_path))


async def _child_write_case(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Saturate the observer, prove the child stops writing, then release."""
    progress_path = tmp_path / "child-progress.txt"
    cmd, catalogue = _progress_command(progress_path)
    hook = _GatedHook()
    probe = _RetentionProbe()
    probe.install(monkeypatch)

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(hook),
    ):
        run_task = asyncio.create_task(
            cmd.run(output=_output_options(capture=False)),
        )
        await asyncio.wait_for(probe.saturated.wait(), timeout=10.0)

        stalled = await _await_progress_stall(progress_path)
        assert "done" not in stalled, (
            "the child must not finish writing while the observer is blocked"
        )
        assert len(stalled) < _CHILD_WRITE_LINES // _PROGRESS_EVERY, (
            "the child must have stopped before writing every chunk; "
            f"progress reached {len(stalled)} chunks"
        )
        assert not run_task.done(), "the run must still be waiting on the child"

        await asyncio.sleep(0.2)
        assert _progress_lines(progress_path) == stalled, (
            "a blocked observer must stop the child writing, not only delivery"
        )

        hook.gate.set()
        await asyncio.wait_for(run_task, timeout=30.0)

    _assert_child_delivery(hook.seen, probe)
    assert _progress_lines(progress_path)[-1] == "done", (
        "the child must finish writing once the observer is released"
    )
