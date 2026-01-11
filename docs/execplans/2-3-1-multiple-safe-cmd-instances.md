# Execution plan: Concurrent SafeCmd execution (2.3.1)

## Summary

Add a helper to run multiple `SafeCmd` instances concurrently with optional
concurrency limits, while preserving hook semantics and providing aggregated
results.

## Deliverables

| File                                         | Status                           |
| -------------------------------------------- | -------------------------------- |
| `cuprum/concurrent.py`                       | Complete                         |
| `cuprum/__init__.py`                         | Updated with exports             |
| `cuprum/unittests/test_concurrency.py`       | Complete                         |
| `tests/features/concurrency_helper.feature`  | Complete                         |
| `tests/behaviour/test_concurrency_helper.py` | Complete                         |
| `docs/users-guide.md`                        | Updated with concurrency section |
| `docs/cuprum-design.md`                      | Updated with section 8.3.1       |
| `docs/roadmap.md`                            | Mark 2.3.1 complete              |

## API design

### Result type

```python
@dc.dataclass(frozen=True, slots=True)
class ConcurrentResult:
    """Aggregated result from concurrent command execution."""

    results: tuple[CommandResult, ...]
    failures: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        """Return True when all commands exited successfully."""
        return len(self.failures) == 0

    @property
    def first_failure(self) -> CommandResult | None:
        """Return the first failed result, or None if all succeeded."""
        if not self.failures:
            return None
        return self.results[self.failures[0]]
```

### Primary functions

```python
async def run_concurrent(
    *commands: SafeCmd,
    concurrency: int | None = None,
    capture: bool = True,
    echo: bool = False,
    context: ExecutionContext | None = None,
    fail_fast: bool = False,
) -> ConcurrentResult:
    """Execute multiple SafeCmd instances concurrently."""

def run_concurrent_sync(
    *commands: SafeCmd,
    concurrency: int | None = None,
    capture: bool = True,
    echo: bool = False,
    context: ExecutionContext | None = None,
    fail_fast: bool = False,
) -> ConcurrentResult:
    """Synchronous wrapper for run_concurrent."""
```

## Design decisions

| Decision             | Choice                     | Rationale                                          |
| -------------------- | -------------------------- | -------------------------------------------------- |
| Result ordering      | Submission order           | Predictable; matches user mental model             |
| Hook handling        | Per-command                | Consistent with existing `SafeCmd.run()` semantics |
| Concurrency limiting | `asyncio.Semaphore`        | Standard pattern; clean async integration          |
| Fail-fast            | `asyncio.TaskGroup`        | Structured cancellation; Python 3.11+              |
| Pre-flight allowlist | Check all before execution | Fail fast on forbidden programs                    |

## Implementation notes

### Concurrency limiting

When `concurrency=N` is provided, an `asyncio.Semaphore(N)` gates command
execution. Each command acquires the semaphore before spawning and releases it
after completion. When `concurrency=None`, all commands run in parallel without
limit.

### Failure modes

- **Collect-all mode** (default): All commands run to completion regardless of
  failures. The `ConcurrentResult.failures` tuple contains indices of commands
  that exited non-zero.
- **Fail-fast mode**: Uses `asyncio.TaskGroup` for structured cancellation.
  When a command exits non-zero, an internal exception triggers TaskGroup
  cancellation. Commands already running receive cancellation; commands not yet
  started are not scheduled.

### Cancellation semantics

- **External cancellation**: When the `run_concurrent` coroutine is cancelled,
  all running command tasks receive `CancelledError`. Each command's
  cancellation handler sends SIGTERM, waits the grace period, then SIGKILL.
- **Fail-fast cancellation**: First non-zero exit cancels pending commands;
  partial results are returned for completed commands.

## Error handling

| Exception                | When raised                                     |
| ------------------------ | ----------------------------------------------- |
| `ForbiddenProgramError`  | Pre-flight check finds forbidden program        |
| `ValueError`             | Empty commands or concurrency < 1               |
| `asyncio.CancelledError` | External cancellation; propagated after cleanup |

Command execution errors are captured in `CommandResult` and surfaced via
`ConcurrentResult.failures`.

## Testing summary

### Unit tests

- Basic functionality: returns `ConcurrentResult`, preserves submission order
- Concurrency limiting: semaphore restricts parallel execution
- Failure handling: collect-all continues; fail-fast cancels pending
- Hook semantics: before/after/observe hooks fire per command
- Validation: empty commands, concurrency < 1 raise `ValueError`
- Allowlist: forbidden program raises `ForbiddenProgramError` before execution

### Behavioural tests

- Execute commands concurrently and collect results
- Concurrency limit restricts parallel execution
- Failed commands are collected in results
- Fail-fast cancels pending commands
