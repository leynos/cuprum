# pyscn Clone Detection Findings

This document categorizes the code clones identified by pyscn (with similarity
threshold 0.95) and provides analysis of which findings are false positives
versus potentially meritorious.

## Executive Summary

- **Total clones detected**: 77 (informational, non-blocking)
- **100% similarity clones**: 2 (both false positives)
- **False positives**: ~65 (structural patterns, not actual duplication)
- **Potentially meritorious**: ~12 (similar patterns that could be reviewed)

The majority of detected clones are **idiomatic Python patterns** that pyscn
identifies as structurally similar but which serve different purposes and
should not be deduplicated.

---

## Category 1: False Positives — Dataclass Patterns

**Pattern**: `@dc.dataclass(frozen=True, slots=True)` with NumPy-style
docstrings and properties.

**Why false positive**: Frozen dataclasses with slots are an intentional
design pattern in this codebase for immutable value objects. The structural
similarity (decorator + docstring + fields + properties) is expected and
desirable—these are not candidates for deduplication.

### Affected Locations

| Location | Description |
|----------|-------------|
| `cuprum/sh.py:90` | `CommandResult` — execution result |
| `cuprum/sh.py:124` | `PipelineResult` — pipeline execution result |
| `cuprum/sh.py:408` | `Pipeline` — pipeline composition |
| `cuprum/context.py:73` | `ScopeConfig` — scope configuration |
| `cuprum/context.py:106` | `CuprumContext` — execution context |
| `cuprum/concurrent.py:32` | `ConcurrentConfig` — concurrency settings |
| `cuprum/concurrent.py:72` | `ConcurrentResult` — concurrent execution result |
| `cuprum/events.py:23` | `ExecEvent` — structured execution event |
| `cuprum/catalogue.py:56` | `_CatalogueEntry` — internal catalogue entry |
| `cuprum/_pipeline_internals.py:116` | `_PipelineSpawnResult` — spawn result |
| `cuprum/_pipeline_streams.py:96` | `_PipelineOutputs` — output collection |
| `cuprum/_process_lifecycle.py:150` | `_WaitConfig` — wait configuration |
| `cuprum/adapters/metrics_adapter.py:120` | `MetricPoint` — metrics data point |
| `cuprum/adapters/tracing_adapter.py:161` | `Span` — tracing span |

**Recommendation**: No action required. These are distinct domain objects that
happen to share Python's dataclass idiom.

---

## Category 2: False Positives — Registration Class Patterns

**Pattern**: Classes with `__slots__`, `__init__`, `detach()`, `__enter__`,
and `__exit__` methods for context manager support.

**Why false positive**: The codebase uses a consistent pattern for
"registration handles" that allow both explicit `detach()` and context manager
(`with`) usage. This is intentional API design, not code duplication.

### Affected Locations

| Location | Description |
|----------|-------------|
| `cuprum/context.py:322` | `AllowRegistration` — allowlist registration |
| `cuprum/context.py:398` | `HookRegistration` — hook registration |
| `cuprum/logging_hooks.py:19` | `LoggingHookRegistration` — logging hook registration |
| `cuprum/logging_hooks.py:93` | `_PairedHookRegistration` — paired hook registration |
| `cuprum/adapters/logging_adapter.py:63` | `LoggingAdapter` — adapter registration |

**Recommendation**: No action required. These classes implement a deliberate
API pattern. Extracting a base class would add complexity without benefit, as
each registration type has different initialization and cleanup logic.

---

## Category 3: False Positives — Test Setup Patterns

**Pattern**: Common test setup code such as
`with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):`.

**Why false positive**: Tests legitimately reuse the same setup pattern.
This is not duplication—it's consistent test structure. Extracting to a
fixture would reduce test readability without meaningful benefit.

### 100% Similarity Clones

| Clone A | Clone B | Pattern |
|---------|---------|---------|
| `test_logging_hook.py:22` | `test_logging_hook.py:113` | `with scoped(ScopeConfig(...)):` context manager |
| `test_safe_cmd_run.py:308` | `test_execution_runtime.py:262` | `async def orchestrate()` async test helper |

