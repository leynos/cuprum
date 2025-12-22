# ADR-001: Optional Rust extension for stream operations

______________________________________________________________________

## Status

Proposed

______________________________________________________________________

## Context

Cuprum's stream operations route data through Python's asyncio event loop in
4KB chunks (defined by `_READ_SIZE = 4096` in `cuprum/_streams.py`). The core
functions affected are:

- `_pump_stream()` – transfers data between pipeline stages with backpressure;
- `_consume_stream()` – reads subprocess output, optionally teeing to sinks;
- `_consume_stream_with_lines()` – handles line-by-line callbacks with
  incremental UTF-8 decoding.

For typical command execution (e.g. `git status`, `ls -l`), this overhead is
negligible. However, for high-throughput pipelines processing large data
volumes, the overhead accumulates significantly:

For a 1GB data stream:

- approximately 262,000 round-trips through the Python event loop;
- approximately 262,000 `bytes` object allocations;
- repeated buffer copying between Python and OS buffers;
- Global Interpreter Lock (GIL) contention when multiple asyncio tasks compete
  for CPU.

This bottleneck limits Cuprum's suitability for ETL workloads, log processing,
and data pipeline orchestration where gigabytes of data flow through pipeline
stages.

______________________________________________________________________

## Decision

Introduce a **strictly optional** Rust extension for stream operations using
PyO3 and maturin. The extension provides alternative implementations of the
pump and consume functions that operate outside the GIL.

### Key design choices

1. **Pure Python remains first-class:** The existing asyncio implementation
   remains the reference implementation and default fallback. Both pathways
   receive equal testing and documentation attention.

2. **Runtime pathway selection:** A `CUPRUM_STREAM_BACKEND` environment
   variable controls which implementation is used:
   - `auto` (default) – use Rust if available, else Python;
   - `rust` – force Rust pathway; raise `ImportError` if unavailable;
   - `python` – force pure Python pathway.

3. **Larger default buffer for Rust:** The Rust extension uses a 64KB default
   buffer (vs 4KB in Python) to reduce syscall frequency whilst still providing
   reasonable memory overhead.

4. **Platform-specific optimisations:** On Linux, the extension leverages the
   `splice()` system call for zero-copy transfer between pipe file descriptors.
   Other platforms use an optimised read/write loop.

5. **File descriptor API boundary:** The Rust extension accepts raw file
   descriptors rather than asyncio stream objects, enabling operation outside
   the Python runtime whilst the dispatcher handles the translation.

______________________________________________________________________

## Alternatives considered

### Alternative 1: Direct C extension with Python C API

A traditional C extension using `Python.h` directly.

**Rejected because:**

- Higher implementation complexity and maintenance burden;
- Manual reference counting increases risk of memory leaks;
- Less ergonomic error handling compared to PyO3;
- Rust provides memory safety guarantees that C does not.

### Alternative 2: Cython

Compile Python-like code to C for performance improvement.

**Rejected because:**

- Still bound to the Python runtime semantics;
- Less control over GIL release timing;
- Does not provide the same level of performance for I/O-bound operations;
- Rust ecosystem has better tooling for cross-platform native builds.

### Alternative 3: External binary helper

Spawn a separate Rust binary that handles streaming, communicating via pipes or
sockets.

**Rejected because:**

- Inter-process communication (IPC) overhead partially negates gains;
- Deployment complexity increases (two binaries to distribute);
- Error handling across process boundaries is more complex;
- Lifecycle management adds significant implementation burden.

### Alternative 4: Increase Python buffer size only

Simply increase `_READ_SIZE` from 4KB to 64KB or larger.

**Rejected because:**

- Does not address fundamental event loop round-trip overhead;
- Does not address GIL contention in concurrent scenarios;
- Does not address Python object allocation churn;
- Provides marginal improvement for truly large data volumes.

### Alternative 5: Use existing libraries (e.g. uvloop)

Replace asyncio with uvloop for faster event loop performance.

**Rejected because:**

- Does not eliminate GIL contention;
- Does not reduce Python object allocation overhead;
- Adds a runtime dependency that may conflict with user environments;
- Provides smaller gains than a native extension approach.

______________________________________________________________________

## Consequences

### Positive

- **Dramatic throughput improvement:** For large pipelines (>100MB), the Rust
  pathway can achieve 5-10x throughput improvement by eliminating event loop
  overhead.

- **Reduced GIL contention:** Rust operations release the GIL, allowing other
  Python threads and asyncio tasks to proceed during I/O.

- **Zero-copy on Linux:** The `splice()` optimisation avoids copying data into
  userspace entirely for inter-stage transfers.

- **Pure Python fallback:** Installations without native wheels continue to
  work unchanged; the feature is purely additive.

- **First-class both pathways:** Parametrised testing ensures both
  implementations maintain behavioural parity and receive equal attention.

### Negative

- **Build complexity:** Contributors building from source require a Rust
  toolchain (rustc, cargo) in addition to Python.

- **Larger wheel sizes:** Native wheels include compiled Rust code, increasing
  download size compared to pure Python wheels.

- **Two code paths to maintain:** Bug fixes and behavioural changes must be
  verified against both implementations.

- **Platform coverage:** Native wheels must be built for each supported
  platform (Linux x86_64/aarch64, macOS x86_64/arm64, Windows x86_64/arm64).

- **Debugging complexity:** Issues in the Rust pathway require different
  debugging tools and expertise.

### Neutral

- **No API changes:** The public Cuprum API remains unchanged; pathway
  selection is transparent to users.

- **Optional dependency:** The Rust extension is not required; pure Python
  installations work identically to current behaviour.

______________________________________________________________________

## Testing strategy

### Both pathways as first-class

All stream-related tests shall be parametrised to run against both backends:

```python
@pytest.fixture(params=["python", "rust"])
def stream_backend(request, monkeypatch):
    if request.param == "rust" and not rust_available():
        pytest.skip("Rust extension not available")
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", request.param)
    yield request.param
```

### Behavioural parity verification

The following properties must hold for both pathways:

| Property                         | Test approach                           |
| -------------------------------- | --------------------------------------- |
| Byte-for-byte output equivalence | Property test with random payloads      |
| UTF-8 decoding with replacement  | Edge case: invalid UTF-8 sequences      |
| Broken pipe handling             | Unit test: downstream closes mid-stream |
| Backpressure propagation         | Integration test: slow consumer         |
| Empty stream handling            | Unit test: zero-byte transfer           |
| Large transfer (>4GB)            | Stress test: 64-bit size handling       |

### Benchmarking CI

Continuous Integration (CI) includes benchmark jobs that:

- run on every pull request and main branch push;
- compare Python and Rust pathway throughput;
- fail if Rust pathway regresses beyond 10% threshold;
- generate comparison reports in GitHub Actions summary.

Benchmark scenarios cover:

- small (1KB), medium (1MB), large (100MB) payloads;
- single-stage and multi-stage pipelines;
- with and without line callbacks.

______________________________________________________________________

## References

- Design specification: `docs/cuprum-design.md` Section 13
- Implementation roadmap: `docs/roadmap.md` Phase 4
- Current stream implementation: `cuprum/_streams.py`
- PyO3 documentation: <https://pyo3.rs/>
- Maturin documentation: <https://www.maturin.rs/>