**Analysis of 100% clones**:

1. **test_logging_hook.py:22 ↔ 113**: Both tests use
   `with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):` but test different
   behaviors (registration vs context manager usage). This is standard test
   isolation, not duplication.

2. **test_safe_cmd_run.py:308 ↔ test_execution_runtime.py:262**: Both define
   `async def orchestrate()` helpers that create a task, wait for a PID file,
   then cancel. However:
   - The unit test uses inline polling logic
   - The behaviour test uses a shared `_wait_for_pid()` helper
   - They serve different test categories with different design philosophies

**Recommendation**: No action required. These patterns are acceptable test
structure.

---

## Category 4: False Positives — Async Function Patterns

**Pattern**: Async functions with similar structure (create task, await,
handle cancellation).

**Why false positive**: Async patterns in Python naturally have structural
similarity due to the language's async/await syntax. Functions like
`_run_collect_all`, `_run_fail_fast`, and `_wait_for_pipeline` share structure
but implement different concurrent execution strategies.

### Affected Locations

| Location | Description |
|----------|-------------|
| `cuprum/concurrent.py:131` | `_run_collect_all` — collect-all execution strategy |
| `cuprum/concurrent.py:166` | `_build_final_results` — result processing |
| `cuprum/concurrent.py:199` | `_run_fail_fast` — fail-fast execution strategy |
| `cuprum/_pipeline_wait.py:89` | `_wait_for_pipeline` — pipeline wait logic |
| `cuprum/_subprocess_execution.py:286` | `_run_subprocess` — subprocess execution |

**Recommendation**: No action required. These implement distinct execution
strategies that happen to follow Python async patterns.

---

## Category 5: False Positives — Adapter/Hook Patterns

**Pattern**: Telemetry adapters and hooks with similar observation patterns.

**Why false positive**: The adapters (logging, metrics, tracing) intentionally
implement the same interface (`ExecHook`) with similar structure but different
backends. This is the adapter pattern in action.

### Affected Locations

| Location | Description |
|----------|-------------|
| `cuprum/adapters/logging_adapter.py:124` | Logging adapter hook |
| `cuprum/adapters/metrics_adapter.py:168` | Metrics adapter hook |
| `cuprum/adapters/tracing_adapter.py:192` | Tracing adapter hook |
| `cuprum/adapters/tracing_adapter.py:228` | `TracingHook` class |
| `cuprum/adapters/tracing_adapter.py:301` | Span creation logic |
| `cuprum/adapters/tracing_adapter.py:339` | Span finalization logic |

**Recommendation**: No action required. Interface conformance naturally
produces structural similarity.

---

## Category 6: Potentially Meritorious — Same-File Clones

These clones are within the same file and might benefit from consolidation,
though the current implementations are acceptable.

### `cuprum/_pipeline_internals.py`

| Clone A | Clone B | Similarity | Analysis |
|---------|---------|------------|----------|
| Line 186 | Line 261 | 96.8% | `_collect_pipeline_inputs` and `_build_pipeline_stage_results` both process pipeline stages |
| Line 116 | Line 310 | 95.6% | `_PipelineSpawnResult` dataclass and `_run_pipeline` function |

**Analysis**: These represent different phases of pipeline execution (spawn,
collect, build results). The similarity comes from processing the same data
structures. Consolidating would likely reduce clarity.

**Recommendation**: Accept as-is. The phases are logically distinct.

### `cuprum/context.py`

| Clone A | Clone B | Similarity | Analysis |
|---------|---------|------------|----------|
| Line 73 | Line 322 | 95.1% | `ScopeConfig` dataclass vs `AllowRegistration` class |
| Line 73 | Line 398 | 95.1% | `ScopeConfig` dataclass vs `HookRegistration` class |
| Line 322 | Line 398 | 96.7% | `AllowRegistration` vs `HookRegistration` |

**Analysis**: `AllowRegistration` and `HookRegistration` could theoretically
share a base class, but they manage different resources (allowlist vs hooks)
with different restoration logic. A shared base would require complex
generics for minimal benefit.

**Recommendation**: Accept as-is. The classes are intentionally parallel.

### `cuprum/unittests/test_adapters.py`

| Clone A | Clone B | Similarity | Analysis |
|---------|---------|------------|----------|
| Line 47 | Line 264 | 95.0% | Test helper patterns |
| Line 169 | Line 318 | 96.8% | `TestMetricsHook` vs `TestTracingHook` test classes |

**Analysis**: The test classes for different adapters naturally have similar
structure (setup, execute command, verify adapter behavior). This is
appropriate test organization.

**Recommendation**: Accept as-is. Parameterizing would reduce test clarity.

---

## Category 7: Potentially Meritorious — Cross-Module Test Patterns

These clones span unit tests and behaviour tests, testing similar scenarios.

### Process Builder Patterns

| Location | Description |
|----------|-------------|
| `test_safe_cmd_run.py:277` | Unit test for safe command execution |
| `test_safe_cmd_run.py:393` | Unit test for timeout handling |
| `test_safe_cmd_run.py:417` | Unit test for cancellation |
| `test_safe_cmd_run.py:479` | Unit test for process lifecycle |
| `test_pipeline_execution.py:92` | Behaviour test for pipeline execution |

**Analysis**: These tests use similar Python subprocess builders to create
test commands. The similarity is in test setup, not in what's being tested.

**Recommendation**: Could consider extracting a shared test helper for
creating Python subprocess commands, but the current approach (each test
file having its own builder) provides better test isolation and readability.

### Context Hook Test Patterns

| Location | Description |
|----------|-------------|
| `test_context.py:257` | Unit test for before hook ordering |
| `test_context.py:288` | Unit test for after hook ordering |
| `test_context.py:324` | Unit test for thread isolation |
| `test_context.py:347` | Unit test for async task isolation |
| `test_context_hooks.py:151` | BDD step for multi-before hooks |
| `test_context_hooks.py:199` | BDD step for multi-after hooks |
| `test_context_hooks.py:298` | BDD step for hook context |
| `test_context_hooks.py:345` | BDD step for allowlist checking |

**Analysis**: These test different aspects of the context system. The
structural similarity comes from testing the same API from different angles
(unit vs behaviour, before vs after, threading vs async).

**Recommendation**: Accept as-is. The tests verify different invariants.

---

## Summary and Recommendations

### No Action Required

The following categories are **false positives** that should not be addressed:

1. **Dataclass patterns** — Intentional immutable value object design
2. **Registration class patterns** — Deliberate API pattern
3. **Test setup patterns** — Standard test isolation
4. **Async function patterns** — Language-inherent structural similarity
5. **Adapter/hook patterns** — Interface conformance

### Low Priority / Optional Review

The following could be reviewed but are acceptable as-is:

1. **`AllowRegistration` vs `HookRegistration`** — Could share a base class,
   but complexity cost exceeds benefit
2. **Test adapter classes** — Could use parameterization, but would reduce
   test clarity

### Configuration Recommendations

The current pyscn configuration is appropriate:

```toml
[clones]
similarity_threshold = 0.95  # Catches only high-similarity clones
```

To further reduce noise, consider:

```toml
[clones]
similarity_threshold = 0.97  # Only near-exact duplicates
min_lines = 10               # Require larger code blocks
enabled_clone_types = ["type1", "type2"]  # Skip structural similarity
```

### Future Improvements

Consider filing a feature request with pyscn for:

1. **Inline suppression comments** (like `# noqa` for ruff)
2. **Per-clone ignore patterns** by file/line
3. **Baseline file support** to suppress known acceptable clones

---

*Generated: 2026-01-20*
*pyscn threshold: 0.95*
*Total clones: 77 (informational)*
