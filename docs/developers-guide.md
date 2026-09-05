# Developers' guide

This guide is for maintainers. It captures the operational scope for build,
test, lint, release, debugging, and extension workflows and acts as the source
of truth for day-to-day contributor expectations. For the system design, see the
[design document](cuprum-design.md); for where code lives, see the
[repository layout](repository-layout.md). Accepted architectural decisions are:

- [ADR-002: Additional Rust components](adr-002-additional-rust-components.md)
- [ADR-003: Two-tier Python linting](adr-003-two-tier-python-linting.md)
- [ADR-004: Interrogate docstring-coverage gate](adr-004-interrogate-docstring-gate.md)
- [ADR-005: Unify Rust availability probe](adr-005-unified-rust-availability-probe.md)
- [ADR-006: Split cuprum/context.py into a context package](adr-006-context-package-split.md)
- [ADR-007: Subprocess execution module boundaries](adr-007-subprocess-execution-module-boundaries.md)
- [ADR-009: Enforce Oxford spelling in source](adr-009-enforce-oxford-spelling-in-source.md)

## GitHub Actions runners

Repository-owned Linux build and test jobs run on Ubicloud managed runners.
Everything else runs on GitHub-hosted runners. Ubicloud offers Linux only, and
a job that sleeps, calls an API, or publishes an artefact somebody else built
gains nothing from a metered build slot.

| Job                    | Workflow                 | Runner                |
| ---------------------- | ------------------------ | --------------------- |
| `typecheck-test`       | `ci.yml`                 | `ubicloud-standard-2` |
| `extension-tests`      | `ci.yml`                 | `ubicloud-standard-2` |
| `coverage`             | `ci.yml`                 | `ubicloud-standard-2` |
| `benchmark-ratchet`    | `ci.yml`                 | `ubicloud-standard-2` |
| `build-pure-wheel`     | `build-wheels.yml`       | `ubicloud-standard-2` |
| `verify-wheel-install` | `build-wheels.yml`       | `ubicloud-standard-2` |
| `coverage-upload`      | `coverage-main.yml`      | `ubicloud-standard-2` |
| `lint-test`            | `ci.yml`                 | `ubuntu-latest`       |
| `changes`              | `ci.yml`                 | `ubuntu-latest`       |
| `refresh-sha`          | `get-codescene-sha.yml`  | `ubuntu-latest`       |
| `publish`              | `release.yml`            | `ubuntu-latest`       |
| `delay_and_comment`    | `delayed-pr-comment.yml` | `ubuntu-latest`       |
| `build-native-wheels`  | `build-wheels.yml`       | `${{ matrix.os }}`    |

`ubicloud-standard-2` (2 vCPU, 8 GB, Ubuntu 24.04 amd64) is the default shape
and the only self-hosted label registered in `.github/actionlint.yaml`.
Escalating a job to `ubicloud-standard-4` requires recorded evidence from at
least three warm runs: peak memory above roughly 6 GB, or the larger shape
reducing billed minutes, or removing the job from the critical path. Because
Ubicloud bills per runner-minute at a rate proportional to vCPU count, a
perfectly parallel job costs the same on either shape and a partly serial job
costs more on the larger one.

`benchmark-ratchet` moved down from `ubicloud-standard-4`. It compares each
scenario's within-run `rust_mean / python_mean` ratio between the baseline and
the candidate, so runner speed cancels out of the comparison and the job needs
neither a fixed nor a larger shape.

`lint-test` stays on `ubuntu-latest`. It installs Whitaker and keeps its own
npm and Whitaker archives on GitHub's cache service, which is a different store
from Ubicloud's. It owns its Cargo registry and compiler caches on that side
exactly as the Ubicloud jobs own theirs.

The native-wheel matrix keeps its platform runners. Ubicloud has no Windows or
macOS capacity, and its Linux legs build inside manylinux containers and under
QEMU emulation.

Every Ubicloud job declares `timeout-minutes` so a wedged runner cannot bill
for GitHub's six-hour default.

### Cache ownership

Caching goes through `actions/cache` pinned to
`55cc8345863c7cc4c66a329aec7e433d2d1c52a9` (v6.1.0), split into its `restore`
and `save` sub-actions. Ubicloud's transparent cache intercepts that version,
so a Linux archive written on an Ubicloud runner lands in Ubicloud's store
rather than GitHub's; v4.3.0 left nothing there. Verified against the Ubicloud
console listings on 2026-09-03. The deprecated `ubicloud/cache` fork is
therefore unnecessary, and one cache action serves both lanes.

Every key is rendered once by `.github/actions/cache-keys`, which exports the
key and its restore-key prefix as environment values. A restore and its save
cannot disagree, and the rendered key is printed into the run summary so any
miss can be explained from the run alone.

| Key family | Paths                                                                                        | Key inputs                                                                                                                          | Writer                                                       |
| ---------- | -------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `cargo-`   | `~/.cargo/registry`, `~/.cargo/git`                                                          | generation, OS, arch, runner environment, hash of `rust/Cargo.lock` and `rust/rust-toolchain.toml`                                  | `extension-tests`, and `lint-test` on the GitHub-hosted lane |
| `tool-`    | `~/.cargo/bin`, `~/.local/bin`, `~/.cache/uv`, `~/.local/share/uv`, `.uv-cache`, `.uv-tools` | the above plus Ubuntu release, Python version, nextest pin, hash of `uv.lock`, `pyproject.toml`, `Makefile`, and the sccache action | `typecheck-test`                                             |
| `sccache-` | `~/.cache/sccache`                                                                           | generation, OS, arch, runner environment, Ubuntu release, run identifier                                                            | `typecheck-test`, and `lint-test` on the GitHub-hosted lane  |

Four rules hold that table together, each with a contract test:

- **One owner per path.** No two cache steps in a job list the same path.
- **One writer per key per lane.** Saves are guarded by
  `github.event_name == 'push' && github.ref == 'refs/heads/main'`. Pull
  requests restore and never save, so they cannot race for a key they are not
  trusted to publish. Every key carries `runner.environment`, so the two lanes
  render different values and a key named in both has one writer on each side.
- **Restore before work.** Every restore precedes the first install, build, or
  test step in its job.
- **No `target` archive.** See below.

`coverage-upload` runs on the same push to `main` as `typecheck-test` and
restores the tool archive without saving it, which is why `coverage-main.yml`
repeats ci.yml's `NEXTEST_VERSION`, `CACHE_GENERATION`, and `UBUNTU_RELEASE`; a
contract test pins the equality.

Compiled tools carry the runner environment and the Ubuntu release in their
key. A binary built against Ubuntu 24.04's glibc 2.39 fails on the 22.04 image.

`build-pure-wheel` and `verify-wheel-install` cache nothing on purpose. The
first runs `uv build` and then checks the repository out again inside its
composite action, which would delete any workspace-scoped restore before it was
read; caching the uv store there would also give `~/.cache/uv` a second writer.
The second installs published wheels into a throwaway virtual environment.

Installed tools use the parent `~/.local/bin` cache path. Do not cache the
terminal `~/.local/bin/sccache` file: a restore creates an empty directory at a
missing mount point, and the installer then cannot replace it with an
executable.

Jobs that install dependencies through Make also cache the workspace's
`.uv-cache` and `.uv-tools` directories. The `Makefile` pins `UV_CACHE_DIR` and
`UV_TOOL_DIR` to that worktree-local pair, so uv's standard `~/.cache/uv` and
`~/.local/share/uv` directories stay empty for those commands. Both are cached
anyway, because the shared coverage action and `uv build` do use uv's defaults.

### `target` is deliberately never cached

No job in this repository archives `target`, `rust/target`, or
`target/${BUILD_PROFILE}`. That is a deliberate estate-wide rule, not an
oversight, and a contract test enforces it.

sccache is the single owner of compiler output for every build shape. The
repository produces three: the objects the lint gate builds through its
alternative code generator and linker, the ordinary debug objects the test
gates build, and the `-C instrument-coverage` objects the coverage gate builds.
sccache hashes the compiler flags into its cache key, so all three coexist in
one store without colliding; run 33677926269 recorded zero non-cacheable
compilations with the cranelift-built Whitaker lints, and Whitaker's
instrumented coverage build reports the same.

A `target` tree, by contrast, is invalidated by any source change, so its
archive is rewritten far more often than the registry it would sit beside and
carries stale intermediates the next run discards. Two shapes would also need
two trees, and a tree built under one set of flags poisons a run using another.

Concretely, this means:

- both shared actions are pinned to a revision that archives no `target` tree
  of its own, so the rule holds even if a caller ever switches back to
  `cache-provider: github`; they still run with `cache-provider: external` in
  every job, which leaves the registry archive to the caller as well;
- the native and manylinux wheel builds cache pip and the Cargo registry but
  not `rust/target`.

The wheel legs are a **documented exception** to the rule that every expensive
build has a compiler cache. sccache does not reach inside the manylinux
container that `maturin-action` builds in, and the Windows and macOS legs run
on GitHub-hosted runners with their own cache service, so dropping
`rust/target` leaves those five legs with no compiler cache at all. That is
accepted rather than fixed: their cold duration in run 33752095108 was 47 to
110 seconds each, which bounds the cost, and re-introducing a `target` archive
to save part of it would reintroduce the ownership problem the rule exists to
prevent. Revisit only if a wheel leg becomes a critical-path gate.

### The compiler cache

Cuprum runs the local-directory backend as a deliberate A/B against Whitaker,
rstest-bdd, and Netsuke, which use the GitHub Actions backend with the
credential re-export; the warm-run comparison of hit rate, restore and save
seconds, and write errors decides the estate default.

`.github/actions/setup-sccache` pins the release version, the archive SHA-256,
and the SHA-256 of the executable inside it. A restored `~/.local/bin/sccache`
is reused only when its digest equals that pin; anything else is replaced from
the verified archive. The tool archive is writable by any job that saves it and
the binary becomes `RUSTC_WRAPPER`, so being executable is not evidence of
provenance. Bumping the version or digest inputs therefore also replaces a
stale cached binary, and changes the tool cache key.

The action points sccache at `~/.cache/sccache` with a 4 GB ceiling, sized to
hold two build shapes. The GitHub Actions backend is deliberately not used:
Ubicloud console listings on 2026-09-03 showed sccache's GHA traffic landing in
GitHub's cache rather than Ubicloud's, where it competes with the Windows and
macOS lanes for GitHub's per-repository quota. A directory the caller caches
goes to the same store as every other archive and appears in the same listing.

The `sccache-` key names the run rather than the content it holds. A compiler
cache depends on the source that was compiled, which no lockfile hash captures,
so a content-addressed key would hit forever and absorb nothing new. Each
writing run publishes a fresh entry seeded from the newest entry its prefix
matches, and the save therefore carries no cache-hit guard.

The key also names the interpreter and the build shape, so there is one family
per compile shape and one writer for each:
[CI cache ownership](ci-cache-ownership.md) lists them and records the
measurement that forced the split. In outline, `extension-tests` writes the
3.13 unoptimized family, each `typecheck-test` leg that runs a suite writes its
own interpreter's, `benchmark-ratchet` writes the 3.13 release family,
`coverage-upload` writes the instrumented one, and `lint-test` writes the
GitHub-hosted lint family.

The writer has to be a job that actually compiles, or the rolling generation
freezes: it would restore the previous entry and republish it unchanged
forever, never absorbing a new object while still reporting hits. That is not
hypothetical. The writer was briefly the 3.13 interpreter leg, which the
deduplication had just reduced to a typechecker.

Every job that compiles Rust installs the wrapper and reports its counters:
`lint-test`, `extension-tests`, `coverage`, `benchmark-ratchet`,
`coverage-upload`, and the `typecheck-test` legs that run the Python suite.

The 3.13 leg is the exception, and its steps are gated on `matrix.python-suite`
for a reason worth keeping: a job that installs sccache and then reports zero
compile requests looks exactly like one whose `RUSTC_WRAPPER` never reached the
compiler, which is a failure this repository has already had. The leg that only
typechecks therefore reports nothing rather than zero. Each zeroes the counters
before its build and writes `sccache --show-stats`, plus the JSON form, into
the step summary afterwards. The probe is not masked with `|| true`: a compiler
cache that cannot report is a broken compiler cache, and a job reporting zero
compile requests is a failed integration rather than a cold cache. Run
33748907011 recorded exactly that for `lint-test`, whose wrapper never reached
the clippy and Whitaker builds; it now uses the same checksum-verified
installer as the rest.

Cuprum's Ubicloud cache listing was empty before this migration, because
`benchmark-ratchet` was its only Ubicloud job. Check the first `main` run's
entries with `ubi gh leynos/cuprum list-cache-entries` to confirm the archives
land in Ubicloud's store rather than GitHub's.

### One execution per suite

Each suite runs once per event. The coverage job is the only place the Rust
suite executes, and no interpreter runs the Python suite both there and in the
matrix. A suite that runs twice costs runner minutes twice and gates nothing
extra.

| Job                                | Python | Rust suite                     | Python suite           | Extension |
| ---------------------------------- | ------ | ------------------------------ | ---------------------- | --------- |
| `coverage` (pull requests)         | 3.13   | **the only run**, instrumented | full collection        | absent    |
| `coverage-upload` (`main`)         | 3.13   | **the only run**, instrumented | full collection        | absent    |
| `typecheck-test` 3.12, 3.14, 3.15a | each   | none                           | `make test-python`     | absent    |
| `typecheck-test` 3.13              | 3.13   | none                           | none, coverage runs it | absent    |
| `extension-tests`                  | 3.13   | none                           | 12 gated modules       | **built** |

The coverage jobs run
`cargo llvm-cov nextest --workspace --all-targets --all-features` under
`RUSTFLAGS=-D warnings`, followed by the doctest pass that `llvm-cov nextest`
skips. Three details are load-bearing:

- **Detection has to be overridden.** Cuprum's workspace lives under `rust/`,
  so the repository root has no `Cargo.toml` and the shared action would
  classify the project as Python-only and skip the Rust suite entirely. The
  jobs therefore pass `language: mixed` and `cargo-manifest: rust/Cargo.toml`.
  This is the single most fragile part of the arrangement: get it wrong and CI
  stays green while running no Rust tests at all, which is why a contract test
  pins both inputs.
- **The flags must match what they replaced.** `all-targets`, `all-features`,
  and `RUSTFLAGS=-D warnings` reproduce the uninstrumented run, so nothing
  drops out of the gate as a side effect of moving it. `doctests` covers what
  `cargo llvm-cov nextest` does not.
- **`main` runs it too.** `coverage-upload` carries the same inputs, not for
  redundancy but because it writes the ratchet baseline that pull-request runs
  compare against. A Python-only baseline would be compared against a
  Python-and-Rust candidate, and the ratchet would measure unlike quantities.

`make test` still runs both suites, which is what a contributor wants locally.
CI calls the halves separately: `make test-python` in the matrix, and the Rust
suite only through the coverage action. `make test-rust` exists for running the
Rust half alone.

Two jobs survive that look like duplicates and are not:

- **`extension-tests`** runs the same interpreter as the coverage job, but with
  the compiled extension present. Coverage runs without it and the gated
  modules skip there; run 33752095108 logged its `rust-backend` cases as
  `SKIPPED`. The two runs execute different code.
- **`typecheck-test` on 3.13** keeps the typechecker and its required check
  name while running neither suite. Dropping its pytest run is only safe
  because the typechecker stands alone: `make typecheck` depends on `build`,
  the dependency sync, and on nothing the test run produces. The other three
  legs keep their Python run because coverage does not run those interpreters.

The nextest installer is gone from this repository. Nothing here runs nextest
directly any more; the coverage action installs its own.

### Concurrency

`ci.yml` declares one constant, `LINUX_RUNNER_VCPUS`, equal to the vCPU count of
`ubicloud-standard-2`. `make test` takes `TEST_JOBS`, `TEST_CARGO_BUILD_JOBS`,
and `PYTEST_CARGO_BUILD_JOBS` from it, and the two jobs that compile outside
`make test` set `CARGO_BUILD_JOBS` from it. Raising the label means raising the
constant in the same change.

pytest stays serial. `PYTEST_WORKERS` defaults to `0` and the coverage action
runs with `pytest-workers: ''`, because the batches compile and reuse the same
Cargo target directory and xdist workers would contend on one build lock rather
than overlap. `-n auto` is rejected outright: it reads the host's core count,
not the two cores the job is billed for.

Codegen selection stays per job. The lint gate builds its Whitaker suite with
cranelift; the coverage gate cannot, because cranelift has no
`-C instrument-coverage`.

### Tool installation

Prebuilt tool installers fail closed, so a missing published binary fails the
job instead of starting a source build that no cache owns. In this repository
that means `cargo binstall` running with `--disable-strategies compile` when
the lint gate installs Whitaker.

cargo-nextest is no longer installed here at all. The coverage job is the only
place it runs, and the shared action installs it from checksummed official
release archives with no source-build fallback of its own. A contract test
rejects a nextest installer reappearing in these workflows, because it would be
an unused download whose failure mode nothing here exercises.

## Rust availability probing

Stream backend availability is resolved through one cached entry point:
`cuprum._backend._check_rust_available()`.

`_check_rust_available()` first checks the testing override
(`set_rust_availability_for_testing`). While active, that override
short-circuits availability resolution before the raw import probe runs, and
`set_rust_availability_for_testing()` clears both
`_check_rust_available.cache_clear()` and `get_stream_backend.cache_clear()`;
otherwise the resolver falls back to that raw import probe. Cached answers only
drift if a long-lived interpreter survives a wheel swap or another out-of-band
import-path or installation-state change.

User callers should use `cuprum.is_rust_available()`, which delegates to
`_check_rust_available()`, so the public answer and dispatch resolver cannot
diverge within a process.

Issue `#128` is resolved in this path: the public helper and backend dispatch
now share the same cached resolver.

## Rust dependency management

When editing `Cargo.toml`, dependencies must use explicit semver-compatible
caret requirements only (for example `"1.2.3"`). Do not use wildcards such as
`*` or open-ended ranges such as `>=` or `~`.

When updating Rust dependencies, keep the requested version aligned to the
patch baseline already present in `Cargo.lock`. This keeps lockfile updates
focused, small, and easy to review.

## Tar and rsync builder helpers

`TarCreateOptions.compression` in `cuprum/builders/tar.py` selects one member
of the `Compression` enum: `NONE`, `GZIP`, `BZIP2`, or `XZ`. This makes the
compression choice mutually exclusive while keeping `TarCreateOptions`
immutable.

The private `_tar_create_argv` and `_tar_extract_argv` helpers in
`cuprum/builders/tar.py`, together with `_rsync_argv` in
`cuprum/builders/rsync.py`, construct immutable argument vectors independently
of `sh.make` wrapping. They exist, so the command construction contract can be
tested directly for issue #71, while the public builders remain responsible for
attaching their curated program.

`_FLAG_ORDER` in `cuprum/builders/rsync.py` defines the fixed rsync flag
emission order: `archive`, `delete`, `dry_run`, `verbose`, then `compress`.
This is a documented contract covered by the property tests in
`cuprum/unittests/test_builder_property_based.py`.

Both `TarCreateOptions` and `RsyncOptions` provide `allow_relative`. It
defaults to `False`, so `safe_path` rejects relative paths unless a caller
explicitly opts in.

### Rejection classifiers for `safe_path` and `git_ref`

`cuprum/builders/args.py` exposes `classify_path_string` and `classify_git_ref`
alongside the `PathRejection` and `GitRefRejection` enums. These are the single
source of truth for *why* an input is rejected: each enum member's value is the
exact `ValueError` message, and `safe_path` / `git_ref` raise directly from the
classifier, so the categories cannot drift from the validators. The intended
reuse policy is:

- These are **developer-facing** helpers, deliberately omitted from the
  module's `__all__` (whose public surface is `safe_path`, `git_ref`, and their
  `SafePath` / `GitRef` return types). They remain importable for in-tree use
  and tests, but are not advertised as end-user API.
- **In-tree callers and tests** may depend on the classifiers to assert on the
  rejection *category* (rather than a brittle message substring) — this is what
  the property tests in `cuprum/unittests/test_args_validators_property.py` do.
- The enum member *names* are the stable contract; enum *values* (messages) may
  be reworded, so match on the member, not the string.
- New validation rules must be added to the classifier (and a matching enum
  member) rather than inline in the validators, keeping the reason taxonomy
  authoritative. Preserve the declared member order — the classifier returns
  the first matching category.

### Stream-backend resolution seam

`cuprum/_backend.py` keeps the same public contract for `get_stream_backend`
(the algorithm documented in the design document's stream-backend section) but
factors its internals into three private helpers so each can be reasoned about
and property tested in isolation:

- `_parse_backend_value(raw)` — pure parsing of a `CUPRUM_STREAM_BACKEND` value
  (whitespace/case normalization, empty → `AUTO`, unknown → `ValueError`),
  taking the raw string so tests inject values without mutating `os.environ`.
- `_probe_rust_availability(requested)` — the impure availability probe,
  encapsulating each mode's failure policy (`PYTHON` never probes; `AUTO`
  tolerates a probe `ImportError`; forced `RUST` propagates it).
- `_resolve_backend(requested, *, rust_available)` — the pure decision core
  that never leaks `AUTO` and raises `ImportError` for forced-`RUST`-unavailable
  (`rust_available` is keyword-only).

`get_stream_backend` composes them inside one boundary `try`/`except`, so a
forced-`RUST` failure always emits the `cuprum.stream_backend_unavailable`
warning whether the probe reports unavailable or itself raises. This is an
internal decomposition for testability; the observable behaviour and precedence
are unchanged.

## Command argument construction

`cuprum.sh.build_argv(*args, **kwargs)` is the public, pure argv-construction
helper behind `sh.make` builders. It delegates to the same internal coercion
path as builders, so tests and project-specific wrappers can verify argument
normalization without catalogue lookup or subprocess execution.

Keep `build_argv` and `sh.make` behaviour aligned:

- positional arguments are stringified with `str()` in the order supplied;
- keyword arguments are serialized after positionals as `--flag=value` entries;
- underscores in keyword names are normalized to hyphens;
- insertion order for keyword flags is preserved;
- `None` raises `TypeError` in positional and keyword positions.

Property coverage for this contract lives in
`cuprum/unittests/test_sh_property_based.py`. Update those properties whenever
argv construction semantics change.

## Program catalogue duplicate diagnostics

`ProgramCatalogue` indexes project settings in two passes: first by project
name, then by program ownership. Keep duplicate diagnostics structured so tests
and configuration tooling can assert on fields instead of parsing messages.

- `DuplicateProjectError` is raised for repeated project names and exposes the
  duplicated name as `project_name`.
- `DuplicateProgramError` is raised when a program is claimed by more than one
  project and exposes the contested `program` plus the existing owner's project
  name as `owner`.

Both exceptions intentionally subclass `ValueError` to preserve compatibility
with callers that already treat catalogue construction failures as invalid
configuration.

## Stream line-splitting properties

Line callbacks in the Python stream backend use two pure helpers from
`cuprum/_streams.py`:

- `_split_complete_lines(text)` splits text into completed lines, strips each
  recognized line ending, and returns `(lines, remainder)`. The `remainder` is
  the final partial line and never ends in `"\n"` or `"\r"`.
- `_strip_line_ending(line)` removes at most one trailing `"\r\n"`, `"\n"`, or
  `"\r"` sequence. It does not normalize or edit interior text.

These helpers are re-exported from `cuprum/_testing.py` so tests can state the
contract directly without driving subprocess I/O. Keep them private to the
package: they exist to make `_emit_completed_lines` small and testable, not as
public user API.

`cuprum/unittests/test_line_splitting.py` contains the direct property suite.
Hypothesis generates text with mixed recognized line endings and checks that
normalized text is preserved, the final remainder is partial, and stripping is
idempotent. CrossHair runs PEP 316 (Python Enhancement Proposal 316) contracts
over bounded symbolic inputs for the same invariants. CrossHair is a
development dependency only; the tests skip the symbolic checks whenever
CrossHair cannot run on the active interpreter. Rather than hard-coding a
Python-version gate, the suite probes CrossHair at import time and degrades to
skipping only for expected availability failures: a missing dev dependency
(`ImportError`) or an interpreter whose opcode set CrossHair cannot yet trace
(`crosshair.tracers.TraceException`, as with the `CALL_KW` gap on early Python
3.15 betas, issue #109). Any other probe exception is allowed to propagate so
that unexpected import failures stay visible. The probe self-resolves once
CrossHair supports the interpreter.

When changing `_emit_completed_lines`, `_split_complete_lines`, or
`_strip_line_ending`, run:

```bash
uv run pytest -q cuprum/unittests/test_line_splitting.py
```

Run `make test` before committing so the stream behaviour and the pure helper
contracts stay aligned.

## `cuprum/context/` package layout

`cuprum/context.py` exceeded the 400-line ceiling and mixed four concerns with
different audiences and change cadences, so it is now a package whose context
surface is re-exported from `cuprum/context/__init__.py`:

- `cuprum/context/env_overlay.py` — pure overlay merging
  (`merge_env_overlays`, `resolve_env`, `_coerce_env_overlay`); no `ContextVar`
  dependency.
- `cuprum/context/core.py` — the domain dataclasses (`CuprumContext`,
  `ScopeConfig`), the `ContextError` package-level root of the domain exception
  hierarchy and its `ForbiddenProgramError` subclass, timeout validation, and
  the before/after hook type aliases.
- `cuprum/context/state.py` — the `ContextVar` plumbing (`current_context`,
  `get_context`, and the internal set/reset helpers).
- `cuprum/context/registration.py` — `scoped`, the `_TokenRegistration`
  base, the registration handles, and the `allow`/`before`/`after`/`env`/
  `observe` factories.

Most context importers are unaffected. `ExecHook` is the exception: its
definition site is `cuprum.events`, it remains available from top-level
`cuprum`, and it is not re-exported by `cuprum.context`. Keep each module under
400 lines; new context features go in the module matching their concern.

## Pipeline execution helper contracts

Pipeline and command execution use small internal helpers whose names expose
their command-query separation contract:

The separate `_enforce_allowlist` and `_collect_hooks` operations supersede the
former combined `_run_before_hooks` helper. Keep authorization as an explicit
command and hook collection as a side-effect-free query; do not recreate the
combined boundary in `_pipeline_types`.

- `_enforce_allowlist(cmd)` is a command.  It reads the active context and
  raises `ForbiddenProgramError` when `cmd.program` is not allowed.  It must
  run before any before-hook dispatch.
- `_collect_hooks(ctx)` is a query.  It copies the hooks already present on
  `ctx` and does not enforce access, dispatch hooks, or mutate the context.
- `_emit_exec_event(hooks, event)` is a dispatcher query over hook return
  values.  It invokes synchronous observe hooks inline, schedules awaitable
  hook results as `asyncio.Task` instances, and returns those tasks to the
  caller.  If a later hook raises, `_ExecEventEmissionError` carries the tasks
  scheduled before the failure so cleanup can still await them.
- `_write_to_stream_writer(writer, chunk)` is a write command with a semantic
  result.  It returns `_WriteOutcome.OPEN` after a successful write/drain and
  `_WriteOutcome.CLOSED` when the downstream pipe closes early.  The caller
  keeps writer ownership and closes it exactly once.

`SafeCmd.run()` enforces the allowlist, then collects hooks from the current
context, emits the `plan` event, runs before-hooks, and delegates subprocess
execution to `_execute_with_hooks`.  Pipeline execution follows the same
ordering per stage: `_build_pipeline_observations` enforces every stage before
collecting hooks, then `_emit_plan_events_and_run_before_hooks` emits and
dispatches.

Observe hooks can return awaitables.  Single-command execution and pipeline
execution both keep a `pending_tasks` list and pass it into every
`_StageObservation`.  For pipelines, all stage observations share that list on
the same asyncio event loop.  Appending a task is synchronous Python bytecode
and the event loop does not pre-empt an `emit()` call halfway through the list
append, so no explicit lock is required.  Do not call `_StageObservation.emit`
from a worker thread; marshal back to the execution loop first.

Private helpers emit diagnostic logs rather than installing a global metrics or
tracing backend.  Hook scheduling and hook failures use the
`cuprum._observability` logger with structured `extra` fields such as
`cuprum_phase`, `cuprum_program`, `cuprum_error_type`, and
`cuprum_scheduled_task_count`.  Stream early-close decisions use debug-level
records on the `cuprum._streams_pump` logger and include
`cuprum_discarded_bytes` when upstream bytes are drained after the downstream
writer has closed. Suppressed writer cleanup failures remain debug-level
diagnostics with `cuprum_operation` and `cuprum_error_type`, because they are
expected during already-closed pipe teardown.  User-facing metrics and spans
remain the responsibility of observe-hook adapters such as `MetricsHook` and
`TracingHook`, which consume `ExecEvent` values without coupling core execution
to a telemetry vendor.

`MetricsHook` keeps that consumption in two halves. The pure
`_metric_operations` reducer maps an `ExecEvent` to a tuple of `_CounterOp` and
`_HistogramOp` records, and `_apply` is the only step that reaches the
`MetricsCollector`. The reducer is therefore the single source of truth for
which counters and observations each phase yields, and it can be property
tested without a collector at all — `test_metrics_adapter_stateful.py` drives
random event streams through it and checks the accumulated totals against an
independent phase-count oracle.

Two consequences are worth preserving when changing the mapping. Labels are
projected only when the reducer yields at least one operation, so a `plan`
event never touches `event.program` or the project tag. And the reducer is
total over `ExecPhase` — every declared phase has an arm, including
`capture_eof_grace_expired`, with `plan`, `stdin`, and `exit` handled directly
and the remaining phases routed through `_PHASE_COUNTERS` — and fail-closed
beyond it: any other phase raises `_UnhandledMetricsPhaseError` rather than
being silently dropped. That is deliberate, and its cost is worth stating
plainly. A hook exception is not swallowed, so adding a value to `ExecPhase`
without adding an arm here would raise for every caller that has already
registered `MetricsHook`. A new phase therefore cannot reach metrics without a
decision in this reducer. The structured logging adapter is fail-open by
contrast, formatting an unrecognized phase generically.

Applying the operations is deliberately non-atomic, and that is a contract
collectors rely on rather than an implementation detail. An `exit` event yields
up to two operations — the failure counter, then the duration observation —
applied as separate collector calls in that order, so a collector that raises
on the second leaves the first applied. The exception propagates out of the
hook and is not swallowed: `_emit_exec_event` logs `observe_hook_failed` and
wraps the error in `_ExecEventEmissionError` so already-scheduled observe tasks
survive cleanup, then `_StageObservation.emit` unwraps it and re-raises the
collector's original exception — so a raising collector fails the user's
command. A collector that must not do that has to swallow its own errors. The
labels are extracted once before the loop and are read-only within it. A
collector must therefore treat each call as independent and never infer a
duration observation from a failure increment. No operation identifier is
passed either, so a repeated call increments again — nothing here is
idempotent, and the hook never retries. See the metrics-hook dispatch figure in
[the design document](cuprum-design.md)
for the full statement, and `test_metrics_adapter_stateful.py` for the case
that pins it.

### Choosing a test shape per observe hook

The three observe-hook adapters are verified differently, and the difference is
driven by whether the hook accumulates state across events rather than by
preference:

Table 1: verification shape for each observe hook, and why

| Hook                      | Shape                                                        | Why                                                                                                                        |
| ------------------------- | ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| `TracingHook`             | `RuleBasedStateMachine` (`test_tracing_span_stateful.py`)    | holds `_active_spans` keyed by `ExecId`; the interesting bugs are correlation and drain failures across interleaved events |
| `MetricsHook`             | `RuleBasedStateMachine` (`test_metrics_adapter_stateful.py`) | accumulates counters and histograms, checked against an independent phase-count oracle                                     |
| `structured_logging_hook` | `@given` properties (`test_logging_adapter_properties.py`)   | holds no state at all: one record per event, no map to drain                                                               |

A state machine over the logging hook would generate interleavings that cannot
distinguish any two implementations, because nothing carries between events.
Its risks are per-event and shape-dependent instead: an `extra` key colliding
with a reserved `LogRecord` attribute — which raises inside the caller's
logging stack, not in cuprum — a phase falling through the level map, and a tag
value that `JsonLoggingFormatter` cannot serialize. `ExecEvent.tags` is typed
`Mapping[str, object]`, so arbitrary values are in contract, which is why
`_json_serializable` and `json.dumps(..., default=str)` both exist and why the
generator must produce values that are not JSON-native.

Only `TracingHook` has an active map, so "the active map drains correctly" is a
claim about that hook alone; `test_tracing_span_stateful.py` asserts it
directly by cross-checking `hook._active_spans` against a model after every
step.

Stream pumping continues to drain the upstream reader after
`_write_to_stream_writer` reports `_WriteOutcome.CLOSED`.  `_pump_stream`
closes the writer in a `finally` block, so writer cleanup runs after success,
exceptions, and cancellation.  Tests cover this contract at four levels:

- direct helper tests in `cuprum/unittests/test_cqrs_helpers.py`
- runtime behaviour and stream-drain properties in
  `cuprum/unittests/test_stream_pump_runtime_behaviour.py`
- observe-task assertions in `cuprum/unittests/test_observe.py`
- CQRS hook and task-scheduling behaviour in
  `cuprum/unittests/test_cqrs_hook_behaviour.py`, covering allowlist
  enforcement, before/after/observe hooks, and async observe-task scheduling

## Timeout and fail-fast reducer verification

The temporal, branch-heavy subprocess-timeout and pipeline-cleanup paths are
backed by two pure reducers, each verified twice: Hypothesis samples them
randomly, and CrossHair confirms the same invariants symbolically over a
bounded state space. Keep both layers when changing either reducer.

`_resolve_timeout_payload` (`cuprum/_subprocess_timeout.py`) — the
timeout-payload seam. The symbolic contracts confirm that:

- a carried `_SubprocessTimeoutError` returns exactly its own timeout, stdout,
  stderr, and exit time, independently of the fallback (the fallback in that
  contract deliberately holds different values and a `None` configured timeout,
  so a resolver that consulted it would return a wrong field or raise);
- a bare `TimeoutError` with a configured timeout present returns exactly the
  fallback's timeout, stdout, stderr, and exit time;
- a bare `TimeoutError` with `configured_timeout is None` raises
  `_SubprocessInvariantError` rather than inventing a timeout.

`_stages_to_terminate` (`cuprum/_process_lifecycle.py`) — the fail-fast
selection. The symbolic contracts confirm that:

- every selected index is in range, unique, and ordered;
- no selected index is the `failure_index`;
- every selected index corresponds to a stage whose `done` flag is `False`;
- the selection is exactly the set of unfinished, non-failed indices;
- cleanup is idempotent — after marking the selected stages done, a second
  invocation selects nothing.

Run the verification with either of:

```bash
uv run pytest -q cuprum/unittests/test_subprocess_timeout_reducers_crosshair.py
uv run crosshair check \
  cuprum/unittests/test_subprocess_timeout_reducers_crosshair.py \
  --analysis_kind=PEP316
```

The ordinary Hypothesis module remains alongside it:

```bash
uv run pytest -q cuprum/unittests/test_subprocess_timeout_reducers.py
```

### Deliberate bounds

The symbolic domains are kept small and finite so CrossHair exhausts them
rather than returning `CANNOT_CONFIRM`:

- pipelines are capped at three stages, with `failure_index` constrained to a
  valid index by precondition;
- the per-stage `done` flags are encoded as a single bounded integer bitmask
  rather than a symbolic list of symbolic booleans, which is what makes the
  space enumerable;
- timeouts and exit times come from a three-value enumeration instead of
  unrestricted floats, and stdout/stderr from a three-value enumeration that
  includes `None`. The reducers only ever copy these values, so representative
  values suffice — what matters is that carried and fallback values stay
  distinguishable, which the enumerations preserve.

These checks run in CI rather than only on demand: the module matches the
`cuprum/unittests/test_*.py` pattern in the Makefile's `PYTEST_TARGETS`, so
`make test` collects and executes it. `check_states` requires
`MessageType.CONFIRMED`, so an available-but-unconfirmed result
(`CANNOT_CONFIRM`) or a refuted postcondition (`POST_FAIL`) fails the run
instead of being downgraded to a skip or a warning. Availability is probed
through the shared helpers in `cuprum/unittests/_crosshair_support.py`, which
degrade to a skip only for a missing CrossHair dependency (`ImportError`) or an
interpreter whose opcode set the tracer cannot handle
(`crosshair.tracers.TraceException`, as with the `CALL_KW` gap on early Python
3.15 betas, issue `#109`); every other failure is re-raised. Supported
interpreters therefore confirm the contracts rather than skipping them.

### Pipeline stream module boundaries

Pipeline byte movement is split so each module has one reason to change:

Table 1: pipeline stream modules and their responsibilities

| Module                        | Owns                                               |
| ----------------------------- | -------------------------------------------------- |
| `_pipeline_streams.py`        | Backend choice and the Python/Rust pump dispatch   |
| `_pipeline_stream_results.py` | Stream-task collection, cancellation, and outcomes |
| `_pipeline_stream_fds.py`     | Raw-descriptor hand-off for the Rust pump          |

### Rust pump raw-descriptor lifecycle

Routing an inter-stage hop through the Rust pump means taking the raw pipe
descriptors back from asyncio for the duration of the transfer.
`cuprum/_pipeline_stream_fds.py` owns that hand-off, keeping its
partial-failure paths in one place rather than inlined in the pump:

- `_extract_stream_fd` pulls the raw descriptor out of an asyncio transport,
  returning `None` when the transport does not expose one.
- `_BlockingModeGuard` is the FD-state object. `engage()` switches both
  descriptors to blocking mode while capturing their prior modes, rolling back
  a partial change if the second switch fails; `restore()` returns them to the
  captured modes. This is what stops a descriptor being left blocking.
- `_paused_reader` is a context manager that pauses the reader transport and
  resumes it on every exit path, including exceptions and cancellation. Only a
  pause that took effect is resumed. It yields whether the descriptor may be
  handed over: a *failed* pause answers `False`, because asyncio may still be
  consuming the reader, and the caller falls back to the Python pump rather
  than racing it. A transport exposing no pause hooks answers `True`, since
  there are no callbacks to suspend. A transport with `pause_reading()` but no
  `resume_reading()` answers `False`: pausing it could not be undone.

Cancellation is handled explicitly. `run_in_executor` cannot interrupt the
worker thread running the Rust pump. The native worker borrows the reader
descriptor and owns only the duplicated writer resource: `_streams_rs`
transfers the duplicate after executor submission, and Rust closes it.
`_run_rust_pump_with_blocking_fds` shields the executor future and re-raises
`CancelledError` after cleanup; its completion callback only restores
descriptor and transport state once the worker settles. Pipeline teardown
remains coupled to that cleanup, so restoring blocking mode or resuming the
transport earlier cannot hand descriptors back to asyncio while native code is
still mid-transfer.

During cancellation, `_await_native_pump_cleanup` emits structured `DEBUG`
records at cleanup start and completion. Both records carry
`cuprum_action=rust_pump_cleanup`, `cuprum_operation=native_pump_cleanup`, and
an outcome of `started` or `completed`; completion also carries the monotonic
`cuprum_duration_s`.

The module's reuse policy is narrow: further descriptor-lifecycle concerns for
this hand-off belong here, but the seams are not a general-purpose descriptor
utility. Anything serving a different caller should be designed against that
caller's real requirements instead of widening these.

#### Observing a declined hand-off

Each of those partial failures ends the same way: the hop falls back to the
Python pump and completes correctly. That is the point — the fall-back is not
an error, and nothing surfaces to the caller. It does mean a deployment that
has quietly stopped taking the fast path is indistinguishable from one that
never had it, which is the question an operator actually asks.

`_log_rust_pump_declined` in `cuprum/_pipeline_streams.py` records each decline
against the `cuprum._pipeline_streams` logger, following the same convention as
the pipeline fail-fast records: a `cuprum_action` of `rust_pump_declined` plus a
`cuprum_reason` naming the seam that refused.

Table 1: `cuprum_reason` values and the seam each one reports

| `cuprum_reason`             | Seam that declined                                                 |
| --------------------------- | ------------------------------------------------------------------ |
| `raw_fd_unavailable`        | `_extract_stream_fd` found no descriptor on at least one transport |
| `reader_pause_failed`       | `pause_reading()` raised, so asyncio may still be consuming        |
| `blocking_mode_unavailable` | `_BlockingModeGuard.engage` could not switch both descriptors      |

These are logged at `DEBUG`, deliberately. A fall-back is a per-hop routing
decision rather than a fault, so promoting it to a warning would make a
correctly-working pipeline noisy on any platform where the fast path does not
apply. To diagnose fast-path coverage, raise that one logger:

```python
import logging

logging.getLogger("cuprum._pipeline_streams").setLevel(logging.DEBUG)
```

`cuprum/unittests/test_pipeline_streams_observability.py` pins each reason to
the real code path that emits it, so a decline that stops being recorded fails
the suite rather than going unnoticed.

A pump failure can also be masked by cancellation. `asyncio.wait` never
retrieves a future's outcome, so when a hop is cancelled
`_report_pump_outcome_after_cancel` consumes the worker's future and records
any error it carried under a `cuprum_action` of
`rust_pump_failed_after_cancel`, with the traceback attached via `exc_info`.
The caller is told about the cancellation it asked for; without this record the
pump's own error would resurface when the future is garbage-collected as an
unretrieved-exception warning, detached from the hop that caused it.
`cuprum/unittests/test_pipeline_streams_cancellation.py` pins the field.

#### Counting those records

`PumpEvent` carries these routing decisions on a dedicated observation channel,
separate from `ExecEvent`. This avoids adding a new `ExecPhase`, which would
break already-registered `MetricsHook` instances that match that closed set.
`observe_pump` registers synchronous hooks in a `ContextVar`, and
`PumpMetricsHook` maps the events to bounded counters and a cleanup-duration
histogram:

Table 1: metrics emitted by `PumpMetricsHook`

| Metric                                       | Labels    |
| -------------------------------------------- | --------- |
| `cuprum_rust_pump_declined_total`            | `reason`  |
| `cuprum_rust_pump_failed_after_cancel_total` | none      |
| `cuprum_rust_pump_cleanup_total`             | none      |
| `cuprum_rust_pump_cleanup_duration_seconds`  | none      |
| `cuprum_rust_pump_handoff_total`             | `outcome` |

`RustPumpDeclineReason` bounds the decline label to its four declared values.
The `outcome` label is also closed: it is exactly `submitted`,
`blocking_setup_failed`, `executor_submission_rejected`, `native_load_failed`,
`buffer_validation_failed`, `platform_writer_transfer_failed`,
`native_io_failed`, `duplicate_writer_failed`, or `reader_preparation_failed`.
The hand-off counter increments once for each such outcome, including a
successful submission. `outcome` is the only label on the hand-off counter.
Descriptor numbers, Windows handle values, errno values, exception types,
exception messages, and tracebacks are never metric labels. Observer failures
are logged and do not alter the successful fallback or the caller's
cancellation. [ADR-008](adr-008-rust-pump-observation-channel.md) records the
decision.

### `_pipeline_wait` completion command/query seam

`cuprum/_pipeline_wait.py` splits completion handling on the same command-query
line, so the fail-fast ordering rules can be verified without processes or a
clock.

- `_PipelineWaitState.record_completion(completed_idx, exit_code, *, ended_at)`
  is a **command**. It writes the completed stage's exit code and the injected
  completion time into that stage's slots, and latches `failure_index` only for
  the *first* non-zero exit in completion order — completion order, not stage
  order, so a stage failing earlier in time wins over a lower-indexed stage
  failing later. It stays deterministic because the caller supplies `ended_at`
  rather than the method reading the clock.
- `_PipelineWaitState.should_terminate_others(completed_idx)` is a
  **side-effect-free query**. It returns `True` only for the latched first
  failure, and `False` for a final-stage failure because no other pipeline
  stage needs stopping. It reads state without changing it, so it is safe to
  call repeatedly and in any order after the command has run.
- `_process_completed_task(...)` owns the runtime concerns: it reads the clock,
  invokes the command, emits the structured records described below, and acts
  on the query by awaiting `_terminate_pipeline_remaining_stages`. Keep logging
  and I/O here — moving either into the command or the query would break the
  determinism the symbolic verification depends on.

Completion order governs — except in the one case where there is no completion
order left to observe. `asyncio.wait` hands back the settled tasks as an
*unordered set*, so stages that land in the same batch are indistinguishable in
time. `_wait_for_pipeline` therefore feeds each batch through
`_process_completed_task` in ascending stage-index order:

```python
for wait_task in sorted(done, key=lambda task: state.task_to_index[task]):
```

That sort is a tie-break, not a priority. Across batches the stage that
completed first still latches `failure_index`, exactly as `record_completion`
describes; the sort only orders the stages *within* a single `asyncio.wait`
batch, where the alternative is set iteration order and a `failure_index` that
varies between runs of the same pipeline. Ascending stage index is the tie
worth breaking towards because in a pipeline the upstream stage is the one
whose failure causes the downstream failures it triggers, so the earliest stage
names the cause rather than a symptom.

Two consequences follow for anyone changing this seam. Removing the sort does
not change which stage is *usually* reported, so a test that fails stages at
distinct times will not catch its loss; only one that forces stages into a
single batch will, which is what `TestSimultaneousCompletions` in
`cuprum/unittests/test_pipeline_wait_async.py` exists to do. And the tie-break
lives at the async boundary rather than in the pure transition, which is why
`record_completion` is specified purely in terms of the order it is called in —
the Hypothesis and CrossHair layers drive that transition directly and never
see a batch.

When that fail-fast path fires it is no longer silent. Three structured records
are emitted through `logging.getLogger("cuprum._pipeline_wait")`, distinguished
by a stable `cuprum_action` field and sharing `cuprum_stage_index`,
`cuprum_stage_count`, `cuprum_exit_code`, `cuprum_duration_s` (elapsed from the
stage's recorded start to the injected completion time), and `cuprum_exec_id`:

- `pipeline_stage_first_failure` — emitted once, when a completion newly
  latches `failure_index`.
- `pipeline_fail_fast_termination` — emitted immediately before termination is
  awaited.
- `pipeline_fail_fast_terminated` — emitted once termination returns, adding
  `cuprum_terminated_stage_count` and `cuprum_termination_duration_s`.

No record is emitted for a successful exit, for a later failure once
`failure_index` is latched, or — for the two termination records — for a
failure with no other stage left to stop. That last case covers more than the
final-stage and single-stage failures the ordering query rules out: a batch is
processed in stage order, so an upstream failure can be reached after every
sibling has already exited. `_process_completed_task` therefore asks
`_has_stages_to_terminate`, which wraps the same `_stages_to_terminate` reducer
the teardown selects its targets with, before publishing anything; the
`pipeline_fail_fast` event is withheld on the same condition.
`pipeline_stage_first_failure` is not, because the failure latched regardless.

Their shape lives in `cuprum/_pipeline_wait_records.py`, separately from the
decision that fires them, because the payload is a published contract that the
users' guide documents while the ordering decision is a verified transition.
That module hard-codes the logger name rather than using `__name__` for the
same reason: operators are told to attach handlers to `cuprum._pipeline_wait`,
so the name is part of the contract and must not follow the module the code
happens to live in.

Two of those fields exist to make a record joinable rather than merely
descriptive:

- `cuprum_exec_id` is the stage's `_StageObservation.exec_id`, the same
  per-execution token the observe hooks publish on that stage's span and its
  `start` and `exit` events. It reaches the wait path through
  `_PipelineSpawnResult.stages.observations`, the immutable `_StageWaitContext`
  snapshot passed to `_PipelineWaitState.from_processes` alongside
  `started_at`. It defaults to an empty tuple, and `_PipelineWaitState.exec_id`
  then reports `None` because the pure transition never reads it and the
  symbolic model must not carry it.
- `cuprum_terminated_stage_count` is what
  `_terminate_pipeline_remaining_stages` returns. Reporting it from the helper
  that does the terminating, rather than recomputing the selection at the call
  site, is what keeps the count honest when the selection changes.

The fail-fast decision is also projected through the opt-in adapters:
`MetricsHook` increments `cuprum_pipeline_fail_fast_total` with only the
`program` and `project` labels, while `TracingHook` records a
`cuprum.pipeline_fail_fast` event on the failing stage's span. The event and
the three structured records above share the stage's `cuprum_exec_id`; no
captured output or unbounded identifiers are used as metric labels.

Three verification layers cover this seam; keep all three when changing it:

```bash
# The state machine, boundary cases, async boundary, batch ordering,
# observability, fail-fast event, and end-to-end wiring are split into eight
# focused modules sharing `cuprum/unittests/_pipeline_wait_support.py` scaffolding.
uv run pytest -q \
  cuprum/unittests/test_pipeline_wait.py \
  cuprum/unittests/test_pipeline_wait_state_machine.py \
  cuprum/unittests/test_pipeline_wait_examples.py \
  cuprum/unittests/test_pipeline_wait_async.py \
  cuprum/unittests/test_pipeline_wait_batch.py \
  cuprum/unittests/test_pipeline_wait_observability.py \
  cuprum/unittests/test_pipeline_wait_fail_fast_event.py \
  cuprum/unittests/test_pipeline_fail_fast_wiring.py
# CrossHair PEP 316 contracts over the bounded symbolic model.
uv run pytest -q cuprum/unittests/test_pipeline_wait_crosshair.py -m crosshair
uv run crosshair check \
  cuprum/unittests/test_pipeline_wait_crosshair.py \
  --analysis_kind=PEP316
```

The symbolic model in `cuprum/unittests/test_pipeline_wait_crosshair.py` is
bounded on purpose: preconditions cap the pipeline at three stages, exit codes
at `-2..2`, and timestamps at `0.0..4.0`, and the state is built directly with
only the fields the pure transition reads, so no asyncio task, subprocess, or
clock enters the symbolic space. The contracts confirm that a completion writes
only its own slot, that the first non-zero completion latches `failure_index`
and a later one does not replace it, that `should_terminate_others` is true
exactly for a non-final first failure (covering the final-stage and
single-stage cases), and that repeating the query changes nothing.

That module probes CrossHair at import time through the same shared helpers
described above, so it degrades to a skip only for a missing dependency or an
unsupported tracer, and confirms its contracts on every supported interpreter.

## Canonical stream-drain loop

`cuprum._streams._drain(stream, config, *, on_chunk=None)` is the single
read/echo/buffer loop behind both consume variants. It reads in `_READ_SIZE`
chunks, extends the capture buffer when capturing, echoes each chunk to the
configured sink when echoing, and hands the chunk to the optional `on_chunk`
callback for variant-specific processing:

- `_consume_stream_without_lines` calls `_drain` with no callback.
- `_consume_stream_with_lines` supplies an `on_chunk` callback that feeds the
  incremental decoder and emits completed lines, then flushes the decoder tail
  after the drain returns.

Re-use policy: the public entry point remains `_consume_stream`, which
dispatches between the two variants on whether an `on_line` callback was
supplied. Any fix to the read/echo/capture mechanics belongs in `_drain` so the
capture path and the line-emitting path cannot silently diverge; new consume
variants must layer behaviour through `on_chunk` rather than copying the loop.

When echoing, `_drain` writes raw bytes to sinks with a `.buffer`. For
text-only sinks, it owns an incremental decoder configured with
`config.encoding` and `config.errors`, then flushes that decoder at end of
stream. This preserves multibyte characters that span read chunks.

Each `_drain` call builds one frozen `_DrainState` carrying a mutable
`_EchoGuard` payload, so concurrent stdout and stderr drains disable echoing
independently. Every echo write, including the final decoder flush through
`_flush_echo_decoder`, routes via `_echo_chunk`. That helper catches
`UnicodeEncodeError` only: the first failure disables echo for the rest of that
drain, logs one `WARNING` on the `cuprum.stream` logger with structured
`cuprum_*` extras, and lets every other error propagate unchanged. Capture
(`buffer.extend`) always runs before the echo step, so a rejected echo write
never loses captured bytes, and the binary `.buffer` fast path inside
`_write_chunk` is unchanged.

The first failure is owned entirely by that one `_echo_chunk` transition: the
`cuprum.stream` `WARNING` and the opt-in `cuprum.echo_observation.observe_echo`
event are two projections of the same guard flip, emitted once per affected
drain and never repeated by a later chunk or the final decoder flush. Because
`ExecPhase` is a closed set that registered consumers match exhaustively, the
echo channel carries its own `cuprum.echo_events.EchoEvent` type on its own
hook registry rather than a new phase, so consumers opt in by registering and
unregistered callers pay nothing. Hook failures are reported and skipped,
mirroring `cuprum.pump_observation`, so a broken metrics backend cannot change
what a run captures.

`cuprum/unittests/test_stream_property_based.py` and
`tests/behaviour/test_stream_property_preservation_behaviour.py` hold the
public-boundary property coverage: Hypothesis generates byte payloads split at
arbitrary boundaries and asserts that real subprocess pipelines preserve
payloads, keep final stdout and stderr captures independent, and echo all
stdout and stderr text when both streams share one sink.
`cuprum/unittests/test_stream_drain.py` keeps focused direct coverage for the
canonical helper contract and the two `_consume_stream` variants.

### Concurrency model

Each `_drain()` invocation is self-contained. It owns its capture buffer
(`bytearray`), the line-emitting variant's `codecs.IncrementalDecoder`, and any
text-only echo decoder. The `on_chunk` callback closes over the line decoder
and acts only as the chunk delivery hook. Concurrent stdout and stderr drains
therefore do not share mutable capture or decoder state.

The echo sink (`config.sink`) may be shared between concurrent drains when
stdout and stderr are both echoed. Writes to that sink interleave only at
`await` points: once a drain starts writing a single decoded chunk, there is no
intermediate `await` before that chunk write finishes.

Cancellation is fail-fast. If an `asyncio.Task` wrapping `_drain()` is
cancelled, `asyncio.CancelledError` propagates from `stream.read()`. When
`config.capture_output` is true and `config.discard_on_cancel` is unset, the
bytes captured so far are decoded and returned; when the discard event is set
(or output is not being captured), those bytes are discarded and cancellation
propagates.

Callers must not share one `asyncio.StreamReader` between two `_drain()`
invocations. Each invocation must receive its own reader.

### Canonical adapter event projection and locked-store base

`cuprum/adapters/_support.py` keeps the three telemetry adapters from repeating
the same event projection and in-memory collector locking. It owns the
canonical logging/tracing `(key, value)` projection helper for optional
execution fields, the `project` tag helper, the shared unhandled-phase debug
log, and `_LockedStore` with its lock-guarded `reset()`. Each adapter retains
backend-specific key names and value shaping, such as tuple versus list `argv`,
at its call site.

This module was extracted to prevent three-way projection drift and keep each
adapter within its cohesion budget. It is importable only by adapter modules:
do not add backend rendering, logging configuration, event construction, or
general-purpose utilities. Production imports stay adapter-only; contract tests
such as `cuprum/unittests/test_adapter_projection.py` may import
`_event_common_fields` to pin the projection contract. Add an adapter-visible
`ExecEvent` field once to `_event_common_fields`; new in-memory collectors
derive from `_LockedStore` and implement `_clear()` while its lock is held.

`cuprum/unittests/test_adapter_projection.py` pins this contract with
Hypothesis properties and redacted per-phase syrupy snapshots.

### Build and test worker controls

`make test` runs pytest serially by default. Set `PYTEST_WORKERS` to a positive
worker count to enable xdist explicitly. Set `BUILD_JOBS=-jN` to pass the same
count to Rust test commands and, through `CARGO_JOB_ENV`, to both
`RAYON_NUM_THREADS` and `CARGO_BUILD_JOBS`.

## Tracing adapter span lifecycle

`cuprum/adapters/tracing_adapter.py` provides `TracingHook`, an observe hook
that turns the `ExecEvent` stream into OpenTelemetry-style spans. It depends
only on the `Tracer` and `Span` protocols from
`cuprum.adapters.tracing_protocols`, so any backend that implements them can be
plugged in. `tracing_adapter` re-exports `Span` and `Tracer` as its public
integration boundary. The legacy `cuprum.adapters._tracing_protocols` module is
a compatibility re-export only and does not define a second protocol contract.
`cuprum/adapters/tracing_memory.py` supplies `InMemoryTracer` and
`InMemorySpan`, the reference doubles used by tests and examples:
`InMemoryTracer` collects spans in memory and protects its span store through
the shared `_LockedStore` lock (its mutators, and `reset()`, run under that
lock), while `InMemorySpan` is a plain mutable record that provides no
synchronization of its own.

**Phase dispatch.** `TracingHook.__call__` matches every `ExecEvent.phase` in a
single `match`, and each phase falls into exactly one of four categories: span
lifecycle (`start` opens a span, `exit` ends it), span event (`stdout`,
`stderr`, `stdin_error`, `timeout`, `teardown_error`, and
`capture_eof_grace_expired`, and `pipeline_fail_fast` record a `cuprum.<phase>`
event on the already-open span), deliberately ignored (`plan` and `stdin` carry
no tracing semantics), or unhandled (the `case _` logs via
`_log_unhandled_phase` instead of failing silently or raising). A new phase
should be slotted into this policy rather than given an ad-hoc side path.

**State model.** `TracingHook` keeps `_active_spans`, a dictionary keyed by
`ExecEvent.exec_id` (the per-execution correlation token), guarded by an
internal `threading.Lock`:

- **start** builds the span outside the lock, then, under the lock, swaps the
  mapping atomically — capturing any span already registered for that `exec_id`
  (a duplicated or reused token) and installing the replacement in one critical
  section. The detached stale span is then marked failed and ended *outside*
  the lock, so an arbitrary `Span` that blocks on I/O in `set_status()`/`end()`
  cannot stall other executions' handlers; each replaced span is still ended
  exactly once because it is already unreachable via the map.
- **stdout/stderr/stdin_error/timeout/teardown_error/capture_eof_grace_expired**
  all route through the single `_record_span_event` helper: it looks up the
  span for the event's `exec_id` under the lock — moving it to the
  most-recently-active end of the registry as it does so, see "Bounded span
  registry" below — then, outside the lock, copies whichever of the `line`,
  `operation`, `error_type`, `note`, `timeout_s`, `timeout_mode`,
  `eof_grace_s`, and `pending_readers` fields are set on the event onto a
  `cuprum.<phase>` span event (for example `cuprum.stdout`, `cuprum.timeout`, or
  `cuprum.teardown_error`). The grace-expiry event carries only `eof_grace_s`
  and `pending_readers` in addition to the common correlation fields. New
  event-recording phases should extend this shared field set rather than add a
  bespoke per-phase method. The helper never sets the span status or ends the
  span — only `exit` does that — so `stdin_error` (the child process may
  legitimately ignore its stdin), `timeout`, `teardown_error`, and
  `capture_eof_grace_expired` are all recorded as diagnostics without failing
  or closing the execution span. `stdout`/`stderr` recording is gated by the
  hook's `record_output` flag; ancillary diagnostics are recorded
  unconditionally, so a stdin-write failure, a timeout, a teardown failure, or
  an EOF-grace expiry stays diagnosable even when line-by-line output recording
  is switched off. The grace-expiry event carries no captured stdout or stderr
  payload. Because `teardown_error` can be an execution's last event — cleanup
  also runs on external cancellation and on a stdin-writer failure, and on
  those paths the original exception propagates with no `exit` — a span opened
  by `start` can otherwise be left open indefinitely; that is what the bounded
  registry below exists to contain.
- **exit** removes (pops) the span for the event's `exec_id` under the lock,
  then sets the exit attributes and status and ends the span outside the lock.

Keying on `exec_id` rather than PID is what stops a recycled PID, or delayed
output/exit from an earlier execution, from attaching to a later execution's
span. `pid` is retained only as the `cuprum.pid` span attribute for
observability.

**Bounded span registry.** `_active_spans` is capped at `_MAX_ACTIVE_SPANS`
(1024) because not every execution reaches `exit`: as noted above, cleanup also
runs on external cancellation and on a stdin-writer failure, and on those paths
the original exception propagates with no `exit`, so a `teardown_error` can be
an execution's last event. Without a cap, those entries would accumulate for
the lifetime of the hook. `_active_spans` is a `collections.OrderedDict`, and
`_record_span_event` calls `move_to_end` whenever one of a span's events lands,
so ordering reflects recency of *activity*, not of arrival — a long-running
execution that is still producing output is not evicted ahead of one that fell
silent. This is a heuristic, not a guarantee: a live but silent execution can
still be evicted. When the cap is exceeded, `_evict_overflow_locked` detaches
the overflow from the front of the registry with `popitem(last=False)`; each
evicted span is then marked failed (`set_status(ok=False)`) and ended *outside*
the lifecycle lock, for the same reason the stale-span replacement in `start`
is — an arbitrary `Span` may block on I/O in those calls, and holding the lock
across them would serialize every other execution's handler.

Every eviction batch is reported once via `_log_span_eviction`
(`cuprum/adapters/_support.py`), which logs a `WARNING` on the
`cuprum.adapters` logger with message `span_registry_overflow` and structured
extras `cuprum_adapter`, `cuprum_spans_evicted`, and `cuprum_spans_active`.
Only counts are recorded — no span attributes (which carry command payloads)
and no execution tokens (which are unbounded in cardinality). `WARNING` rather
than `DEBUG` because an evicted span is ended as failed while its execution may
still be running: the trace it would have carried is lost, and without a signal
that loss is undiagnosable.

**Legacy or manual events.** An event whose `exec_id` is `None` (a legacy or
hand-constructed event) cannot be correlated, so it is ignored rather than
guessed from PID: a `start` without an `exec_id` creates no span, and `stdout`/
`stderr`/`stdin_error`/`timeout`/`teardown_error`/ `capture_eof_grace_expired`/
`pipeline_fail_fast`/`exit` without one are dropped. Every event Cuprum itself
emits carries an `exec_id`, so this only affects hand-built event streams.

## Canonical `_TokenRegistration` handle base

All `ContextVar`-backed scope-registration handles — `AllowRegistration`,
`HookRegistration`, and `EnvRegistration` in `cuprum/context/registration.py` —
derive from one canonical `_TokenRegistration` base. The base owns the `_token`/
`_detached` pair, the idempotent `detach()`, the context-manager protocol, and
the `_install(new_ctx)` step that sets the derived context and captures the
restoration token. Subclasses implement only the context-derivation step in
`__init__`. The consolidated "Token-based Restoration" docstring lives on the
base.

Re-use policy: any new scope-registration handle must derive from
`_TokenRegistration` and confine itself to deriving the new context; the
restoration protocol is subtle (`ContextVar` token discipline), so a divergent
copy is a latent correctness hazard. Note that `LoggingHookRegistration`
(`cuprum/logging_hooks.py`) is a *pair* handle: it composes two
`HookRegistration` instances and detaches them in reverse order; it
deliberately carries no token of its own.

`cuprum/unittests/test_token_registration_stateful.py` verifies the token
discipline with a Hypothesis `RuleBasedStateMachine` driving randomized
register/detach sequences across all token-backed handle types (nesting,
context-manager exit, LIFO detach, double-detach), plus an example test pinning
the documented non-LIFO hazard.

## Canonical stage-observation inputs

The observation tag schema is a wire contract for observability, so the
env-overlay resolution and base tag construction shared by the single-command
and pipeline paths live in exactly one place, `cuprum/_observability.py`:

- `_resolve_env_overlay(extra)` layers the per-call overlay (typically
  `ExecutionContext.env`) over the scoped overlay from the active
  `CuprumContext` and returns the immutable merge result. It stays overlay-only
  — `os.environ` is merged separately at spawn time by `resolve_env`.
- `_base_stage_tags(cmd, capture=…, echo=…)` builds the shared tag schema
  (`project`, `capture`, `echo`). The pipeline observation builder grafts on
  only its stage-specific keys (`pipeline_stage_index`, `pipeline_stages`);
  per-call tags are merged over the base via `_merge_tags`.

Re-use policy: the three call sites — `_prepare_execution_observation`
(`cuprum/sh.py`), `_build_pipeline_observations`
(`cuprum/_pipeline_internals.py`), and `_build_spawn_observations`
(`cuprum/_process_lifecycle.py`, which now delegates to the pipeline builder
and adds only its no-observe-hooks assertion) — must route through these
helpers. A new shared tag is added once, in `_base_stage_tags`, or it will
silently diverge between the single-command and pipeline telemetry.

`cuprum/unittests/test_stage_observation_builder.py` pins the contract with
Hypothesis properties (overlay resolution matches `merge_env_overlays`
semantics and stays immutable; both paths agree on the shared tag keys) and a
syrupy snapshot of representative single-command and pipeline tag dictionaries.

## Context allowlist internals

`CuprumContext` stores an `allowlist` plus the internal
`_allowlist_is_restricted` marker. The marker distinguishes the permissive
default context from a context that has deliberately narrowed to an empty
allowlist. It defaults to `False` on the default context and becomes `True` for
explicit narrowing, when `ScopeConfig.allowlist` is provided, or when
`with_allowlist()` receives a non-empty replacement. Direct allowlist
replacement also preserves restriction when the previous context already had an
explicit policy. Replacing that allowlist with `frozenset()` therefore cannot
widen the context back to the permissive default by accident.

`check_allowed()` therefore has two empty-allowlist modes. Empty and
unrestricted means *no policy has been established yet*, so every program is
permitted for the adoption-friendly default. Empty and restricted means a
policy has been established and then narrowed to no programs, so every program
is denied. Keeping that bit separate from the set contents prevents permission
broadening regressions where `frozenset()` could otherwise mean both "allow
everything" and "deny everything".

`narrow()` handles allowlists in three cases:

- An empty unrestricted parent uses the provided allowlist directly, creating
  the first explicit base scope.
- An empty restricted parent stays empty, preserving the deny-all result of
  earlier narrowing.
- A non-empty parent intersects its allowlist with the provided allowlist, so
  nested scopes can remove programs but cannot add new ones.

`with_allowlist()` is the direct replacement path. It preserves restriction
when the current context already has an explicit policy, even if the
replacement allowlist is empty, and a non-empty replacement establishes an
explicit policy by setting restriction. So direct replacement cannot turn a
deny-all context into the permissive default.

The allowlist, hook, and timeout rules are split into pure helpers in
`cuprum/context/_policy.py` so the invariants can be tested directly:

- `_narrow_allowlist(parent, config, parent_is_restricted=...)` returns the
  narrowed allowlist for the three parent/config cases without mutating either
  input.
- `_is_narrowed_allowlist_restricted(config, parent_is_restricted=...)`
  returns whether the child context should enforce allowlist policy after
  narrowing.
- `_merge_hooks(parent, config, *, scoped_first)` merges parent and scoped
  hooks under one generic ordering contract. With `scoped_first=False` it
  returns `parent + config`, preserving FIFO ordering for before hooks and
  observe hooks. With `scoped_first=True` it returns `config + parent`,
  preserving LIFO teardown ordering for after hooks.
- `_validate_timeout(timeout, class_name)` coerces non-negative timeout values
  to `float`, preserves `None`, and rejects negative values as well as
  non-finite values (NaN and positive or negative infinity).
- `_resolve_narrowed_timeout(parent, config)` inherits the parent timeout when
  the scoped config is silent and otherwise uses the scoped value.

Core context tests live in `cuprum/unittests/test_context.py`. Context policy
property tests live in `cuprum/unittests/test_context_narrowing.py` and
`cuprum/unittests/test_context_timeouts.py`; they exercise both ordering modes
of `_merge_hooks` (parent-first FIFO and scoped-first LIFO) through the generic
helper rather than per-hook variants. Run them directly with:

```bash
uv run pytest -q cuprum/unittests/test_context_narrowing.py \
    cuprum/unittests/test_context_timeouts.py
```

Those property modules mark pure-helper properties for optional CrossHair
execution. The `crosshair` Hypothesis profile is registered in
`cuprum/unittests/conftest.py`; using it requires the `hypothesis-crosshair`
package from the dev dependency group.

### Extracted module boundaries

Several implementation modules were split out of larger files to keep each seam
small and single-purpose. `cuprum/context/_policy.py` is described above; the
subprocess module boundaries (`cuprum/_subprocess_execution.py`,
`cuprum/_subprocess_stdin.py`, `cuprum/_subprocess_timeout.py`) and concurrent
execution are covered in [Cuprum design](cuprum-design.md) §8.1.5 (with
[ADR-007](adr-007-subprocess-execution-module-boundaries.md)) and §8.3.1
respectively, and are not repeated here.

Runtime (`cuprum/`):

- `cuprum/_pipeline_wait_records.py` — typed completion-report payloads.
  `_CompletionLogFields` carries shared completion fields; `_CompletionReport`
  carries an action, message, and optional record fields; and
  `_PipelineWaitReporter` is the optional adapter port for the three canonical
  direct pipeline-wait WARNING records. That direct-record contract is distinct
  from the typed `pipeline_fail_fast` `ExecEvent` contract.
- `cuprum/_concurrent_config.py` — the `ConcurrentConfig`/`ConcurrentResult`
  dataclasses and their validation; an implementation detail of
  `cuprum.concurrent`.
- `cuprum/_pipeline_collect.py` — drives a spawned pipeline to completion and
  collects its output; also hosts the `cuprum.sh` lazy-import shim. Its
  `_PipelineInvariantError` reports a missing lazy import or a `TimeoutError`
  reached without a configured timeout. It derives from the shared
  `_ExecutionInvariantError` and `RuntimeError`, and the timeout path chains
  the originating exception.
- `cuprum/_pipeline_stream_results.py` — pipe-result triage for pipeline
  stages.
- `cuprum/_streams_pump.py` — the stream pump loop with backpressure.
- `cuprum/adapters/tracing_protocols.py` — the canonical PEP 544 `Span`/
  `Tracer` protocols. `tracing_adapter` re-exports both;
  `_tracing_protocols.py` remains a compatibility re-export only.

Benchmarks (`benchmarks/`):

- `benchmarks/_github_http.py` — authenticated GitHub request construction,
  bounded response reads, retries, and cross-origin redirect policy for
  benchmark baseline discovery and downloads. `_load_bounded_response_bytes`
  rejects non-HTTPS URLs before constructing a request, sends the GitHub bearer
  token and API headers, and enforces the caller's byte ceiling while reading
  64 KiB chunks. `_load_json_response` uses a 1 MiB ceiling; `_download_bytes`
  uses a 64 MiB ceiling. Transient `429`, `5xx`, and URL errors are retried
  after 0.5 and 1.0 seconds before the final failure is raised. The
  `_ResponseLabels` value identifies the response kind in size-limit
  diagnostics and carries a wrapper-specific retry description. Reuse this
  helper for benchmark GitHub transfers; keep general-purpose HTTP concerns
  outside this private module.
- `benchmarks/ratchet_ratio_extraction.py` — extracts within-run Rust/Python
  ratio maps and owns validation that baseline and candidate comparison groups
  match. `benchmarks/ratchet_rust_performance.py` consumes that validation
  while orchestrating comparisons; this module owns ratio extraction and its
  comparison-group validation.
- `benchmarks/_tee_profile_worker_backend.py` — backend selection for the tee
  hot-path profiling worker (`_EnvBackendSelector` and its supporting state).

Spelling policy (`scripts/`):

- `scripts/typos_rollout_dictionary.py` — the shared dictionary model, TOML
  parsing, and merging; standard library only.
- `scripts/typos_rollout_refresh.py` — cache freshness policy: HTTP validator
  metadata, local mtime comparison, and the conditional HTTPS fetch with its
  stale-cache fallback. Redirect handling and degradation telemetry are owned by
  `scripts/typos_rollout_degradation.py`.
- `scripts/typos_rollout_degradation.py` — the HTTPS-only redirect policy and
  bounded refresh-degradation counters, exposed through `reset_degradations()`
  and `degradation_snapshot()`.
- `scripts/typos_rollout.py` remains the rendering module and public façade,
  re-exporting the API so callers keep one entry point.

Test helpers:

- `cuprum/unittests/test_maturin_pins.py` — reads and validates the
  synchronized maturin version pins; its narrowly shared exceptions live in
  `cuprum/unittests/_maturin_pin_support.py` (see
  [Maturin pin synchronization and native wheel tests](#maturin-pin-synchronization-and-native-wheel-tests)).

## `rust_consume_stream` integration status

`rust_consume_stream` is implemented, tested, and exported, but production
consumes currently go through the pure-Python `_consume_stream` function until
Phase 2 is complete. Integration is deferred to
[ADR-002: Additional Rust components](adr-002-additional-rust-components.md)
(Phase 2). The rationale is to defer consume-side dispatch until the ADR-002
Phase 2 stack is complete, including dispatcher wiring, the Python fallback
path, and parity/property coverage.

## Fail-fast reducer properties

`_build_final_results` in `cuprum/concurrent.py` is the pure reducer that
compacts fail-fast concurrent command results.  It drops `None` (cancelled)
entries and remaps failure indices to the compacted result list.  The reducer
carries explicit postcondition-style contracts in its docstring:

- `final_results` contains only non-`None` entries (cancelled slots removed).
- `len(final_results)` equals the number of non-`None` entries in `inputs`.
- Every index in `failures` is within `[0, len(final_results))`.
- `failures` is sorted in ascending order.
- Every index in `failures` points at an entry with `ok == False`.
- `failures` contains *all* such indices — no non-ok entry is omitted.
- The relative order of non-`None` inputs is preserved in `final_results`.
- `submission_indices[i]` is the original position of `final_results[i]` in
  `inputs`, so the submission order is recoverable after compaction.

These invariants are verified at two levels:

- **Hypothesis** (`cuprum/unittests/test_build_final_results_property.py`)
  generates up to 50 compact `CommandResult | None` lists and asserts
  `_build_final_results_invariants_hold` over each.  Run:

  ```bash
  uv run pytest -q cuprum/unittests/test_build_final_results_property.py
  ```

- **CrossHair** performs bounded symbolic verification over the assertion
  target.  Run:

  ```bash
  uv run crosshair check \
    cuprum.unittests.test_build_final_results_property._assert_build_final_results_invariants \
    --analysis_kind asserts
  ```

  CrossHair is a development dependency only.  The property module skips
  symbolic checks on Python 3.15, where CrossHair cannot yet trace the
  `CALL_KW` opcode (tracked in issue `#109`).

When changing `_build_final_results`, run both verification paths before
committing.

## ConcurrentResult submission mapping

`ConcurrentResult` (in `cuprum/concurrent.py`) exposes the compacted results
alongside a submission-stable mapping so callers can relate any result — or
failure — back to the command they submitted:

- `submission_indices` is a tuple parallel to `results`; each entry is
  the original submission index of the corresponding result. It defaults to the
  identity sequence when omitted (the constructor treats `None` as "omitted"),
  and a supplied sequence whose length differs from `results` raises
  `ValueError`.
- `failure_submission_indices` maps each entry of `failures` through
  `submission_indices`, yielding the original submission positions of the
  failed commands. Unlike `failures` (positions within the possibly compacted
  `results`), it is stable across collect-all and fail-fast modes.

## Environment overlay resolution

The user-facing `env(...)` context manager and the related `ScopeConfig` field
carry an *overlay-only* mapping that is layered on top of the live `os.environ`
at subprocess spawn time. The implementation sits in
`cuprum/context/env_overlay.py` and is built on three cooperating helpers:

- `merge_env_overlays(parent, child)` (public) returns an immutable
  `MappingProxyType` whose entries are `parent` updated by `child`. Either
  layer may be `None`, in which case the result is whichever layer is set (or
  `None`); empty mappings are treated as "no contribution".
- `resolve_env(*layers)` (public) returns `os.environ.copy()` updated by
  every non-empty layer, in left-to-right order. When every layer is `None` or
  empty, the helper returns `None` so the caller can pass it straight through to
  `subprocess.Popen` to mean *inherit the parent environment unchanged* — this
  is also the path that avoids the redundant `os.environ` copy.
- `_coerce_env_overlay(overlay)` (internal) wraps any caller-supplied
  mapping in `MappingProxyType(dict(overlay))` so the stored overlay cannot be
  mutated through the original reference.

The split between `merge_env_overlays` and `resolve_env` is deliberate.
`merge_env_overlays` is the overlay-only merge used by observation tagging
(`_StageObservation.env_overlay` and the `ExecEvent.env` field) — it must not
include a snapshot of `os.environ`, otherwise structured event logs would carry
the entire parent process environment on every emission. `resolve_env` is the
spawn-time merge that *does* include `os.environ`; it is called from
`_process_lifecycle._merge_env` for both the single-command and pipeline paths.

The live-view contract from issue #100 is enforced at one place only:
`resolve_env` reads `os.environ` at call time, not when the overlay is
registered. Any code that touches the spawn path must therefore route through
`resolve_env` (directly or via `_merge_env`) — never via a captured snapshot of
`os.environ` at registration time.

The `CuprumContext.env_overlay` field is a `MappingProxyType` (or `None`) and
is itself part of the immutable context dataclass.
`scoped(ScopeConfig(env_overlay=...))` and `env(...)` both build a new
`CuprumContext` via `with_env_overlay`, capture the resulting `ContextVar`
token, and reset it on scope exit; nested scopes therefore behave as a stack
and are restricted by the same LIFO detach rule as `AllowRegistration` and
`HookRegistration`.

Property tests for the merge and resolve invariants live in
`cuprum/unittests/test_env_context_properties.py`. They use
[Hypothesis](https://hypothesis.readthedocs.io/) to exercise arbitrary layer
counts, payload contents, and overlap patterns, and to confirm that the helpers
never mutate caller-supplied mappings.

## Pipeline throughput benchmark configuration

`PipelineBenchmarkConfig` controls the hyperfine-based end-to-end throughput
suite in `benchmarks/pipeline_throughput.py`. Scenario commands run
`benchmarks/pipeline_worker.py` with `python_bin`, which defaults to the active
interpreter and is resolved to an absolute executable path before measured
non-dry-run benchmarks. The measured command intentionally avoids `uv run` so
the Rust ratchet measures worker pipeline throughput rather than environment
startup overhead.

Each worker process executes `worker_iterations` pipeline runs (default: 20).
Hyperfine therefore measures a batched worker invocation rather than one cold
pipeline execution, reducing Python interpreter startup noise in the ratchet.
The ratchet itself compares each scenario's within-run
`rust_mean / python_mean` ratio between the baseline and candidate runs, so
runner-speed differences and residual startup overhead cancel out of the
comparison. Its CI profile places each matched Python/Rust scenario pair next
to each other and records ten measured runs per command, reducing temporal
runner drift and three-sample outliers. Dry-run plans record
`benchmark_profile_version` and `worker_iterations`; ratchet comparison skips
older baseline artefacts whose profile metadata does not match the current
benchmark shape.

The remaining fields follow the benchmark plan: `output_path` receives
hyperfine JSON or dry-run plan JSON, `worker_path` points at the worker module,
`scenarios` supplies the rendered command matrix, `warmup` and `runs` configure
hyperfine iteration counts, `hyperfine_bin` selects the hyperfine executable,
`dry_run` writes the plan without invoking hyperfine, and `rust_available`
records whether Rust scenarios are included.

`uv_bin` is a deprecated legacy field that remains accepted in the dataclass
for backward compatibility, but current benchmark command construction ignores
it entirely. Keep it unset in new usage and set `python_bin` when a specific
interpreter is required. In dry-run mode, command rendering does not resolve
`python_bin` via PATH.

### The baseline the ratchet compares against

The bar is the median of a rolling window of the last seven `main` runs, held in
`main-baseline-history.json` inside the `benchmark-ratchet-main-baseline`
artefact. `benchmarks/ratchet_history.py` owns the window;
`benchmarks/update_baseline_history.py` appends to it.

It used to be the single latest `main` measurement, and two properties of that
arrangement combined into a failure that no re-run could clear:

- **One sample is not an estimate.** Its noise was the bar's noise.
- **The sample was only published if its own run passed.** A run passes when
  it is no more than 30% *slower* than the bar, so an anomalously fast
  measurement was always accepted, while the ordinary measurements that would
  have corrected it were rejected — a bar biased towards the low tail of the
  noise, and sticky once it got there.

On 2026-08-06 a `main` run measured `medium-single-nocb` at 0.760 against a
baseline of 1.013, passed as a 25% improvement, and published. Pull requests
whose code was identical to `main` then reported a 46% regression on that one
scenario, three re-runs included, while the other three scenarios agreed with
the baseline to within 0.14. Issue #219 has the wider analysis.

Both properties are fixed, and both fixes are needed:

- The window's **median** is the bar, so one sample cannot be it. The spread
  of those samples then widens the threshold: a candidate must exceed both the
  flat 0.30 and three estimated standard deviations of the observed spread. The
  estimate is `3 * 1.4826 * MAD` relative to the median, because the outlier
  being tolerated barely moves the median absolute deviation but would inflate
  a standard deviation in proportion to itself. The band is capped at 1.00 —
  past that the benchmark indicates that it cannot measure what it gates on,
  and an uncapped band would disable the ratchet silently instead of saying so.
- **Every non-cancelled completed `main` run records its sample and publishes
  the artefact**, whichever way its own ratchet went. That is why the recording
  and upload steps are gated on `!cancelled()` rather than on success, and why
  the fetch passes `--run-status completed`: a run that failed its own ratchet
  is still part of the compatible history. A cancelled run records no
  half-finished measurement.

The window makes the *bar* robust to one noisy run. It cannot make the
*candidate* robust: a pull request is measured once, on whichever runner CI
gave it. So when the comparison reports a regression, the job measures again
under the `confirmation` prefix, compares again against the same window, and
`benchmarks/ratchet_confirmation.py` performs the typed intersection while
`benchmarks/confirm_regression.py` adapts the reports and CLI — a scenario
fails only if it regressed both times. A flake has to land on the same scenario
twice to survive, which turns a one-in-N false failure into roughly one-in-N².
The second benchmark is only ever spent on a run that was about to fail, so
ordinary runs cost exactly what they did before.

Three properties of that pass are load-bearing:

- Confirmation may only turn a failure into a pass. A scenario the first run
  did not flag is not failed by the second, or re-measuring would be a second
  chance to fail and would double the false failures it exists to halve.
- A confirmation that could not compare at all — a skip report — leaves the
  first verdict standing. The primary comparison succeeded on the same inputs,
  so an unusable confirmation is a fault in the retry, not evidence about the
  candidate.
- The re-measurement writes under its own prefix, so `candidate-*`, and
  therefore the sample recorded into the window, stays the primary measurement.
  Recording the confirming run instead would put a second sample into the
  window only for the merges that were about to fail — a verdict-dependent bias
  in the samples, which is the thing being removed.

An exit code of 2 from the comparison is malformed input rather than a
regression, and fails on the spot rather than spending a second benchmark to
reread the same broken file.

Two consequences are worth knowing. A re-run cannot clear a ratchet failure
caused by the window, because a re-run does not change the window — only a
merge does. And a regression that survives several merges eventually enters the
window and raises the bar; the flat threshold still applies to each step, but
the ratchet measures drift from recent `main`, not from a fixed point.

Samples are pruned to those sharing the candidate's `benchmark_profile_version`
and `worker_iterations`. A window emptied that way, or absent on a first run or
after the artefact expires, falls back to comparing against the latest completed
`main` baseline artefact as a single sample — the pre-window bar, reported in
`ratchet-report.json` as `baseline_sample_count: 1` so a surprising verdict can
be read against the evidence behind it.

Every `ratchet-report.json` carries a bounded decision record.
`baseline_source` is `history`, `fallback`, or `none`; `baseline_reason` is
`compatible_history`, `history_unavailable`, `no_compatible_history`,
`no_baseline_available`, or `incompatible_profile`. The
`compatible_sample_count` records compatible samples in the selected window
before scenario-specific selection, while `comparison_state` is `compared`,
`skipped_no_baseline`, or `skipped_incompatible_profile`. These fields explain
passes, regressions, fallbacks, and intentional skips from the artefact.

The combined confirmation report preserves `confirmation_performed` and adds
`confirmation_status`: `not_required`, `confirmed`, `unconfirmed`, or
`unavailable`. It records whether retry evidence reproduced, cleared, or could
not re-measure a primary regression; confirmation-only regressions cannot
change the verdict.

### Benchmark-ratchet implementation boundaries

The ratchet is split by responsibility rather than by the workflow steps that
invoke it:

- `benchmarks/ratchet_history.py` contains the typed rolling-window model.
  `HistorySample` records ratios and provenance, `BaselineHistory` filters and
  appends samples, and `RatchetPolicy` holds the flat and noise thresholds.
  `median_ratio` and `noise_tolerance` are the statistics used by the policy.
  `history_from_payload` validates the JSON shape; `load_history` and
  `write_history` are the file adapter, with the latter replacing the output
  atomically. `load_history` distinguishes an absent history
  (`BaselineHistoryNotFoundError`, the only empty-window fallback) from
  unreadable or invalid data (`BaselineHistoryReadError`).
- `benchmarks/ratchet_ratios.py` is the benchmark-data adapter. `load_plan`
  and `load_throughput` validate the two input payloads, `run_ratios` derives
  the matched Rust/Python ratios, and `profile_metadata` exposes the profile
  version and worker-iteration contract used for compatibility filtering.
- `benchmarks/ratchet_baseline.py` selects compatible history or the
  single-sample fallback and returns its typed `RatchetDecision` provenance.
  `ratchet_rust_performance.py` consumes that selection during orchestration.
- `benchmarks/ratchet_rust_performance.py` is the comparison entry point. Its
  `compare_rust_regressions` function consumes the selected baseline and
  comparison-group validation from `ratchet_ratio_extraction`, and returns the
  typed report; `main` supplies the CLI exit status and `write_report`
  serializes the result. A malformed input returns status 2, a regression
  status 1, and a passing or profile-skipped comparison status 0.
- `benchmarks/ratchet_confirmation.py` is the pure typed retry policy.
  `confirm_regressions` intersects the primary and confirmation regression
  lists, preserving the primary verdict when confirmation has no comparison
  evidence. `benchmarks/confirm_regression.py` is the JSON/CLI adapter: it
  writes the combined report and returns status 1 only for a reproduced
  regression.
- `benchmarks/update_baseline_history.py` is the `main`-run recorder. It loads
  the previous history, derives one sample from the candidate plan and
  throughput, appends it when valid, and always writes the resulting history
  file. Missing or malformed candidate measurements carry the existing window
  forward; an unreadable history or failed write returns status 2.

Keep these boundaries intact when changing the ratchet: ratio extraction must
remain shared by comparison and recording, while history compatibility and
regression policy must not be reimplemented in the workflow or in JSON callers.

## Profiling harness overview

The profiling benchmark harness provides deterministic parent-side tee and
capture hot-path profiling for Cuprum, distinct from end-to-end throughput
benchmarks that measure whole pipelines. It lives under `benchmarks/`, uses
Linux `perf` as the primary profiler, supports optional `py-spy` corroboration,
and can run unprofiled smoke scenarios when only command construction and
worker behaviour need to be checked.

### Profiling prerequisites and build settings

Linux is the reference platform for profiler artefacts. Symbol-quality and
sampling settings must match those used to collect the baseline, otherwise the
call graphs lose Rust and Python frames:

- Build the native extension with frame pointers so `perf` can unwind mixed
  Python and Rust stacks: set `RUSTFLAGS="-C force-frame-pointers=yes"`, then
  run
  `uv run maturin develop --release --manifest-path rust/cuprum-rust/Cargo.toml`
  from the repository root (as in the reproduction block below).
- Export `PYTHONPERFSUPPORT=1` so CPython emits `perf` map entries for
  interpreted frames.
- Sample with `perf record -F 999 -g --call-graph dwarf,16384`. DWARF
  unwinding is more robust than frame-pointer-only unwinding for the mixed
  stacks here; the driver applies these defaults and exposes `--perf-frequency`
  and `--perf-call-graph` overrides.
- Grant `perf` permission to collect user-space samples. The baseline was
  taken at `perf_event_paranoid=2`, which is sufficient for the user-space call
  graphs this harness needs; kernel symbols remain unresolved at that level
  (raw addresses in the call trees). Check the current level with
  `cat /proc/sys/kernel/perf_event_paranoid`. If sampling is denied, either
  lower it on the host (`sudo sysctl -w kernel.perf_event_paranoid=2`, or a
  lower value such as `1` or `-1` if kernel frames are also required), or grant
  `CAP_PERFMON` to the `perf` binary (for example with `setcap`).
- Install `perf`, `inferno-collapse-perf`, and optionally `py-spy` on `PATH`.

These settings, the deterministic fixtures, and the full reproduction sequence
are recorded in
[the tee hot-path profiling baseline](tee-hotpath-profiling-baseline-2026-06-12.md).
The harness reproduction entrypoint is:

```bash
export RUSTFLAGS="-C force-frame-pointers=yes"
export PYTHONPERFSUPPORT=1
uv run maturin develop --release --manifest-path rust/cuprum-rust/Cargo.toml
uv run python benchmarks/profile_tee_hotpath.py --profiler perf run
```

## Fixture generation (`benchmarks/deterministic_b64_fixture.py`)

`FixtureConfig` describes deterministic fixture generation with three fields:
`seed`, `raw_bytes`, and `wrap`. `raw_bytes` must be greater than or equal to
zero, and `wrap` must be one of `0` or `76`; `wrap=0` writes unwrapped base64
output, while `wrap=76` writes line-oriented output for callback scenarios.

The generator uses an SHA-256 (Secure Hash Algorithm 256) counter-mode seeded
stream. It encodes `str(seed).encode("utf-8")` plus successive big-endian
counters, reads deterministic bytes in stable chunks, base64-encodes those
chunks, and streams the encoded output to disk. The JSON (JavaScript Object
Notation) manifest records `seed`, `raw_bytes`, `wrap`, `output_bytes`,
`sha256`, and `algorithm`.

Use the command-line interface (CLI) as a module entry point:

```bash
python -m benchmarks.deterministic_b64_fixture \
  --seed N \
  --raw-bytes N \
  --wrap 0|76 \
  --output F \
  --manifest M
```

## Sink model (`benchmarks/sinks.py`)

<!-- markdownlint-disable MD013 -->

| Sink kind        | Implementation                                   | Cost model                                                                        |
| ---------------- | ------------------------------------------------ | --------------------------------------------------------------------------------- |
| `devnull`        | Operating system null device                     | Discards bytes without allocation.                                                |
| `text_blackhole` | `TextBlackhole` text stream                      | Counts characters and exposes no `.buffer`, forcing the text branch.              |
| `pty_blackhole`  | `PtyBlackhole` pseudo-terminal master/slave pair | Drains the master side from a daemon thread to simulate terminal-like throughput. |

<!-- markdownlint-enable MD013 -->

`PtyBlackhole` transfers master-file-descriptor ownership to its daemon drainer
before `__enter__` returns. The drainer counts raw bytes read from the master,
while the caller writes text to the slave stream. On exit, the slave and master
are closed and the drainer is given a bounded five-second join. `drained_bytes`
exposes the count only after the drainer has terminated; it is `None` while a
timed-out drainer is still running. Tests that verify the count should write a
multibyte payload without a newline and compare it with the payload's UTF-8
encoded byte length, since PTY line-ending translation would otherwise make the
result platform-dependent.

## Worker (`benchmarks/tee_profile_worker.py`)

`TeeProfileWorkerConfig` defines one worker run. It validates that
`fixture_path` points to an existing file, `stages >= 1`, `repeat_count >= 1`,
and that `mode`, `sink_kind`, and `backend` are members of the supported
literal sets. It also carries `encoding` and `errors`, which default to `utf-8`
and `replace`.

`run_tee_profile_worker` builds a command or pipeline through `_build_command`,
selects the stream backend through `_EnvBackendSelector`, runs the workload
`repeat_count` times, accumulates `captured_output_length` and
`stdout_line_count`, and returns a `TeeProfileWorkerResult`. The worker result
is the machine-readable payload written by the worker CLI and by the scenario
driver.

The worker mode maps directly to Cuprum's final consume flags:

| Mode      | `capture` | `echo`  |
| --------- | --------- | ------- |
| `echo`    | `False`   | `True`  |
| `capture` | `True`    | `False` |
| `tee`     | `True`    | `True`  |

Backend selection is process-local and environment-driven. `auto` unsets
`CUPRUM_STREAM_BACKEND`; `python` and `rust` set it explicitly.
`_EnvBackendSelector` holds a process-wide lock while the worker runs so
concurrent benchmark workers cannot race on `os.environ` or the backend
availability and selection caches. The selector clears those caches before
entering the context and again when restoring the previous environment value.
It holds `_BACKEND_LOCK` across the complete `repeat_count` loop, serializing
concurrent benchmark workers for the full worker workload, including every
`run_sync` subprocess execution. Consequently, workers that require different
or process-local stream backend selection cannot execute their repeat loops in
parallel, which limits their aggregate throughput. It is intentionally not
re-entrant: a thread-local guard detects nested entry on the same thread, logs
the rejected backend and thread identifier, and raises
`ReentrantBackendSelectorError` (a `RuntimeError` subclass, retained for
backward compatibility) before mutating backend state.

### Selector observability metrics

`TeeProfileWorkerResult` includes selector metrics gathered while activating
the backend. The metrics are thread-local, reset for each worker run, and
reported with the rest of the worker payload. They represent per-invocation
totals for `TeeProfileWorkerResult`, not process-lifetime aggregates.

| Field                       | Type    | Description                                                                          |
| --------------------------- | ------- | ------------------------------------------------------------------------------------ |
| `lock_wait_seconds`         | `float` | Cumulative time spent blocking on `_BACKEND_LOCK` during selector activation.        |
| `reentrant_rejection_count` | `int`   | Count of selector re-entrancy violations detected and rejected on the worker thread. |

*Table: Selector observability metrics reported in each
`TeeProfileWorkerResult`, with field name, type, and what each value records.*

## Scenario driver (`benchmarks/profile_tee_hotpath.py`)

`benchmarks/profile_tee_hotpath.py` remains the public driver and module entry
point. It re-exports the stable API while the implementation is split across
supporting modules: scenario composition in
`benchmarks/tee_profile_scenarios.py`, profiler orchestration in
`benchmarks/tee_profile_profilers.py`, and command-line interface and JSON
output helpers in `benchmarks/tee_profile_driver.py`. `TeeProfileScenario`
records a resolved scenario: name, fixture path, stage count, mode, sink kind,
line-callback flag, backend, repeat count, encoding, and error handling.
`TeeProfileDriverConfig` records fixture paths, output directory, profiler
choice, warm-up count, measured repeat count, `perf` frequency, call-graph
configuration, and an optional scenario name. It validates that run counts and
`perf` frequency are in range, and that the `perf` call-graph setting is not
blank.

The default matrix is stable and ordered:

1. `echo-devnull-nocb-s1`
2. `echo-textblackhole-nocb-s1`
3. `echo-pty-nocb-s1`
4. `tee-devnull-nocb-s1`
5. `echo-devnull-cb-s1`
6. `echo-devnull-nocb-s4-python`
7. `echo-devnull-nocb-s4-rust`

The Rust scenario is conditional on `can_use_rust_backend()`, so pure-Python
installs omit `echo-devnull-nocb-s4-rust` from the plan before execution.

The driver exposes three CLI subcommands:

- `plan` emits a JSON plan with the resolved `worker_command` for each
  scenario.
- `run-scenario` runs one named scenario with warm-up executions followed by one
  measured run.
- `run` executes the full matrix serially.

Profiler modes are selected through `TeeProfileDriverConfig.profiler`. `none`
runs the worker directly and writes a note that profiler artefacts were not
generated. `perf` uses Linux `perf record`, then post-processes with
`perf report`, `perf script`, and `inferno-collapse-perf`. `py-spy` runs the
optional Python-first profiler and writes its raw output.

### Profiler adapter protocol

Profiler orchestration is decoupled from scenario execution through the
`ProfilerAdapter` protocol (defined in `benchmarks/tee_profile_profilers.py`).
Any object with a `run(scenario, *, scenario_dir, config)` method satisfies the
protocol. Three concrete adapters are provided:

<!-- markdownlint-disable MD013 -->

| Adapter class    | `profiler` value | Behaviour                                                                                                                                         |
| ---------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `_NoneProfiler`  | `"none"`         | Runs the worker directly and writes `notes.txt` explaining that profiler artefacts were not generated.                                            |
| `_PerfProfiler`  | `"perf"`         | Records `perf.data`, generates `perf.report.txt` and `stacks.folded` via `inferno-collapse-perf`, and summarizes folded stacks to `summary.json`. |
| `_PySpyProfiler` | `"py-spy"`       | Records a raw `py-spy` trace to `pyspy.raw`.                                                                                                      |

<!-- markdownlint-enable MD013 -->

`_profiler_for(name)` is the factory that maps a `ProfilerName` literal to its
adapter. Adding a new profiler requires implementing the protocol and
registering it in `_profiler_for`.

### `TeeProfileScenario` semantics

`TeeProfileScenario` is a frozen dataclass representing one fully resolved
profiling scenario. Its fields are:

<!-- markdownlint-disable MD013 -->

| Field                 | Type           | Description                                                                     |
| --------------------- | -------------- | ------------------------------------------------------------------------------- |
| `name`                | `str`          | Unique scenario identifier, used as the sub-directory name under `output_dir`.  |
| `fixture_path`        | `pathlib.Path` | Path to the base64 fixture file replayed by the worker.                         |
| `stages`              | `int`          | Number of pipeline stages (1 = single stage, >1 = chained pass-through stages). |
| `mode`                | `TeeMode`      | Consumption mode: `"echo"`, `"capture"`, or `"tee"`.                            |
| `sink_kind`           | `SinkKind`     | Output sink variant used during execution.                                      |
| `with_line_callbacks` | `bool`         | Whether stdout-line observers are registered during the run.                    |
| `backend`             | `BackendName`  | Stream backend: `"auto"`, `"python"`, or `"rust"`.                              |
| `repeat_count`        | `int`          | Number of measured repetitions.                                                 |

<!-- markdownlint-enable MD013 -->

`as_dict()` returns a JSON-serializable dictionary. `worker_config()` converts
the scenario into a `TeeProfileWorkerConfig`, optionally overriding
`repeat_count`.

### Worker configuration validation

`TeeProfileWorkerConfig.__post_init__` delegates validation to three private
methods:

- `_coerce_fixture_path` coerces `fixture_path` to `pathlib.Path` and raises
  `ValueError` if the path does not refer to an existing file.
- `_validate_numeric_bounds` raises `ValueError` if `stages < 1` or
  `repeat_count < 1`.
- `_validate_enum_fields` raises `ValueError` if `mode`, `sink_kind`, or
  `backend` are not members of the respective `_VALID_*` sets.

### Worker test suite layout

The worker test suite is split across focused modules so that each file covers
one boundary of behaviour:

- `cuprum/unittests/test_tee_profile_worker_core.py` covers parent-side consume
  hot-path execution, result accounting, and snapshotted worker output.
- `cuprum/unittests/test_tee_profile_worker_cli.py` covers CLI invocation, the
  JSON payload shape, and `TeeProfileWorkerConfig` validation errors.
- The `_EnvBackendSelector` concurrency coverage is itself split across two
  modules sharing a common support module, keeping each file's responsibility
  count within the cohesion budget:
  - `cuprum/unittests/test_tee_profile_worker_selector_reentrancy.py` — the
    `_BACKEND_LOCK` `RLock` reentrancy guarantee, plus same-thread re-entrant
    selector rejection, recovery, and the structured warning log (snapshot).
  - `cuprum/unittests/test_tee_profile_worker_concurrency.py` — concurrent
    `run_tee_profile_worker` race-freedom across backend pairs and
    `CUPRUM_STREAM_BACKEND` preservation under concurrent, interleaved access.
  - `cuprum/unittests/_tee_profile_concurrency_support.py` — the coordinating
    backend selectors and race harness, plus the timeout constants, worker
    plumbing, and the thread join/assert helper used by those tests.
  - `cuprum/unittests/_tee_profile_signalling_lock.py` — the instrumented
    `_SignallingRLock` wrapper that flags first-observed lock contention.
  - `cuprum/unittests/_tee_profile_backend_support.py` — backend-availability
    detection and the Hypothesis backend strategies/parametrization shared by
    the concurrency and reentrancy modules.
- `cuprum/unittests/test_tee_profile_worker_selector_metrics.py` covers the
  selector observability metrics (`lock_wait_seconds`,
  `reentrant_rejection_count`): their accumulation, thread-locality, reset per
  run, and presence in the worker result payload.

Keeping the concerns in separate files makes the coverage boundary explicit: a
change to command construction touches the core module, a change to the CLI
contract touches the CLI module, a change to backend locking or the selector
state machine touches one of the two concurrency modules (with shared
scaffolding in the support module), and a change to selector metrics touches
the metrics module.

### `_EnvBackendSelector` concurrency invariants

The two concurrency modules verify the `_EnvBackendSelector` state machine that
serializes process-local backend selection. The selector is backed by a
process-wide reentrant lock (`_BACKEND_LOCK`) and a thread-local reentrancy
guard; the tests assert the following invariants:

1. `_BACKEND_LOCK` is held for the full duration of the selection context.
2. `os.environ["CUPRUM_STREAM_BACKEND"]` is restored to its previous value on
   context exit.
3. Backend availability and dispatch caches are cleared on entry and on exit.
4. Same-thread reentrancy is rejected before any nested environment mutation.

These invariants mirror the state transitions a threading-level model checker
would explore. Candidate full model-checking routes include `pynusmv` and
translating the selector state machine to Promela for SPIN (Simple Promela
Interpreter). Full tool integration is out of scope; the explicit checkpoint
tests keep the observable states aligned with the model such tools would verify.

#### Hypothesis property-based generation

[Hypothesis](https://hypothesis.readthedocs.io/) generates the input domains
that fixed examples cannot cover exhaustively:

- `test_nested_selector_rejects_generated_backend_pairs` draws an outer and an
  inner backend from the available set and asserts that same-thread nested
  entry always raises `ReentrantBackendSelectorError` before mutating backend
  state, regardless of which backend pair is generated.
- `test_generated_concurrent_workers_complete` draws a thread count (2–8) and a
  same-length sequence of backend selections, then runs one worker per backend
  concurrently and asserts every worker completes with `status == "ok"` and
  `exit_code == 0`.

The strategies sample only backends available in the current environment
(`_available_backend_names`), so pure-Python installs omit the Rust backend
from generated cases rather than skipping individual examples. Both generated
tests set `deadline=None` because real worker execution time is not a useful
signal for these properties, and suppress the `function_scoped_fixture` health
check because each example reuses the per-test `tmp_path` fixture.

#### Checkpointed interleaving tests

Property generation establishes that races do not occur across the input
domain; the checkpointed tests prove *why* by pinning a specific interleaving
that would expose a missing lock. They inject a coordinating backend selector
and a `_SignallingRLock` wrapper that signals when a blocking acquire first
observes contention, then drive two worker threads through a deterministic
schedule using `threading.Event` checkpoints:

- `test_concurrent_workers_preserve_backend_environment` holds the lock in the
  first ("python") worker while a second worker contends, and asserts the first
  worker's view of `CUPRUM_STREAM_BACKEND` stays pinned to `"python"`.
- `test_selector_interleaving_blocks_environment_observation_until_unlock`
  asserts the second worker cannot enter its context — and therefore cannot
  observe the environment — until the first worker releases the lock, yielding
  the serialized observation sequence `["python", None]`.

When changing `_EnvBackendSelector`, `_BACKEND_LOCK`, or the reentrancy guard,
run the two concurrency modules together:

```bash
uv run pytest cuprum/unittests/test_tee_profile_worker_selector_reentrancy.py \
  cuprum/unittests/test_tee_profile_worker_concurrency.py
```

## Rust availability probe stack

The Rust availability API uses a single source of truth:

- `cuprum.rust.is_rust_available()` is the public entry point and returns
  `cuprum._backend._check_rust_available()`.
- `_backend._check_rust_available()` is `functools.lru_cache(maxsize=1)` and
  short-circuits to `set_rust_availability_for_testing()` while an override is
  active; otherwise it delegates to the private `_rust_backend.is_available()`
  probe.
- `get_stream_backend()` reads the same cached resolver, so
  `CUPRUM_STREAM_BACKEND` dispatch and calls to `cuprum.is_rust_available()`
  stay aligned.
- Tests can force a deterministic result via
  `set_rust_availability_for_testing(is_available=...)`, which also clears the
  two relevant caches.

Keep tests that bypass `cuprum.is_rust_available()` focused on this private
layer, because `_rust_backend.is_available()` is an uncached import probe
without lifetime caching.

## Folded-stack summarizer (`benchmarks/summarize_folded.py`)

The folded-stack summarizer consumes one text file where each non-empty line
has the form `frame1;frame2 count`. Malformed lines, empty stacks, and
non-positive sample counts are ignored.

It writes a JSON summary with `total_samples`, `top_inclusive_frames`,
`top_leaf_frames`, and `top_stacks`. Frame entries include inclusive samples,
leaf samples, normalized percentages, and example stacks, while stack entries
record sample counts and percentages.

Inclusive frame accounting counts each distinct frame name **once per stack**,
regardless of how many times it appears in that stack (for example, through
recursion or inlined duplicate symbols). This matches the convention used by
most sampling profilers: a recursive frame inflates the wall-time cost of the
leaf, not the inclusive tally of every caller on the path.

## Makefile tooling changes

`LOCAL_TOOL_ENV` prepends `~/.local/bin` and `~/.bun/bin` to `PATH` for `uv`
and tool-discovery recipes only. This supports non-interactive Continuous
Integration/Continuous Delivery (CI/CD) hook environments without globally
shadowing system tools for unrelated Makefile workflows.

## Rust error taxonomy (`PumpError`)

The `cuprum-rust` crate reports stream pump and consume failures through one
semantic error enum, `PumpError` (`rust/cuprum-rust/src/errors.rs`), derived
with `thiserror`:

- `LengthOverflow` — an integer length conversion overflowed its target
  type ("impossible" on supported platforms, kept observable rather than
  silently truncating).
- `BufferRangeExceeded` — a computed range exceeded the backing buffer.
- `Io(io::Error)` — an operating-system I/O failure (transparent wrapper).

Conversion to a Python exception happens in exactly one place
(`From<PumpError> for PyErr`). The overflow variants surface as plain
`OSError`. The non-fatal write classification (broken pipe / connection reset)
lives on the enum as `PumpError::is_nonfatal_write`, replacing the free
function the splice and read/write paths previously shared. New failure
conditions get a variant here rather than a stringly-typed
`io::Error::other(...)`.

### Preserving the operating-system error code

The `Io` variant needs care, because PyO3's own `From<io::Error> for PyErr`
loses the number. It selects the exception *type* from `io::ErrorKind` and then
constructs it with a single argument, the error's `Display` string — and Python
populates `OSError.errno` and `OSError.strerror` only when it receives **two or
more** arguments. The number therefore survived in the message text and nowhere
a caller could branch on, forcing callers to parse English to tell `EBADF` from
`EPIPE`. This was issue `#265`; `io_error_to_py_err` now handles it.

The construction is platform-specific, so it sits behind a `cfg`-selected
`os_error_to_py_err`:

- **Unix** — `raw_os_error` *is* an `errno`, so the two-argument form
  `OSError(code, strerror)` is exactly right. It also fixes the exception type
  for free: CPython maps the errno to the matching subclass itself, so
  `OSError(32, ...)` *is* a `BrokenPipeError` and reading a directory raises
  `IsADirectoryError`. That is the same subclass selection PyO3 reached for
  through `ErrorKind`, taken from the authoritative source instead of a
  parallel table.
- **Windows** — `raw_os_error` carries a `GetLastError` code, not an `errno`.
  Passing it as an `errno` would assign an unrelated number
  (`ERROR_INVALID_HANDLE` is 6, which as an `errno` is `ENXIO`) and pick the
  subclass from it. The five-argument form
  `OSError(errno, strerror, filename, winerror, filename2)` is the one that
  carries a native code: given a `winerror`, CPython ignores the `errno`
  argument, derives `errno` from the Win32 code, and selects the subclass from
  the derived value, so all three agree.

Two details are worth knowing before changing this code. First, `io::Error`
renders a raw OS error as `"{strerror} (os error {code})"`, so the suffix is
stripped before it becomes `strerror` — otherwise the number appears twice, as
`"[Errno 9] Bad file descriptor (os error 9)"`. `strip_os_error_suffix` is
anchored to *this* error's own code so it can never truncate a message
belonging to a different one; three proptests pin that. Second, an `io::Error`
with no `raw_os_error` — one synthesized in Rust rather than returned by a
syscall — has no number to preserve, so PyO3's `ErrorKind` mapping remains the
best available and is used unchanged.

`cuprum/unittests/test_rust_errno.py` covers the POSIX arm and
`cuprum/unittests/test_rust_errno_windows.py` the Windows one, with each set of
assertions scoped to the platform whose taxonomy it names — the POSIX cases
name an `errno` and the subclass CPython derives from it, the Windows case
names a `winerror` and the `errno` CPython derives from *that*. The Windows
case does not hard-code either expectation: it reads them back from an
`OSError` it builds from the observed `winerror`, so it pins the derivation
without depending on which Win32 code the failure happens to raise.

What actually executes is narrower than what is written, so do not read a green
run as coverage of both arms:

- **POSIX** — the cases run natively on Linux whenever the extension is built,
  so a local `make develop` followed by `make test-extension` executes them.
  `cuprum/unittests/test_rust_errno.py` is one of the `EXTENSION_TEST_TARGETS`,
  so CI's `extension-tests` job runs them with
  `CUPRUM_REQUIRE_RUST_EXTENSION=1`: a build that never produced the extension
  fails rather than skipping quietly (`#258`).
- **Windows** — the `#[cfg(windows)]` arm is compiled natively on
  `windows-2022` by the wheel build (`.github/workflows/build-wheels.yml`,
  reached unconditionally from `ci.yml`, so it runs on every pull request),
  which catches a Windows arm that does not build or type-check. No job runs
  the Python suite on Windows, so nothing executes the `winerror` assertions;
  that gap is tracked by `#277`.

That wheel build is a plain `maturin build`, so it compiles the Windows arm
without `-D warnings`. It therefore catches a Windows arm that fails to
compile, and only that. Warn-level regressions pass it — and the cheapest way
to introduce one is to gate a helper on `#[cfg(unix)]` incorrectly, which makes
it dead code on Windows rather than a compile error. `make lint-windows` closes
that gap by running Clippy for the Windows target with the same `-D warnings`
posture the host gate uses:

```bash
rustup target add x86_64-pc-windows-msvc
make lint-windows
```

No Windows machine is needed: Clippy type-checks without linking, so the target
standard library is sufficient. `PYO3_CROSS_PYTHON_VERSION` is required and the
Makefile sets it, because PyO3 cannot probe an interpreter for the target
platform and so the ABI version has to be stated explicitly; keep the
`WINDOWS_PYTHON_VERSION` variable in step with the `python-version` the Windows
wheel job builds against.

`make lint-windows` is deliberately not part of `make lint`. It hard-fails when
the target standard library is absent rather than skipping, and requiring every
contributor to install a second standard library before any lint run is a poor
trade for a check this narrow. CI's `lint-test` job installs the target and
runs it on every pull request, which is where it has to hold.

This is the only check in the repository that reads the `#[cfg(windows)]`
branches with warnings denied. A trybuild case cannot substitute for it:
trybuild compiles its fixtures for the host target, so on a Linux runner it
sees the `#[cfg(unix)]` arm and never the Windows one.

## Rust FD-borrow ownership contract

The pump and consume entry points in `rust/cuprum-rust/src/lib.rs` sort every
descriptor they touch into a *borrowed* or a *consumed* role. `pump_stream`
borrows its reader and consumes its writer; `consume_stream` borrows its reader
and takes no writer at all. The borrow half is centralized in one helper,
`with_borrowed_reader`. The helper rebuilds a `StreamHandle` from the
caller-owned raw descriptor, wraps it in `ManuallyDrop`, and runs the caller's
closure against it. `ManuallyDrop` suppresses the close on *every* exit path —
a normal return and unwinding from a panicking operation alike — so a
descriptor the Python side still owns is never closed by Rust. `pump_stream` and
`consume_stream` both route their reader through the helper, keeping the
"borrow this FD without owning it" rule in a single place.

The private `stream_pyfunctions::run_stream_operation` helper is limited to the
two PyO3 stream exports. It owns their shared buffer validation, reader
descriptor preparation, GIL release, and `PumpError` conversion; the pump
export alone prepares its ownership-consuming writer and both exports supply
their stream operation. Do not reuse it outside this FFI adapter boundary or
move writer ownership into the helper, because that would blur the distinct
borrow-versus-consume contract.

This supersedes an earlier pattern that reconstructed the handle and called
`std::mem::forget` after the inner operation returned. Because a panic unwinds
past the trailing `forget`, that pattern dropped — and therefore closed — the
caller-owned descriptor on the unwind path, exposing the Python transport to a
double close of the same FD (the `#125` panic-unwind hazard). `ManuallyDrop`
holds regardless of how the scope exits, so no drop guard or `forget` call is
required.

There is deliberately no borrowed *writer* variant. The writer FD handed to
`pump_stream` is consumed: it must close on drop — including during unwinding —
so downstream readers observe EOF. Reconstruct the writer with
`stream_from_raw` (which yields an owning handle) and let it drop; reserve
`with_borrowed_reader` for descriptors whose ownership stays with the caller.
Python callers must therefore fully relinquish the writer descriptor supplied to
`pump_stream`/`rust_pump_stream`. The pipeline caller does this by passing an
`os.dup` of the asyncio transport descriptor: asyncio keeps and closes the
original, while Rust closes the received duplicate on drop to signal EOF. The
two descriptor numbers must never be shared between those owners. The helper's
safety contract obliges the caller to guarantee `fd` is a valid open descriptor
(or Windows handle) for the duration of the call and that ownership remains
with the caller; in return the helper guarantees it never closes `fd`.

The contract is checked at two levels, which are deliberately not
interchangeable. `rust/cuprum-rust/src/lib_tests.rs` holds the
integration-level regression tests: they open *real* descriptors, run the
helper both normally and through a panicking operation, and assert the borrowed
FD is still open afterwards. That is the only place actual `close(2)` behaviour
is exercised, and it stays the authority on it. Alongside them,
`rust/cuprum-rust/src/fd_ownership_kani_proofs.rs` carries a bounded Kani proof
of the ownership invariant itself: a borrowed reader FD is never closed by Rust
on a normal or an unwinding exit, and a `pump_stream` writer is always consumed
and closes exactly once to signal EOF.

Kani does not interpret I/O, so the proof cannot use real descriptors. It runs
against the pure model in `fd_ownership_model.rs` — gated
`#[cfg(any(test, kani))]`, so `make test` exercises it as ordinary unit tests
too — where `ModelFd` records a close on drop instead of issuing one. The
`ManuallyDrop` wrapper, the closure call, and the early-exit edge are all real
Rust, so Rust's own drop elaboration decides the outcome rather than any
hand-written accounting. Because Kani compiles panics as aborts, the unwind
path is modelled with `?`: a `?` early return and a real unwind both leave the
frame without running the statements that follow the operation, so
reintroducing the superseded trailing-`mem::forget` makes the proof fail (that
mutation was run to confirm the proof is not vacuous). Being a bounded model
checker, Kani establishes this over an explicitly bounded state space — the two
exit modes, and at most three repeated borrows — rather than for all
executions. Active verification tracking, including whether Verus adds anything
beyond the Kani model once that model is complete, lives in issue `#89`.

### Python-side native pump descriptor lifetime

`_run_rust_pump` keeps the asyncio transport's writer descriptor in Python's
ownership. It gives `rust_pump_stream` a duplicate instead, because the native
pump consumes and closes the descriptor it receives. Python retains ownership
of the duplicate through blocking-mode setup and executor submission. If either
step fails, Python closes it. Once submission succeeds, the `_streams_rs` shim
owns the hand-off: it closes the duplicate if native loading or platform
preparation fails, otherwise it transfers an independently owned resource to
Rust, which closes that resource after the native call. On Windows, the shim
transfers a duplicated Win32 handle and closes the duplicate CRT descriptor
before invoking Rust. The native-future completion callback must not close the
writer resource. asyncio keeps and closes the original transport descriptor
after the worker settles. No descriptor number may be closed by both owners.
The reader transport remains paused, and the original descriptor modes are
restored, until that same completion boundary. This prevents cancellation
cleanup from racing with native I/O on a descriptor that is still in use.

Executor-side failures while creating the duplicate or submitting the executor
work are re-raised after rollback and recorded at `DEBUG` on the
`cuprum._pipeline_streams` logger. Shim-side failures while preparing the
reader or transferring the platform writer are recorded at `DEBUG` on the
`cuprum._streams_rs` logger. These records use
`cuprum_action="rust_pump_handoff_failed"`, a fixed hand-off phase, the
exception class, and `errno` when available; they contain no descriptor number
or exception text. Duplicate-creation failure emits `duplicate_writer_failed`,
and reader-preparation failure emits `reader_preparation_failed`. Executor
rejection emits `executor_submission_rejected` before it is re-raised.
Blocking-mode failure selects the Python fallback and emits
`blocking_setup_failed`. The outcome events are counted by
`cuprum_rust_pump_handoff_total` as described above.

## Rust splice-loop and drain contract

The Linux zero-copy path in `rust/cuprum-rust/src/splice.rs` follows one
canonical loop. `try_splice_pump` performs the first `splice_once` solely to
detect support: `EINVAL` on that first call signals unsupported descriptor
types and the read/write fallback. Every outcome thereafter — including the
first call's, which is fed into the loop — is handled by the same arms: `Ok(0)`
ends the transfer, `Ok(n)` accumulates, a non-fatal write error (broken pipe /
connection reset) drains the reader and reports the bytes transferred so far,
and anything else propagates.

`splice_once` retries at the syscall level: an interrupted splice (`EINTR`) is
re-issued rather than surfaced, matching the Unix read and write policies, so a
signal delivered mid-transfer does not spuriously fail an otherwise-healthy
pump. The retry loop is factored into `splice_once_with`, which a regression
test drives with an injected `EINTR`, mirroring the `read_raw_fd` coverage, and
a proptest exercises the retry across any number of interruptions and either
terminal outcome. The accumulation loop is factored the same way into
`accumulate_splices`, whose `next` (splice) and `drain` side effects are
injected so a proptest can assert the `Ok(0)` / `Ok(n)` / broken-pipe-drain /
fatal transitions over generated outcome sequences. Exhaustive bounded
model-checking of these paths with Kani remains tracked in issues `#87` and
`#88`.

`drain_reader` routes through the canonical raw-fd read helper
(`io_utils::read_raw_fd`), so it shares the Unix read policy with the
read/write fallback: interrupted reads (`EINTR`) retry instead of silently
ending the drain, end of file terminates it, and other errors propagate.
Behavioural tests in the module cover full pipe-to-pipe transfer, the fallback
signal for regular files, broken-pipe draining, the drain's EOF termination,
and the syscall-level `EINTR` retry.

The read/write fallback's write half routes through `io_utils::write_all_unix`,
whose partial-write loop is factored into `write_all_unix_with` — the
injectable write seam mirroring `read_raw_fd_with`. Unit tests drive it over a
scripted single-write operation so the write policy is exercised
deterministically without real descriptors: interrupted writes (`EINTR`) retry,
zero progress raises `WriteZero`, partial writes accumulate, over-long progress
surfaces `BufferRangeExceeded`, non-fatal short writes (broken pipe /
connection reset) report the bytes transferred so far, and other errors
propagate. Proptests confirm `PumpError::is_nonfatal_write` and
`map_short_write_error` suppress exactly `BrokenPipe`/`ConnectionReset` while
preserving the accepted byte total. These raw-fd helpers live in the `io_utils`
directory module (`io_utils/mod.rs`, with tests in `io_utils/tests.rs`), split
from the former single file to stay within the per-module line cap.

The read/write fallback's control flow — how each read and write outcome moves
the running byte total and the latched `writer_open` flag — is factored into a
pure, `io::Error`-free state machine in `src/pump_machine.rs`.

[Cuprum design](cuprum-design.md) §13.7, "Read/write fallback state machine",
is the normative reference for this API: it carries `advance`'s full signature
and the reasoning behind its shape. The notes below record the conventions that
govern using it, and name the types so a reader arriving from the `io_utils`
sections above knows what to look for.

`advance` is the machine's only production entry point. It takes a `PumpState`
by mutable reference, the raw read length, and a closure that performs one
write, and it returns a `Flow` — `Continue` to keep reading, or `Stop` on end
of input. `PumpState` holds exactly the two values the loop must not get wrong,
the running `total_written` total and the latched `writer_open` flag, exposed
through `total_written()` and `writer_open()` accessors rather than as public
fields, so only the machine may move them.

`advance` owns both decisions the loop would otherwise have to make for itself:
a zero-length read is end of input, anything else is a chunk; and the write
runs **only** for a chunk read while the writer is still open.
`pump_stream_files_readwrite`, the property tests, and the bounded proofs all
go through it, so there is one definition of that policy rather than three
copies that could drift.

The write closure yields a `WriteEvent`, the machine's `io::Error`-free view of
one write: `Complete { bytes }` accepted the whole chunk and leaves the writer
open, while `Closed { bytes }` carries the bytes accepted before a broken pipe
or connection reset and latches the writer shut for good. The sole production
adapter that builds one is `io_utils::classify_write`, which collapses a
`WriteOutcome` and the non-fatal error partition into those two variants; the
test-only `classify_write_with` shares its mapping through a `write(2)` seam.
Genuinely fatal failures never reach the machine at all — they propagate as
`PumpError` — which is what keeps `pump_machine` free of `io::Error` and so
tractable for Kani. Re-use policy: a new caller of the machine must classify
its writes through `classify_write` rather than constructing a `WriteEvent`
inline, otherwise "non-fatal" stops having one definition.

The precondition is enforced by the type rather than by a check. Internally
`advance` builds a private `Transition` — `Wrote(WriteEvent)`, `Drained`, or
`Eof` — and `Wrote` is constructible only on the branch that has already
established the writer is open. An earlier API took a read event and an
`Option<WriteEvent>` independently, which let a caller pass a write for an EOF
read or for a closed writer; both were silently ignored, so the precondition
lived in a doc comment. Making those states unrepresentable also removed the
defensive re-check inside `step`, which had been masking exactly the mistake it
looked like it was guarding against.

That encapsulation is what the proptests and Kani proofs assume rather than
establish — neither can observe a transition it cannot spell — so it is pinned
separately by the compile-fail case
`rust/cuprum-rust/tests/ui/fail/pump_transition_unreachable.rs`. That case
includes `src/pump_machine.rs` as a child module, reproducing the relationship
`lib.rs` has with it, and asserts the compiler's refusal of an attempt to build
a `Transition::Wrote` and pass it to `step` from the parent. Widening either
item, even to `pub(crate)`, changes the diagnostic and fails the case, so the
"`advance` is the only constructor" claim cannot quietly stop being true.

Whether the write *ran* is what the tests assert, not merely its effect: a
dropped precondition would leave the resulting state identical and differ only
in having performed a spurious write, so the drivers report invocation.

Extracting the decision this way lets it be checked without descriptors:
proptests fold random scripts of `(read_len, WriteEvent)` through `advance` to
assert the total is monotonic, the writer never reopens once a broken pipe
latches it closed, a closed writer drains without accruing bytes, the loop
stops exactly on a zero-length read, and the write runs exactly when the
transition permits it. Kani proves the same invariants over unbounded byte
counts and arbitrary starting states in `src/pump_machine_kani_proofs.rs`
(`#[cfg(kani)]`, so outside the commit gate), closing the model-checking
follow-up from issue `#84`. Those proofs target `advance` rather than the
private `step`, because `advance` is where the precondition lives — proving
`step` in isolation would establish nothing about a closed writer.

Neither the proptests nor the proofs call `advance` directly. Both go through
`drive`, a `#[cfg(any(test, kani))]` helper beside it that supplies the write
closure and returns the `Flow` alongside whether the closure was invoked. That
is deliberate: it keeps the two harnesses driving the machine identically, and
it is where the "was the write attempted?" observable comes from. Add new
harnesses through `drive` rather than reconstructing the closure in each one.

The path emits bounded `tracing` diagnostics at the three boundaries operators
need visibility into: support detection logs a `debug` event when the
descriptors cannot splice and the read/write fallback takes over; the
`splice_once` `EINTR` retry logs a `trace` event per re-issue (the lowest
level, filtered out by default, so signal-heavy workloads stay quiet unless
trace is enabled); and the broken-pipe drain logs a `debug` event carrying the
`bytes_transferred` so far. Messages are static and low-cardinality. Following
the library convention, the crate emits instrumentation but installs no
subscriber — the embedding application (the Python command boundary) owns
subscriber configuration and higher-level command observation, and `PumpError`
values with transfer counts are still returned across that boundary as the
primary signal.

The read/write fallback loop is instrumented to the same standard. Each seam
emits a `debug` event per successful read or write (carrying the byte count and
platform), a `warn` event on every `EINTR` retry, and an `error` event on a
fatal read/write failure, a zero-progress write, or a length-conversion
overflow — so no fatal boundary stays silent. The `pump_stream_readwrite` and
`consume_stream` loops wrap these seams in an operation span
(`io_utils::operation_span`) that carries the `operation` and `buffer_size`
and, on completion, records `total_bytes` and the cumulative `EINTR` retry
counts as structured fields. `pump_stream_readwrite` reads and writes, so it
records both `read_retries` and `write_retries`; `consume_stream` only reads,
so it records `read_retries` alone (`write_retries` stays unset). The retry
counts are accumulated in operation-scoped thread-local counters that
`pump_stream_readwrite` and `consume_stream` explicitly reset at operation
start (right after entering the span, via `io_utils::reset_retry_counters`;
`operation_span` itself does not touch the counters), so the seams stay
parameter-free while the span still reports them.

One event in that loop comes from the pump's own state rather than a seam. When
the writer-close latch first closes — the downstream stage hung up and the
remaining reads only drain the upstream, the `head`-style early exit — the loop
emits a `debug` event carrying `bytes_transferred`, using the same field and
message as the splice path's broken-pipe report so a hang-up looks identical
whichever path handled it. It is observed in the loop rather than in
`pump_machine`, which stays free of I/O and logging so its bounded proofs stay
tractable.

The span is created at `error` level so the `warn`/`error` events keep their
operation context even under a `warn`/`error`-only production filter, where an
`info` span would be disabled; it emits no log line of its own.

Unix Rust tests share pipe creation, duplicated-file wrapping, result helpers,
and descriptor-state checks through `test_support`. Re-use that module for
descriptor-backed test setup; keep production code independent of test helpers.
The splice behavioural tests expose that shared `make_pipe` as an rstest `pipe`
fixture, so scenarios needing several independent pipes inject it once per
`#[from(pipe)]` parameter.

## Rust property testing and verification

Rust-level tests for `cuprum-rust` live with the crate under
`rust/cuprum-rust/src/`. Use them for pure decoder, parsing, state-machine, and
adapter logic where Python integration tests would only cover a few examples.

Property tests use [proptest](https://docs.rs/proptest/latest/proptest/) as a
development dependency. Prefer generated payloads and small helper functions
that expose pure behaviour. The UTF-8 decoder tests generate arbitrary byte
vectors and chunk split points, then compare the decoded output with
`String::from_utf8_lossy` as the oracle.

When a property fails, proptest writes the shrunk case to a seed file under
`rust/cuprum-rust/proptest-regressions/`. Commit that file: it re-runs the
failing case ahead of the generated ones, so the same regression cannot return
unnoticed.

Seeds are only worth keeping when they came from a genuine failure. Running the
Rust suite against deliberately-broken code — to prove a property is not
vacuous — also writes a seed, and that one pins a case that never failed
against correct code. Disable persistence for those runs so the file is never
created:

```bash
PROPTEST_DISABLE_FAILURE_PERSISTENCE=1 make test
```

If a seed file does appear after a mutation run, delete it rather than
committing it. Checking it in would imply a history the code does not have.

Snapshot tests use [insta](https://docs.rs/insta/latest/insta/) as a
development dependency, declared with `default-features = false` so the crate
pulls in none of insta's optional format integrations. Prefer insta where the
valuable assertion is the *exact* output text rather than a property of it —
`consume_snapshot_tests.rs` pins the UTF-8 replacement output of the
`consume_stream_files` read loop this way, so a regression in the loop, the
bounds-checked slicing, or the `final_chunk` handling surfaces as a concrete
text diff rather than a boolean failure.

These Rust-side cases are not the only coverage of these categories, and it is
worth knowing why they carry the load. `TestRustConsumeStream` in
`cuprum/unittests/test_rust_consume_stream.py` already defines Python/Rust
boundary tests over the same four inputs — ASCII, multibyte UTF-8 split across
a read boundary, invalid UTF-8, and an incomplete trailing sequence — each case
calls `rust_consume_stream` and compares the result against Python's own
replacement decoding, `payload.decode("utf-8", errors="replace")`.

Those cases do now execute in CI — see
[Building the extension for tests](#building-the-extension-for-tests) — but
they did not until `#258` was resolved, because `make build` only runs
`uv sync --group dev` and never compiled `cuprum._rust_backend_native`. Running
them also surfaced `#265`, where an `OSError` crossing the boundary lost its
`errno`; that is fixed too, under
[Preserving the operating-system error code](#preserving-the-operating-system-error-code).

The two layers verify different things and neither replaces the other: the
snapshots and properties cover the `consume_stream_files` read-and-decode loop,
while `TestRustConsumeStream` covers the exported surface a caller actually
touches. Keep both when changing either.

Those snapshots are written inline with
`insta::assert_snapshot!(value, @"...")` rather than as separate `.snap` files,
which keeps the expected text beside the case that produces it and leaves no
snapshot files to review or prune. Accept a deliberate change by editing the
inline literal; `cargo insta` is not required for the inline form.

### Building the extension for tests

Several test modules exercise the compiled PyO3 extension and are gated on it
being importable. `make build` does not build it — that target only syncs
dependencies — so build it explicitly:

```bash
make develop
```

That runs `maturin develop` against `rust/cuprum-rust/Cargo.toml` in the
project virtual environment, preceded by `ensurepip` because maturin resolves
its own script through the interpreter's `sysconfig` scheme, which needs pip
present in the environment. CI's `extension-tests` job runs the same target, so
a local run and a CI run build the extension identically. The Makefile keeps
only a pointer to this section rather than repeating the reasoning.

Every CI job that installs the extension and then runs against it goes through
this target — `extension-tests` and `benchmark-ratchet`, and no others. The
wheel build is not one of them: `.github/workflows/build-wheels.yml` runs
`maturin build` to produce a distributable artefact rather than installing it
into a virtual environment, so it neither uses nor needs this target.

The `benchmark-ratchet` job needs an optimized, in-place build of the mixed
Python/Rust project. It passes
`make develop MATURIN_DEVELOP_FLAGS='--release --skip-install'` rather than
restating the three-step sequence. `--skip-install` avoids Maturin's
unnecessary editable dependency install while still building the extension in
place. Keep it that way: a second copy of the sequence is how the two drift,
and the ratchet then measures a build nobody maintains. `MATURIN_DEVELOP_FLAGS`
is empty by default, because a debug build is what contributors and the
`extension-tests` job want.

Without it these modules skip rather than fail, which is the right default
locally — most changes do not need the native path rebuilt — and the wrong one
in CI, where a job that never built the extension reports a green run
indistinguishable from one that exercised the whole boundary.
`make test-extension` sets `CUPRUM_REQUIRE_RUST_EXTENSION=1` to make that
silence fatal:

```bash
make develop
make test-extension
```

Run that rather than `CUPRUM_REQUIRE_RUST_EXTENSION=1 make test`. Once the
extension is installed the full suite aborts the interpreter — see the `#124`
constraint below — so `make test-extension` runs only the gated modules. Their
list lives in one place, the Makefile's `EXTENSION_TEST_TARGETS`, which the CI
job consumes too, so the two cannot drift apart.

None of that wiring is exercised by ordinary tests — drop the guard variable
and the suite still passes — so two contract modules read it back, split by
what they read rather than by what they assert.

`test_extension_build_contract.py` covers the Makefile: that the recipe sets
the guard variable, and what `EXTENSION_TEST_TARGETS` must contain. It reads
the Makefile with `make --dry-run` rather than by parsing the file, because the
expanded recipe is the command line CI actually runs. It scrubs the Makefile's
`?=` variables from that nested `make`'s environment first. `make` exports each
of its command-line overrides under its own name, and a `?=` assignment yields
to a name already in the environment, so without the scrub a run of
`make test EXTENSION_TEST_TARGETS=…` would have the contract report on whoever
invoked it rather than on the repository — passing or failing according to the
command line rather than the wiring. Stripping `MAKEFLAGS` alone does not
prevent that, because the override travels under its own name as well.

`test_extension_ci_contract.py` covers `ci.yml`: that the `extension-tests` job
runs `make develop` before `make test-extension`, that `benchmark-ratchet`
builds through the same target with `--release`, and that no job reintroduces a
second copy of the build sequence.

It reads the workflow through `tests/helpers/workflow.py`, which is the one
place `ci.yml` is parsed. That helper declares the shapes it reads — jobs,
steps, and a step's `run:` — so a misspelled key is a type error rather than a
`None` that quietly satisfies the assertion above it. Keep it the only parser:
a second one drifts from the first, and then two suites disagree about what the
same file says.

### Shared workflow test support

The session-scoped `workflow_data` fixture parses the checked-in
`.github/workflows/ci.yml` once. `filter_path_patterns` derives the
performance-relevant paths from that same model. The workflow contract tests
consume the parsed model directly, while the behavioural tests use both
fixtures to exercise the gate and its summary against the checked-in
configuration.

The support is split by responsibility: `workflow_types.py` defines the narrow
`TypedDict` shapes; `workflow.py` parses the workflow and provides queries over
its jobs and steps; `workflow_gate.py` contains the pure path matching and
benchmark-admission model; and `workflow_shell.py` recognizes commands in
`run:` scripts while ignoring comments and here-document bodies. Keep
repository access in the fixtures and use these helpers rather than creating
another workflow parser in a test.

`EXTENSION_TEST_TARGETS` gets two separate checks, because neither implies the
other:

- A scan derives the extension-gated modules from the suite itself — those
  requesting the root `rust_streams` fixture, skipping with the shared "Rust
  extension is not installed" reason, or naming `cuprum._rust_backend_native` —
  and requires each one to be a declared target. This is the check that notices
  a *new* gated module being forgotten, which a hard-coded copy of today's list
  never would. The scan is textual and deliberately narrow, so it is a lower
  bound: a module gating through some other idiom goes unnoticed. A companion
  check fails when a signal stops matching anything, so a renamed fixture
  cannot quietly empty the scan instead of failing it.
- Modules that do not gate at all, but belong in the job anyway, are named
  explicitly alongside the reason. `test_extension_requirement_guard.py` is
  one: running it inside the guarded job is what proves the guard stays silent
  when the extension is present, rather than only that it fires when the
  extension is absent.

So add a newly gated module to `EXTENSION_TEST_TARGETS`. If it is boundary
coverage that never skips, add it to the companion list with its reason instead.

Running `make test-extension` without having built the extension is safe: the
guard fails the run with a message naming `make develop`, which is the whole
point of it.

The check runs once per session in `conftest.py`, so it covers every gated
module regardless of how each one gates — fixture, module-level guard, or
availability probe — and a new module cannot opt out by skipping differently.
The decision itself lives in `tests/helpers/extension_requirement.py` rather
than in the root `conftest.py`, because a root conftest is shadowed by the
per-package one and so cannot be imported by name from a test module;
`test_extension_requirement_guard.py` covers it, and reaches the hook through
the plugin manager to check that it actually raises.

CI runs these in a dedicated `extension-tests` job rather than folding the
extension into `typecheck-test`. That is a deliberate constraint, not tidiness:
with the extension present, `test_pipeline.py` trips the file-descriptor close
race tracked by issue `#124` (roadmap 8.1.1) and aborts the interpreter part
way through the suite, reproducibly. Until that is fixed, the extension must
not be installed for the general test run. Widening the job is the natural
follow-up once `#124` lands.

Table 1: modules gated on the compiled extension

| Module                                             | Covers                                                                                                                                                               |
| -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_rust_streams.py`                             | the Rust-backed pump entry point                                                                                                                                     |
| `test_rust_consume_stream.py`                      | the Rust-backed consume entry point, including the four replacement scenarios that are the end-to-end regression coverage for `#105` and the I/O-error boundary case |
| `test_rust_streams_boundary_property.py`           | randomized payloads across the boundary                                                                                                                              |
| `test_rust_extension.py`                           | extension availability and module surface                                                                                                                            |
| `test_rust_splice.py`                              | the Linux `splice` fast path                                                                                                                                         |
| `test_rust_errno.py`                               | POSIX `OSError.errno` conversion and subclass selection across the boundary                                                                                          |
| `test_rust_errno_windows.py`                       | Windows `winerror` conversion and the `errno` and subclass values CPython derives from it                                                                            |
| `test_backend.py`                                  | the extension-dependent backend-selection cases                                                                                                                      |
| `test_extension_requirement_guard.py`              | the fail-loud guard itself                                                                                                                                           |
| `tests/behaviour/test_rust_streams_behaviour.py`   | the consumer-facing pump and consume scenarios                                                                                                                       |
| `tests/behaviour/test_rust_extension_behaviour.py` | availability agreeing with the installed native module                                                                                                               |
| `tests/behaviour/test_stream_backend_pipeline.py`  | pipelines dispatched through the Rust backend                                                                                                                        |

The behavioural modules are listed for the same reason as the unit ones: their
extension-dependent scenarios skip in the ordinary test jobs, so they were
never boundary coverage there either. Confirm with `pytest -rs` against a
virtual environment that has no extension — four scenarios report
`Rust extension is not installed`.

Kani harnesses are reserved for bounded verification of small, high-value state
spaces. Gate Kani-only modules and helpers with `#[cfg(kani)]`, and share pure
test helpers behind `#[cfg(any(test, kani))]` when both proptest and Kani need
the same simulation path. Register new custom cfg names in the workspace lint
configuration so `unexpected_cfgs` warnings remain meaningful.

Run the normal project test gate from the repository root:

```bash
make test
```

`make test` runs the Python pytest batches before the crate tests through
`cargo nextest`, including proptest cases compiled under `#[cfg(test)]`. Run
the complete Rust lint and formatting gates before committing Rust changes:

```bash
make check-fmt
make lint
```

Run Kani separately because it is a bounded model checker rather than a normal
unit-test runner. The Kani installer places the verifier under `~/.kani`; the
dynamic library path is required when invoking the crate harnesses. Resolve the
toolchain library directory from the installed version rather than hard-coding
it:

```bash
KANI_VERSION=$(cargo kani --version | awk '{print $2}')
cd rust && \
  LD_LIBRARY_PATH="$HOME/.kani/kani-${KANI_VERSION}/toolchain/lib" \
  cargo kani --package cuprum-rust
```

When adding new Kani proofs, keep the bounds explicit with attributes such as
`#[kani::unwind(N)]`, include `kani::cover!` statements for the intended
boundary cases, and avoid broad symbolic comparisons that force Kani through
large allocation-heavy standard-library internals unless the proof genuinely
requires that surface.

## Python linting

Cuprum uses a five-stage Python lint gate. Ruff is the first stage and remains
the fast, broad lint pass for formatting-adjacent checks, import order,
docstring *style*, security checks, naming, complexity, and Ruff's native
Pylint-derived rules. `interrogate` is the second stage and enforces docstring
*presence* at 100 per cent across the `cuprum` package. Built-in Pylint checks
run third through the `leynos/pylint-pypy-shim` package under PyPy. The pinned
`df12-python-lints` plugin runs fourth under CPython 3.14, and `ambrleaks`
scans Syrupy snapshots fifth under the same interpreter.

The decisions are recorded in
[ADR-003: Two-tier Python linting](adr-003-two-tier-python-linting.md) and
[ADR-004: Interrogate docstring-coverage gate](adr-004-interrogate-docstring-gate.md).
The short version is:

- Ruff owns fast feedback and the primary rule set, including docstring style.
- `interrogate` owns docstring coverage: it fails the gate when any
  documentable node — including nested closures, dunder methods, properties,
  and stub classes — lacks a docstring that Ruff's `D` rules do not require.
- Pylint owns selected checks that Ruff does not cover, especially logging
  interpolation, pattern matching, generator control flow, environment
  handling, subprocess safety, and selected readability checks.
- Pylint runs through the PyPy shim so that the third tier is isolated from the
  project virtual environment and matches the lint approach used by
  `leynos/episodic`.
- `$(PYLINT)` pins Pylint itself with
  `--with 'pylint==$(PYLINT_VERSION)'` because the shim revision and Pylint
  package version are separate sources of lint behaviour.
- `$(DF12_PYLINT)` enables every message shipped by
  `df12-python-lints` v0.3.0 under CPython 3.14 while retaining Cuprum's
  `py-version = "3.12"` semantic baseline.
- `$(AMBRLEAKS)` scans `cuprum/unittests` and `tests`; exact deterministic
  fixture values that resemble secrets belong in `ambrleaks.toml`.

### Markdown formatting

`make fmt` runs `mdformat-all`, which applies `mdtablefix` with
`--wrap --renumber --breaks --ellipsis --fences --in-place` before applying
`markdownlint-cli2 --fix`. `mdtablefix` therefore owns table padding and
paragraph wrapping, while `make markdownlint` verifies the result.

`make check-fmt` passes repository Markdown files to
`scripts/check-markdown-format.sh`. Because `mdtablefix` has no check-only
mode, the checker formats temporary copies and compares them with the source
files; it never modifies the worktree. It accepts exact LF or CRLF output, but
rejects mixed line endings. Run `make test-markdown-format` after changing the
checker.

The gate installs the `mdtablefix` version pinned by `MDTABLEFIX_VERSION` in
`.github/workflows/ci.yml` through the
`leynos/shared-actions/.github/actions/install-mdtablefix` action. The action
requires `mdtablefix` 0.5.1 or later, installs only a matching prebuilt
release, and fails closed when the runner has no supported archive; it never
builds the formatter from source. Cuprum's project toolchain remains Rust
1.85.0.

Install the pinned prebuilt version locally with `cargo-binstall` so formatter
output matches CI:

```bash
MDTABLEFIX_VERSION=0.5.1
cargo binstall --no-confirm --locked --disable-strategies compile \
  --install-path "$HOME/.local/bin" "mdtablefix@${MDTABLEFIX_VERSION}"
```

### Docstring structure

Public functions, classes, and methods require comprehensive NumPy-style
docstrings, with examples where appropriate. Prefer single-line docstrings for
private helpers, and use structured NumPy-style sections only to describe
non-obvious behaviour.

When a private helper needs an explanatory paragraph, first inspect whether it
combines query and command responsibilities or unrelated concerns. Split or
extract a focused helper when doing so makes the boundary or invariant local
and simpler. Retain the paragraph when the helper remains cohesive and the
explanation records an unavoidable local constraint.

Run the complete lint gate with:

```bash
make lint
```

`make lint` performs the following commands in order:

1. `$(RUFF) check`
2. `$(UV_RUN_ENV) uv run interrogate --fail-under 100 cuprum`
3. The PyPy-backed `pylint-pypy` command stored in `$(PYLINT)`, with
   `$(PYLINT_TARGETS)` appended.
4. The CPython 3.14 `df12-python-lints` pass stored in `$(DF12_PYLINT)`, over
   the same targets.
5. The CPython 3.14 `ambrleaks` scanner over both Syrupy snapshot roots.

Each stage must pass before the next runs. When investigating a lint failure,
fix findings in execution order, then rerun `make lint` to reach the next
stage. Do not disable df12 messages to absorb existing findings; repair the
assertion, alias, suppression rationale, or dispatch structure instead.

### Spelling policy

The lint and Markdown gates run pinned `typos` 1.48.0 with British English and
Oxford `-ize` conventions. The single spelling recipe checks tracked Markdown,
Python, and Rust files, so the policy governs code identifiers, comments,
docstrings, string fixtures, and prose. Only spellings required by external
contracts or deliberate spelling-test fixtures are exempt.

Before checking the repository, the generator refreshes the shared/base
en-GB-oxendict dictionary into an untracked local cache only when the authority
is newer, then merges `typos.local.toml`. The generated `typos.toml` is
reviewed and committed so a clean, network-restricted checkout can still
enforce the last known-good policy.

Put an unavoidable external-contract spelling or a deliberate spelling-test
fixture in `typos.local.toml` as a narrowly anchored `[patterns].ignore` entry.
Document the specific upstream contract or fixture beside the entry. Do not
accept a globally incorrect Oxford form under `[words].accepted`, and never
edit generated entries in `typos.toml` by hand. Regenerate after changing the
overlay:

```bash
uv run scripts/generate_typos_config.py
```

Run `make spelling` to verify the generated configuration and all three file
types. The gate also runs the helper's Python 3.13 tests with at least 90% line
coverage.

The cache refresh in `scripts/typos_rollout_refresh.py` fetches the shared
dictionary only over HTTPS and delegates redirect enforcement to
`scripts/typos_rollout_degradation.py`. Its dedicated
`_HttpsOnlyRedirectHandler` refuses any redirect that would downgrade the
connection to plain HTTP before urllib reissues the request, so a compromised
or misconfigured upstream cannot silently serve the dictionary in cleartext.
Refresh degradations — a rejected HTTPS-downgrade redirect, falling back to a
stale cache after a failed refresh, or reusing the cache in offline mode — are
counted by the bounded, fixed-key counters in
`scripts/typos_rollout_degradation.py` and reported through structured
`logging` warnings (or info, for the offline case). Those log records never
include the request URL; they carry only the event name and non-sensitive
context such as the rejected redirect's scheme or the triggering error's type.

Ruff and ty are invoked through pinned `uv tool run` commands rather than
floating host tools. `RUFF` expands to
`$(RUFF_ENV) $(UV_RUN_ENV) uv tool run --from 'ruff==$(RUFF_VERSION)' ruff`, and
`TY` expands to `$(UV_RUN_ENV) uv tool run --from 'ty==$(TY_VERSION)' ty`. The
Makefile defaults are `RUFF_VERSION ?= 0.16.4` and `TY_VERSION ?= 0.0.74`; the
workflow-level `RUFF_VERSION: '0.16.4'` and `TY_VERSION: '0.0.74'` environment
values in `.github/workflows/ci.yml` override those defaults, and the
pin-parity contract test keeps all three sites aligned with the
`pyproject.toml` dev dependencies. The `typecheck` recipe passes
`--python .venv` so the tool-run ty process checks the project environment.
`interrogate` remains invoked via `uv run` in the `lint` recipe.

Because `interrogate` requires a docstring on every documentable node,
documenting a large module can take it over the project's 400-line ceiling
enforced by Pylint's `too-many-lines`. Split the module by feature rather than
suppressing the limit; this is why the pipeline dataclasses live in
`cuprum/_pipeline_types.py` (re-exported from `cuprum/_pipeline_internals.py`)
rather than inline.

### Lint Makefile variables

The root `Makefile` exposes the following lint-related variables:

<!-- markdownlint-disable MD013 -->

Table: Lint-related Makefile variables and their defaults.

| Variable                | Default                                                                      | Purpose                                                                                                                     |
| ----------------------- | ---------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `VENV_TOOLS`            | `pytest ruff`                                                                | Tools checked in the project virtualenv; Ruff uses its pinned command.                                                      |
| `RUFF_VERSION`          | `0.16.4`                                                                     | Ruff release supplied to `uv tool run --from`.                                                                              |
| `RUFF_ENV`              | `RAYON_NUM_THREADS=1`                                                        | Keeps Ruff parallelism deterministic for the lint and format gates.                                                         |
| `RUFF`                  | `$(RUFF_ENV) $(UV_RUN_ENV) uv tool run --from 'ruff==$(RUFF_VERSION)' ruff`  | Pinned Ruff command used by `fmt`, `check-fmt`, and `lint`.                                                                 |
| `TY_VERSION`            | `0.0.74`                                                                     | ty release supplied to `uv tool run --from`.                                                                                |
| `TY`                    | `$(UV_RUN_ENV) uv tool run --from 'ty==$(TY_VERSION)' ty`                    | Pinned ty command used by `typecheck`.                                                                                      |
| `PYLINT_PYTHON`         | `pypy`                                                                       | Python interpreter requested by `uv tool run` for the Pylint tier.                                                          |
| `PYLINT_TARGETS`        | `benchmarks conftest.py cuprum tests`                                        | Directories and files passed to `pylint-pypy`.                                                                              |
| `PYLINT_PYPY_SHIM_REF`  | `726d09f968b4d729ee4b29c71fc732e744854f3b`                                   | Pinned revision of `leynos/pylint-pypy-shim`.                                                                               |
| `PYLINT_PYPY_SHIM`      | `git+https://github.com/leynos/pylint-pypy-shim.git@$(PYLINT_PYPY_SHIM_REF)` | Install source used by `uv tool run`.                                                                                       |
| `PYLINT_VERSION`        | `4.0.7`                                                                      | Pylint package version supplied to `uv tool run` through `--with`.                                                          |
| `PYLINT_CACHE`          | `.cache/pylint`                                                              | Worktree-local cache shared by both Pylint passes.                                                                          |
| `PYLINT`                | Derived command                                                              | Full PyPy-backed Pylint command used by `make lint`.                                                                        |
| `DF12_PYTHON_LINTS_REF` | `v0.3.0`                                                                     | Controlled release tag selected for DF12 lint tooling.                                                                      |
| `DF12_PYTHON`           | `3.14`                                                                       | CPython runtime used for df12 Pylint and `ambrleaks`.                                                                       |
| `DF12_PYLINT_MESSAGES`  | All v0.3.0 message IDs, including `R9112`                                    | Explicit allowlist for the df12 Pylint pass.                                                                                |
| `DF12_PYLINT`           | Derived command                                                              | CPython 3.14 Pylint command loading `df12_python_lints`.                                                                    |
| `AMBRLEAKS`             | Derived command                                                              | Lock-backed snapshot-scanner command used by `make lint`.                                                                   |
| `LOCAL_TOOL_ENV`        | Derived `PATH`                                                               | Adds local binary directories before invoking host and `uv`-managed tools.                                                  |
| `UV_ENV`                | `UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools`                               | Keeps `uv` cache and tool installs local to the worktree.                                                                   |
| `UV_RUN_ENV`            | `$(LOCAL_TOOL_ENV) $(UV_ENV)`                                                | Shared environment prefix for locked `uv run` commands and the pinned `uv tool run` commands used by `$(RUFF)` and `$(TY)`. |

<!-- markdownlint-enable MD013 -->

Override these variables only for local diagnosis. For example, to lint a
single module with the configured second tier:

```bash
PYLINT_TARGETS=cuprum/sh.py make lint
```

Do not change `PYLINT_PYPY_SHIM_REF` casually. Updating the pinned shim
revision changes the lint runtime and must be reviewed like any other toolchain
update. Update the `df12-python-lints` development dependency and
`DF12_PYTHON_LINTS_REF` together so the Pylint plugin and standalone scanner
select the same controlled release tag. When adopting a new release, update
both references to that tag.

### Episodic lint policy

Cuprum imports the lint policy used by `leynos/episodic` rather than inventing
a separate house style. The imported policy consists of:

- Ruff `target-version = "py312"` for Cuprum's supported Python baseline.
- Ruff banned `typing.*` generic aliases, requiring modern built-in generics or
  `collections.abc` and `contextlib` equivalents.
- Test-file exceptions for assertion-heavy tests and pytest method conventions.
- A focused Pylint configuration that disables all messages by default, then
  enables only the selected messages that complement Ruff.
- A PyPy-backed Pylint invocation through the pinned shim repository.
- Every `df12-python-lints` v0.3.0 message, including `R9112`, executed under
  CPython 3.14.
- `ambrleaks` coverage for both in-package and behavioural Syrupy snapshots.

This means new code should prefer:

```python
from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def names(values: cabc.Iterable[str]) -> list[str]:
    return list(values)
```

Use `typing as typ` for `TYPE_CHECKING`, casts, aliases, and other `typing`
helpers that are not banned. Use `collections.abc` imports inside
`typ.TYPE_CHECKING` when annotations are deferred and the names are only needed
for type checking.

### `pyproject.toml` lint configuration

The canonical lint configuration lives in `pyproject.toml`:

- `[dependency-groups] dev` pins `ruff==0.16.4`. The pin exists so that Ruff's
  version — and therefore its rule set and preview-rule behaviour — is
  reproducible between developer machines and CI; an unpinned Ruff could
  silently gain or lose findings when a new release ships. The same version is
  pinned as `RUFF_VERSION` in the Makefile and in `.github/workflows/ci.yml`;
  the pin-parity contract test in `cuprum/unittests/test_toolchain_pins.py`
  keeps the three sites aligned (alongside the matching `ty` pins) without
  asserting any specific version.
- `[tool.ruff]` sets line length, preview mode, and target Python version.
- `[tool.ruff.lint]` selects the active Ruff rule families; the selection
  mirrors the `episodic` repository's configuration, including `TD` (require
  authors and issue links on TODO comments).
- `[tool.ruff.lint.per-file-ignores]` records test-specific exceptions,
  including:
  - `boolean-type-hint-positional-argument` is ignored for `**/test_*.py`
    because parameters bound by `@pytest.mark.parametrize` or Hypothesis
    `@given` are data rows, not API flags, and pytest's signature-based
    binding rules out keyword-only parameters.
  - `scripts/tests/conftest.py` carries `assert` and `no-self-use`
    exemptions because it is test-support code outside the test globs,
    whose stub opener must keep the `OpenerDirector` instance-method
    shape.
- `[tool.ruff.lint.flake8-import-conventions]` and
  `[tool.ruff.lint.flake8-import-conventions.aliases]` enforce import aliases
  such as `typing as typ` and `collections.abc as cabc`.
- `[tool.ruff.lint.flake8-tidy-imports.banned-api]` bans deprecated
  `typing.*` aliases and explains each replacement.
- `[tool.ruff.lint.pylint]` sets Ruff's Pylint-derived thresholds.
- `[tool.ruff.lint.pydocstyle]` and `[tool.ruff.lint.pydoclint]` configure the
  `DOC` docstring-consistency gate; see
  [Docstring consistency gate](#docstring-consistency-gate) below.
- `[tool.pylint.main]`, `[tool.pylint.design]`, and
  `[tool.pylint."messages control"]` configure the second-tier Pylint pass.
- `[dependency-groups].dev` selects the controlled `df12-python-lints` v0.3.0
  release tag, matching the standalone scanner. The enabled message list
  includes `R9112` (`prefer-type-statement`).
- `ambrleaks.toml` contains narrow value allowlists for deterministic public
  fixture data that matches a scanner pattern.

When changing lint policy, update both `pyproject.toml` and this guide. If the
change alters the architecture of the lint gate, update
[ADR-003](adr-003-two-tier-python-linting.md) as well.

### Ruff `ASYNC` (flake8-async) policy

Cuprum selects the Ruff `ASYNC` (flake8-async) rule family in
`[tool.ruff.lint]` so async-correctness lints run as part of `make lint`. The
two that matter for the subprocess surface are `ASYNC109` (async functions that
take a `timeout` parameter) and `ASYNC240` (blocking `pathlib` calls inside an
async function). The family guards the async subprocess code against
callee-owned deadlines and accidental blocking I/O.

Two suppressions are scoped as narrowly as possible rather than disabling the
family:

- **Public API (`# ruff: ignore[async-function-with-timeout]`).** `SafeCmd.run`
  and `Pipeline.run` keep their documented `timeout` parameter, which
  deliberately mirrors `subprocess.run(timeout=...)`. `ASYNC109` would instead
  have the caller own the deadline through `asyncio.timeout()`, but the
  parameter is public, documented ergonomics, so each definition carries a
  per-line `# ruff: ignore[async-function-with-timeout]` with a rationale
  comment rather than dropping the parameter. Internal helpers do not take a
  `timeout` parameter (see
  [ADR-007](adr-007-subprocess-execution-module-boundaries.md)); only the
  public surface is suppressed.
- **Test scaffolding (`per-file-ignore`).** `ASYNC109` and `ASYNC240` are
  ignored through `[tool.ruff.lint.per-file-ignores]` in `pyproject.toml` for
  two modules — `cuprum/unittests/test_observe_stdin_early_close.py` and
  `tests/behaviour/_execution_runtime_support.py`. Their async scaffolding
  polls a PID file with asyncio-only helpers, so a `timeout` parameter and
  blocking `pathlib` calls are acceptable there; the async-native path libraries
  (`trio.Path` / `anyio.Path`) that `ASYNC240` recommends are not in use. A
  third module, `cuprum/unittests/test_pipeline_teardown_cancellation.py`,
  shares only the `ASYNC240` exemption, for the same reason as the two modules
  above: it polls a cross-process marker file that no asyncio primitive can
  observe. Naming these paths rather than globbing `**/test_*.py` stops
  unrelated and future async tests inheriting the exemption silently. The
  rationale is recorded next to each ignore in `pyproject.toml`.

When changing either suppression, keep the `pyproject.toml` comments and this
section in step.

### Docstring consistency gate

Ruff's `DOC` rule family, configured under `[tool.ruff.lint.pydocstyle]` (NumPy
convention, section order Parameters -> Returns -> Raises) and
`[tool.ruff.lint.pydoclint]`, checks that a docstring's structured sections
match the signature it documents:

- `DOC201` — a value-returning function documents no `Returns` section.
- `DOC402` — a generator documents no `Yields` section.
- `DOC501` — an exception raised directly in the function body is not
  documented in a `Raises` section.
- `DOC502` — a documented exception is not raised directly in the function
  body (it only propagates from a callee).

`ruff==0.16.4` is pinned in the dev dependency group because the `DOC` rules
are preview-only (`[tool.ruff]` sets `preview = true`). An unpinned Ruff could
change which docstrings pass the gate and make it non-reproducible between
machines and CI. The rule-name suppression comment form used throughout this
codebase (`# ruff: ignore[rule-name]`, for example
`# ruff: ignore[docstring-extraneous-exception]`) is itself preview-only in
Ruff 0.16, so `preview = true` and the pinned Ruff version are load-bearing for
every suppression in this codebase, not only for the `DOC` gate.

`[tool.ruff.lint.pydoclint]` sets `ignore-one-line-docstrings = true`, so a
single-line docstring needs no structured sections at all. This drives a
recurring judgement call: a multi-line docstring on a value-returning function
must carry a `Returns` section, so there are exactly two legal shapes — a
one-line summary, or a multi-line docstring with the required sections. An
explanatory paragraph cannot be kept while dropping `Returns`. When a private
helper's rationale is worth keeping but the structured sections would be noise,
the established pattern is to collapse the docstring to one line and move the
rationale to an inline `#` comment immediately above the relevant code.

When an exception merely propagates from a callee rather than being raised by a
literal `raise` in the function's own body, documenting it trips `DOC502`. The
house convention is a scoped suppression on the docstring's closing line, with
a justification naming where the exception comes from:

```python
    """Run the command.

    Raises
    ------
    ForbiddenProgramError
        If the program is not permitted by the active context allowlist.
    """  # ruff: ignore[docstring-extraneous-exception] - propagates from allowlist
```

Use only one such suppression per docstring.

## Maturin pin synchronization and native wheel tests

These checks span three test modules, one per concern — `test_maturin_pins.py`,
`test_maturin_toolchain.py`, and `test_maturin_build.py` — and the helpers
behind them are split across three boundaries.

The **pin-synchronization** checks live in
`cuprum/unittests/test_maturin_pins.py`, with their readers and regexes local
to that module: they read repository files, have a single consumer, and gain
nothing from indirection. Two exceptions sit in
`cuprum/unittests/_maturin_pin_support.py`, each because it genuinely has a
second consumer — the threshold this policy asks for before sharing anything:

- `read_expected_maturin_version` — the pin comparison here, and the wheel
  snapshot's `Generator` assertion in `test_maturin_build.py`.
- `MANYLINUX_CONTAINER_SHA256_RE` — the container-pin assertion here, and the
  generated references in `test_manylinux_container_ref_properties.py`. Sharing
  it through the support module keeps one test module from importing a private
  name out of another.

The **availability detectors** are tested together in
`cuprum/unittests/test_maturin_toolchain.py`: `toolchain_available` and
`maturin_script_locatable` answer adjacent questions about the same build, and
the wheel test is gated on both.

The **wheel build and toolchain detection** stay in `tests/helpers/maturin.py`,
because they wrap `subprocess` and `sysconfig` probing that does not inline
cleanly: build a wheel (`build_native_wheel_artefact`), report toolchain
availability (`toolchain_available`), and report separately whether maturin's
own script lookup can find its binary (`maturin_script_locatable`).

The build runs `python -m maturin` under the current interpreter, so it uses
whichever maturin that environment provides rather than selecting a version
itself. Two separate mechanisms enforce alignment with the declared pin.
`test_installed_maturin_matches_expected_pin` compares the `pyproject.toml` pin
against the installed maturin distribution's version, read from the current
interpreter's package metadata with `importlib.metadata.version("maturin")` —
the same interpreter that runs the build — and gates on that interpreter being
able to *import* `maturin`, not on a CLI being present on `PATH`. A launcher
found on `PATH` can belong to a different environment from the one the build
uses. The snapshot test asserts the built wheel's `Generator` matches that same
pin, so a wheel built by an unexpected maturin fails the suite.

The **wheel-artefact snapshot** parsers (`wheel_build_snapshot` and its private
helpers) live in the sibling module `tests/helpers/maturin_wheel.py`, keeping
each module below the Pylint module-length limit. `tests/helpers/maturin.py`
re-exports `wheel_build_snapshot`, so import sites use
`from tests.helpers.maturin import wheel_build_snapshot` unchanged.

The re-use policy for all three is to stay that way — do not re-externalize
further helpers until a second concrete consumer exists and the shared
interface can be designed against real requirements rather than anticipated
ones.

**Pin synchronization** (`test_maturin_pins_are_synchronized`) Asserts that the
maturin version declared in `pyproject.toml`,
`.github/workflows/build-wheels.yml`, and
`.github/actions/build-wheels/action.yml` are identical. When updating the
maturin pin, update all three locations and run this test to confirm they are
in step.

**Aarch64 manylinux container pin**
(`test_manylinux_aarch64_container_is_pinned_to_sha256` and
`test_manylinux_aarch64_container_is_referenced_by_build_step`) Asserts that
`MANYLINUX_AARCH64_CONTAINER` in `.github/workflows/build-wheels.yml` is pinned
to an SHA-256 digest and that `build-wheels.yml` uses the pinned variable in
the Linux aarch64 maturin build step.

When refreshing this container, update the value in
`MANYLINUX_AARCH64_CONTAINER` to
`ghcr.io/rust-cross/manylinux_2_28-cross@sha256:<digest>` and keep the inline
comment to the original mutable reference:
`# ghcr.io/rust-cross/manylinux_2_28-cross:aarch64`.

The deterministic tests assert that the live workflow value is correctly formed
and consumed by the aarch64 build step. The property-based tests prove that the
shared regex accepts every valid 64-character hexadecimal digest and rejects
the unbounded space of mutable tags and truncated digests, giving confidence
beyond any single example.

To update the pinned digest, resolve the tag digest for
`ghcr.io/rust-cross/manylinux_2_28-cross:aarch64`, replace only the value in
`MANYLINUX_AARCH64_CONTAINER`, and rerun:

```bash
uv run pytest cuprum/unittests/test_maturin_pins.py \
    -k "manylinux_aarch64_container"
```

**Installed version check** (`test_installed_maturin_matches_expected_pin`)
Skipped automatically when the `maturin` *module* cannot be imported by the
running interpreter. That is the right boundary rather than `PATH`, because the
build runs `python -m maturin`: a `maturin` launcher earlier on `PATH` can
belong to an entirely different environment from the one the build uses. When
the module is importable, asserts that the installed version matches the pinned
development dependency.

**Wheel build snapshot** (`test_maturin_wheel_build_snapshot`) Requires the
Rust toolchain (`cargo` and `rustc`). Builds a native wheel into a temporary
directory, extracts normalized metadata and layout information, and compares
the result against a [syrupy](https://github.com/syrupy-project/syrupy)
snapshot stored at `cuprum/unittests/__snapshots__/test_maturin_build.ambr`.

To update the snapshot after a maturin or PyO3 bump, run:

```bash
uv run pytest cuprum/unittests/test_maturin_build.py \
    --snapshot-update -k test_maturin_wheel_build_snapshot
```

### Debug Rust-pump early-exit regression

`cuprum/unittests/test_rust_pump_debug_abort.py` checks the Rust pump's writer
ownership on broken-pipe and timeout paths. It requires both the Rust toolchain
and a maturin script that the current interpreter can locate, using
`toolchain_available()` and `maturin_script_locatable()` for those
prerequisites.

The test uses `build_debug_native_wheel_artefact()` without `--release`, so the
debug build retains Rust's I/O-safety assertions. It extracts that fresh wheel
and starts a fresh child interpreter to run the scenarios with
`CUPRUM_STREAM_BACKEND=rust`; this keeps the child from reusing a native module
loaded by the test process. The child must exit successfully, and the test
explicitly rejects `SIGABRT` (`-6`) and `SIGSEGV` (`-11`).

### `maturin_script_locatable()` — native-wheel skip boundary

`maturin_script_locatable()` (in `tests/helpers/maturin.py`) is the shared
probe that decides whether the native-wheel build contract can actually run. It
mirrors `maturin.__main__.get_maturin_path`: maturin resolves its bundled
binary by scanning each `sysconfig` scheme's `scripts` directory for a file
named `maturin`, keyed off the running interpreter's `sys.prefix` — **not**
`sys.path` or `PATH`.

This is deliberately narrower than `toolchain_available()`, and the two answer
different questions:

- `toolchain_available()` — is the `maturin` module importable and are `cargo`
  and `rustc` on `PATH`? It uses `importlib.import_module` rather than
  `importlib.util.find_spec`, because the build runs `python -m maturin`, which
  needs the module to *import*: a module that is merely findable can still fail
  to import.
- `maturin_script_locatable()` — can maturin find its own compiled script the
  way `python -m maturin build` will at runtime?

The two disagree in layered or ephemeral interpreters — most importantly the
`uv run --with mutmut==3.6.0` overlay used by the mutation-testing workflow.
There, the project virtualenv is on `sys.path` (so the module imports and
`toolchain_available()` returns `True`), but `sys.prefix` points at a temporary
environment that never received maturin's script, so `python -m maturin build`
fails with ``Unable to find `maturin` script`` before it can invoke `cargo`.
This previously aborted the whole mutmut baseline. In a normal virtualenv (CI,
`build-wheels.yml`, local `uv run pytest`) `sys.prefix` matches the install
location, the probe returns `True`, and the real build runs — so the skip never
masks a genuine regression.

**Reuse policy.** Any test that shells out to `python -m maturin build` (or
otherwise depends on maturin locating its own binary) — for example a new
wheel-layout, packaging, or reproducibility test — should gate on **both**
`toolchain_available()` and `maturin_script_locatable()`, skipping with a
reason that names `sys.prefix` when the script is unreachable. Tests that only
import the `maturin` Python module, or that inspect pins/metadata without
building, need only the checks they already use and should **not** adopt this
probe. Reuse the existing helper rather than re-deriving the `sysconfig` scan;
extend `maturin_script_locatable()` in place if maturin changes how it locates
its binary.

## Rust stream buffer-size validation

`rust/cuprum-rust/src/lib.rs` validates the `buffer_size` argument to
`rust_pump_stream` / `rust_consume_stream` at the PyO3 boundary through a pure
`checked_buffer_size(i64) -> Result<usize, &'static str>` helper, wrapped by
`validate_buffer_size` (which maps the message to `PyValueError`). The contract
is: reject non-positive values, values that overflow `usize` on the target
platform, and values above `MAX_BUFFER_SIZE` (1 GiB, `1 << 30`) — the cap
guards against absurd allocations while comfortably exceeding any realistic
transfer buffer (the default is 64 KiB). `checked_buffer_size` is kept pure so
its boundaries are property tested directly in
`rust/cuprum-rust/src/buffer_size_tests.rs`; the Python-side error mapping is
exercised in `cuprum/unittests/test_rust_streams_boundary_property.py`. Keep the
`_streams_rs.py` wrapper docstrings, `docs/cuprum-design.md`, and the users'
guide aligned with this contract when the cap changes.

## Development dependency pins

Test tooling occasionally needs a temporary upper bound while an upstream
project catches up with a newer release. These pins live in `pyproject.toml`'s
`[dependency-groups]` `dev` group — dev-only, never in the runtime
`[project.optional-dependencies]` — each with an inline rationale and a
tracking link, and are lifted once the upstream fix lands.

- `pytest<9.1` — pytest 9.1 deprecates the nodeid/baseid path that `pytest-bdd`
  8.1.0 relies on for fixture registration, raising `PytestRemovedIn10Warning`
  under the behavioural suite. The constraint holds the dev environment on
  pytest 9.0.x until `pytest-bdd` migrates to the node-based fixture API. Track
  [pytest-bdd#823](https://github.com/pytest-dev/pytest-bdd/issues/823);
  remove the pin and this note once a released `pytest-bdd` supports pytest 9.1.

## Gating the paid benchmark job

`benchmark-ratchet` is the longest-running job in `ci.yml` and runs on a paid
`ubicloud-standard-2` runner, declared to `actionlint` in
`.github/actionlint.yaml` alongside the configuration variables the workflows
read. Most pull requests — documentation edits, Dependabot `github-actions`
batches — cannot change pipeline throughput, so a cheap GitHub-hosted `changes`
job classifies the diff with `dorny/paths-filter` and publishes a single
`bench` output. `benchmark-ratchet` takes `changes` in `needs` and gates on:

```yaml
if: needs.changes.result == 'success' && (github.event_name != 'pull_request' || needs.changes.outputs.bench == 'true')
```

Three properties of that arrangement are load-bearing:

- **`changes` runs on every event, not only pull requests.** A skipped
  dependency skips its dependants, so gating `changes` itself would stop pushes
  to `main` from benchmarking.
- **Non-pull-request events are never gated.** The run on `main` republishes
  the `benchmark-ratchet-main-baseline` artefact that pull-request runs compare
  against. Gating it fails open: the baseline ages, and regressions stop being
  detected rather than being reported.
- **The filter watches inputs, not just sources.** `cuprum/**`, `rust/**` and
  `benchmarks/**` are the obvious entries; `uv.lock`, `pyproject.toml`,
  `Makefile`, `conftest.py` and `.github/workflows/ci.yml` are there because a
  dependency bump or a change to how the benchmark is invoked can move the
  numbers as readily as a change to the code being measured.

The benchmark checkout sets `persist-credentials: false`, keeping the
repository-scoped token out of the checked-out code's Git configuration. The
benchmark needs Maturin and the development benchmark tooling, so its
optimized, in-place build calls the shared target with the runner's job limit:

```bash
CARGO_BUILD_JOBS="${LINUX_RUNNER_VCPUS}" make develop MATURIN_DEVELOP_FLAGS='--release --skip-install'
```

The baseline client and benchmark scripts are invoked directly with
`UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run python ...`, keeping the
`uv` caches and tools local to the checkout while using the prepared project
environment.

The `changes` job also writes the decision — event, detector status, filter
verdict, and whether the benchmark ran or was skipped — to
`$GITHUB_STEP_SUMMARY` on every run. A skipped job and a broken gate are
indistinguishable in the run list, so that table is where a maintainer auditing
paid-runner spend, or wondering why a pull request has no benchmark report,
reads what happened. Every field is a closed set, so the summaries stay
countable across runs; `benchmark-ratchet` is exactly one of `run`, `skip`, or
`skip-detector-failed`.

That step carries `if: ${{ !cancelled() }}` rather than inheriting the implicit
`success()`. A failed detector is the case most worth recording, because the
benchmark then skips for a reason that has nothing to do with the diff, and a
summary that stops being written exactly when the gate misbehaves documents
only the runs that needed no explanation. When the detector did not produce a
verdict the table says `unknown` rather than `false`: recording `false` would
assert "no performance-relevant changes", which is a claim nothing measured.

The workflow declares `concurrency: ci-${{ github.ref }}` with
`cancel-in-progress` true only for pull requests. A superseded pull-request run
only spends benchmark minutes on a diff nobody will merge; a cancelled `main`
run, by contrast, abandons the baseline upload, so `main` runs are left to
finish.

Be precise about what that does *not* buy. GitHub replaces a pending run when a
newer one arrives and promises nothing about the order runs complete in, so two
merges in quick succession may still publish baselines out of commit order, and
a superseded pending run publishes nothing at all. Concurrency is not an
ordering mechanism; anything that needs monotonic baselines has to enforce it
where the artefact is written.

The gate deliberately names no status function. GitHub inserts an implicit
`success()` into a job's `if:` unless the expression already names one, so a
failed `changes` job skips `benchmark-ratchet` rather than running it. Adding
`always()` reads as a harmless robustness tweak and is the single edit that
turns a broken detector into an unconditional paid run, so a test pins its
absence.

None of this is exercised by ordinary tests — invert the condition and the
suite still passes — so two suites read it back, split by what they assert
rather than by what they parse. Both go through `tests/helpers/workflow.py`,
which owns the parsing and the path model, so neither can pass against a gate
the other does not see:

- `cuprum/unittests/test_benchmark_gate_ci_contract.py` pins the
  *declarations*: the `bench` output wiring, the `needs` edge, the gate
  expression verbatim, the absent status function, the exact filter path set,
  the runner `changes` uses, the summary step's condition, and the concurrency
  policy. Property tests over sampled changed-path sets then check the rule
  those declarations encode — any watched path benchmarks however it is mixed
  with docs, a diff touching nothing watched skips, and a non-pull-request
  event always benchmarks.
- `tests/behaviour/test_benchmark_path_gate_behaviour.py`, with
  `tests/features/benchmark_path_gate.feature`, states the *decision* for pull
  requests a maintainer would recognize: docs-only, a Rust change, a dependency
  bump, a mixed diff, an empty diff, and a push to `main`.
- `tests/behaviour/test_benchmark_gate_summary_behaviour.py`, with
  `tests/features/benchmark_gate_summary.feature`, extracts the summary step's
  script from `ci.yml` and *runs* it under `bash` for each combination of event
  and detector state, then reads back the row it emitted. Asserting that the
  script mentions the right words is not enough: one that emitted nothing, or
  the opposite verdict, would contain the same words. The script touches only
  `$GITHUB_STEP_SUMMARY` and its own environment variables, which is what makes
  running it outside Actions evidence rather than simulation.

The path model handles the two pattern forms the filter is allowed to use — a
literal path, and a `dir/**` prefix — and a companion test fails if a pattern
outside those forms is added, so the model cannot silently stop describing the
filter. Pinning the gate expression verbatim is what keeps the model honest
about the other half: the property and behavioural tests reason with
`benchmark_runs`, which is only evidence about `ci.yml` because the expression
it mirrors is asserted character for character.

## Workflow pins and Dependabot

Dependabot owns the upgrade of GitHub Actions and reusable workflows, including
calls into `leynos/shared-actions`. Contract tests that assert a caller's exact
commit SHA create a lockstep dependency: every time Dependabot opens a bump PR,
the test fails until a human edits the pinned constant to match. That defeats
the purpose of automated dependency updates and turns a routine bump into a
manual chore.

Contract tests may still verify the *shape* of a reusable-workflow caller. They
must not verify the specific SHA value.

- Do assert the workflow references the correct reusable workflow path.
- Do assert the ref is pinned to a full 40-character commit SHA, not a
  mutable branch such as `main` or `rolling`.
- Do assert the expected `on:` triggers, least-privilege `permissions:`, and
  the inputs the caller relies on.
- Do not hard-code the current SHA value as an expected string. Match it with
  a pattern instead.
- Do not fail a test purely because Dependabot bumped the pinned SHA.

```python
import re

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def test_uses_pinned_full_sha(caller_step):
    ref = caller_step["uses"].split("@")[-1]
    assert SHA_RE.match(ref), f"expected a 40-hex commit SHA, got {ref!r}"
```

If a workflow's behaviour genuinely depends on a feature only present from a
particular commit onwards, express that as a comment or a changelog note, not
as a test assertion on the SHA string.

## Compile-time UI tests (trybuild)

The Rust crate at `rust/cuprum-rust/` uses
[trybuild](https://github.com/dtolnay/trybuild) to validate contracts that hold
at compile time and so cannot be observed by a runtime test: PyO3 macro
behaviour, and encapsulation boundaries such as the pump machine's private
`Transition`. Tests live under `rust/cuprum-rust/tests/ui/`:

- `tests/ui/pass/` — Rust files that **must compile** without error.
- `tests/ui/fail/` — Rust files that **must fail** compilation with diagnostics
  matching the corresponding `.stderr` file.

Run compile-time UI tests with:

```bash
cd rust && cargo test compile_time_ui
```

Committed `.stderr` fixtures must be regenerated with Rust 1.85.0, the
minimum-supported Rust compiler version used by CI. Update them after a PyO3 or
compiler upgrade:

```bash
cd rust && TRYBUILD=overwrite cargo +1.85.0 test compile_time_ui
```

Inspect the updated `.stderr` files before committing to confirm that each fail
test still represents a genuine compile-time error.

A fail case that pins an encapsulation boundary must include the real module
under test with `#[path]` rather than restating its shape, because a
hand-written copy would only prove the copy private.

Such a fixture may legitimately need to silence a lint the throwaway crate
trybuild builds cannot configure — that crate inherits no `[lints]` table, so
the workspace's `check-cfg = ["cfg(kani)"]` does not apply and the included
module's `#[cfg(kani)]` gates would otherwise bury the expected diagnostic in
`unexpected_cfgs` noise. Scope that suppression to the item that needs it:

- Do not use a crate-level inner attribute (`#![allow(...)]` or
  `#![expect(...)]`). It suppresses the lint for the probe code as well as the
  included module, so a diagnostic the case ought to report can vanish
  unnoticed. There are no crate-level `allow` or `expect` attributes anywhere in
  `rust/`, and no fixture needs the first one.
- Put an outer `#[expect(<lint>, reason = "...")]` on the `#[path]` module
  declaration instead, next to the `#[path]` attribute itself. Prefer `expect`
  over `allow`: if the production module later drops the gates that provoke the
  lint, an unfulfilled expectation fails the build rather than leaving a stale
  suppression behind.
- Every suppression carries a `reason` string naming the constraint, per the
  repository-wide rule that lint suppressions are tightly scoped and explained.

`tests/ui/fail/pump_transition_unreachable.rs` is the worked example.

Before committing one, verify it is not vacuous: weaken the production item it
guards, confirm the case then fails, and restore. Re-run that check after any
change to the fixture's attributes or structure, not only after a change to the
item under test — narrowing a suppression can alter which diagnostics reach the
`.stderr` fixture.

## Design decisions

### Deterministic fixtures over random data

Fixtures are generated from an SHA-256 counter-mode seeded stream rather than
from `os.urandom` or `random`. This makes every profiling run reproducible from
the same `--seed` and `--raw-bytes` arguments, enabling artefact comparison
across runs and across machines without storing large binary files in the
repository.

### Extracted helper methods in `__post_init__`

`TeeProfileWorkerConfig.__post_init__` delegates to private helpers rather than
containing all validation inline. This keeps the cyclomatic complexity of each
method below the project threshold of 9 while preserving the single-class
boundary. Inlining the helpers back into `__post_init__` would restore a
complexity of 13 and is explicitly rejected.

### Table-driven validation in `TeeProfileDriverConfig.__post_init__`

`TeeProfileDriverConfig.__post_init__` uses a table of `(name, value, minimum)`
triples to validate numeric bounds in a single loop, reducing measurable
cyclomatic complexity while preserving exact error messages.

### Scenario matrix order is a stable contract

The default scenario matrix order is fixed and documented. Callers, snapshot
tests, and CI artefact directories all depend on it. It must not be reordered
without updating snapshot files and any downstream tooling.

## Pipeline stdio policy and cwd conversion

Two canonical helpers own the subprocess spawn flags used by the subprocess
spawn paths:

- `_get_stage_stream_fds(idx, last_idx, capture_or_echo=...)` in
  `cuprum/_pipeline_stage_streams.py` is the single source of truth for the
  PIPE-versus-DEVNULL stdio selection when spawning pipeline stages. The first
  stage reads stdin from `DEVNULL`, later stages from a `PIPE`; intermediate
  stages always pipe stdout, while the final stage pipes stdout only when
  output is captured or echoed; stderr is piped exactly when output is captured
  or echoed. `_spawn_pipeline_processes` routes through this helper — do not
  re-derive the flags inline at pipeline-stage spawn sites, and do not use it
  for single-command spawning.
- `_cwd_arg(cwd)` in `cuprum/_subprocess_context.py` renders an optional
  working directory (`str | Path | None`) into the `cwd` argument for
  `asyncio.create_subprocess_exec`. Every spawn site must use it, so the
  conversion cannot drift between single-command and pipeline paths.

Re-use policy: any new spawn site must call `_cwd_arg`, and pipeline-stage
spawn sites must call `_get_stage_stream_fds` rather than copying the policy.
Changes to stdio selection (for example, adding stdin handling to pipelines)
belong in `_get_stage_stream_fds` so pipeline-stage behaviour and the
exhaustive tests in `cuprum/unittests/test_stage_stream_fds.py` stay
authoritative. That test module covers the full finite input domain (stage
position × capture/echo) and asserts agreement with the single-command policy
on the overlapping cases.

## Output behaviour carrier

`RunOutputOptions` (`capture`, `echo`) is the canonical carrier for command
output behaviour. Public command execution should accept or construct this
object rather than threading separate `capture` and `echo` keyword arguments
through new APIs. Keep that pairing intact so stdout/stderr handling stays
explicit, testable, and compatible with the `IOOptions` deprecation path.

`SafeCmd.run` / `run_sync` accept `RunOutputOptions` via the `output` parameter
and pass it straight through to `_prepare_execution_observation`, which reads
`output.capture` / `output.echo` for the observation tags. `Pipeline.run` /
`run_sync` use the same `output` parameter and resolve it before building the
pipeline execution config. There is no parallel internal `(capture, echo)`
value object: the former `_IOBehaviour` was redundant with `RunOutputOptions`
and has been removed. `IOOptions` remains only as a deprecated subclass alias
that emits a `DeprecationWarning`.

Internal adapters may translate legacy or aggregate configuration into
`RunOutputOptions` at the boundary. For example, the concurrent runner converts
its `_ConcurrentRunConfig` flags into `RunOutputOptions` once before calling
`SafeCmd.run`. Avoid reintroducing parallel output-option structures unless a
new boundary genuinely has different semantics; in that case, document the
translation rule here and cover it with behavioural tests.

`Pipeline.run` and `Pipeline.run_sync` retain `capture` and `echo` keyword
arguments only for compatibility. Those flags emit `DeprecationWarning` and
must not be combined with `output=RunOutputOptions(...)`; mixed usage raises
`ValueError` before any deprecation warning is emitted, so warning filters do
not obscure the documented ambiguity error.

## Subprocess execution module boundaries

The subprocess execution implementation is split by lifecycle concern across
`cuprum/_subprocess_execution.py`, `cuprum/_subprocess_stdin.py`,
`cuprum/_subprocess_timeout.py`, and `cuprum/_subprocess_wait.py`. See
[Cuprum design](cuprum-design.md) §8.1.5 and
[ADR-007](adr-007-subprocess-execution-module-boundaries.md) for the accepted
rationale and compatibility constraints.

Keep these boundaries intact. New stdin pipe behaviour belongs in
`_subprocess_stdin`; timeout or exit-event policy belongs in
`_subprocess_timeout`; the rules for *ending* a run — applying the deadline,
terminating the process, and draining the stream consumers exactly once —
belong in `_subprocess_wait`; and orchestration that coordinates them —
spawning, wiring streams, and assembling the result — belongs in
`_subprocess_execution`.

`cuprum/_subprocess_wait.py` holds `_wait_for_exit_code`,
`_wait_for_exit_code_within_timeout`, `_drain_stream_consumers`,
`_cancel_pending_consumers`, and `_reconcile_run_tasks` (see the wait-path
detail below). The drain interface is explicit: `_RunTaskOwnership` bundles the
optional stdin-writer task with the stdout and stderr consumer tasks,
`_DrainContext` carries capture, observability, and the optional
`discard_on_cancel` event, and `_reconcile_run_tasks(tasks, context)` cancels
stdin before settling both consumers. The reconciliation is one unit for
shielded cleanup. A capturing context gives readers the bounded EOF-grace
window and decodes absent output as text, while a non-capturing context settles
promptly and discards output. `_await_capture_eof_grace` uses an injected
waiter when supplied and otherwise delegates to the bounded `_await_eof_grace`;
`_settle_consumers` is the single optional settlement boundary that sets the
discard event before cancelling pending readers.
`_build_stream_config(execution, discard_on_cancel)` passes that shared event
into each stream's `_StreamConfig`, so timeout capture can retain buffered text
while cancellation and failure cleanup can discard it. It was split out of
`_subprocess_execution` so that module stays about orchestration; termination
routes through `_terminate_all_shielded` (`cuprum/_process_lifecycle.py`), so a
caller cancelling during the grace period cannot skip the `SIGKILL` escalation.

The pipeline side has an analogous split. `cuprum/_pipeline_results.py` owns
per-stage *reporting*: the terminal `exit` event a stage owes its observers
(`_emit_timeout_exit_events`) and the `CommandResult` assembly alongside it
(`_build_pipeline_stage_results`). It was split out of
`cuprum/_pipeline_internals.py`, which keeps that module about *running* a
pipeline — spawning, waiting, and cleanup. `_pipeline_internals` calls into
`_pipeline_results` to emit each stage's `exit` event and assemble its result,
on both the success and the timeout paths.

The subprocess wait path uses caller-owned deadlines: `asyncio.timeout()` was
adopted in place of `asyncio.wait_for()`, so the deadline is applied by the
caller rather than threaded through a `timeout` parameter. The wait logic is
split accordingly: `_wait_for_exit_code` awaits the process and terminates it
on cancellation, and no longer takes a timeout argument, while
`_wait_for_exit_code_within_timeout` applies `execution.timeout` around it. A
non-positive timeout is special-cased to expire immediately and
deterministically, because `asyncio.timeout()` alone would let a fast,
already-exited process race past a zero or negative deadline; this preserves
the behaviour of the previous `asyncio.wait_for()` implementation and is
guarded by regression tests in `cuprum/unittests/test_subprocess_timeout.py`.

Neither wait helper terminates unconditionally, and neither drains.
`_wait_for_exit_code` terminates the process only when the wait is cancelled —
which is also how an `asyncio.timeout` expiry arrives. A successful wait
completes when `_await_process_exit` obtains either the normal `process.wait()`
result or an already-published `process.returncode` when the asyncio waiter is
stranded, leaving the process alone in both cases.
`_wait_for_exit_code_within_timeout` terminates on its non-positive fast path
before any wait begins; when a positive timeout expires it instead cancels
`_wait_for_exit_code`, which terminates the running process.

Draining is the caller's job either way: after termination it drains the stream
consumers exactly once via `_drain_stream_consumers` (see the stdin-injection
sequence below), and terminating the process before that drain is what lets it
reach EOF.

The drain boundary carries an important teardown policy. Only the timeout path
passes `capture=True`, allowing the fixed `_CAPTURE_EOF_GRACE_S` window to
preserve text that is about to arrive at EOF. Cancellation, stdin-writer
failure, and consumer-failure cleanup pass `capture=False`, so they cancel and
settle promptly without waiting for that window or retaining output. The
timeout and cancellation reconciliations run under `_shielded_cleanup`; if
cancellation arrives during capture grace, both consumer tasks are settled
before `CancelledError` is re-raised. Keep these choices at the drain boundary:
moving capture draining into generic cleanup changes both timeout output and
cancellation latency.

All three subprocess exit-wait paths use `_await_process_exit` from
`cuprum/_process_exit.py`: the single-command wait in `_wait_for_exit_code`,
the per-stage waits created by `_PipelineWaitState.from_processes`, and the
standalone teardown wait used by `_terminate_process` for timeout or
cancellation cleanup. Fail-fast pipeline teardown awaits the existing per-stage
wait tasks and therefore inherits the same protection. The helper first accepts
an already-published `process.returncode`; otherwise it races `process.wait()`
against a bounded exponential-backoff poll of that return code, starting at
0.01 seconds and capped at 1.0 second, and cancels the losing task. This closes
the asyncio lost-wakeup race where `returncode` has been published but the
waiter remains pending. Keep the regression in
`cuprum/unittests/test_process_exit.py` aligned with this contract.

### Pipeline timeout and teardown

A pipeline enforces one deadline for the whole run, rather than a separate
deadline per stage. `_collect_pipeline_inputs`, in
`cuprum/_pipeline_internals.py`, awaits the pipeline against that single
deadline and maps its expiry to `TimeoutExpired`.

On expiry, `_collect_pipeline_inputs` calls `_terminate_timed_out_stages` (in
`cuprum/_process_lifecycle.py`) before it gathers stage output. Doing so first
lets the stage pipes reach EOF, so a captured pipeline cannot block on a
producer stage that is still running.

`_terminate_timed_out_stages` delegates to `_terminate_all_shielded`, which
runs the terminations in an owned, `asyncio.shield`ed task and holds off a
caller's cancellation until they finish before re-raising it. Without that
shielding, a cancellation landing in the grace-period wait would skip the
`SIGKILL` escalation and leave a `SIGTERM`-immune stage running. A shield
covers only the one `await` it wraps, so the wait is resumed behind a fresh
shield after each cancellation rather than re-awaited bare: only the teardown's
own completion ends the loop, and the first cancellation is re-raised so the
caller still sees exactly one. `_terminate_all_shielded` also backs
`_cleanup_spawned_processes` and `_cleanup_pipeline_on_error`, and the
fail-fast route in `_terminate_pipeline_remaining_stages` uses the shared
`_await_teardown_shielded` helper directly.

`_await_teardown_shielded` retries the shielded wait in a loop rather than
awaiting the teardown task once: cancelling a task propagates to whatever
future it is blocked on, so a second cancellation landing on a bare
`await termination` would cancel the teardown itself, and `asyncio.gather`
would pass that on to each `_terminate_process`, again skipping the `SIGKILL`
escalation and leaving a `SIGTERM`-immune child alive. The loop re-enters
`asyncio.shield(termination)` until the task reports done, so every wait —
including the retries — stays shielded, and it terminates because the
termination always settles, bounding it by the grace period rather than by the
caller's patience.

`_await_teardown_shielded` is itself built on `_shielded_cleanup`
(`cuprum/_process_lifecycle.py`), the shared primitive behind every shielded
cleanup in the codebase:
`async def _shielded_cleanup[T](cleanup: cabc.Awaitable[T]) -> T`. It owns
`cleanup` in a task, awaits it under `asyncio.shield`, and on cancellation
re-enters the shielded wait until the task reports done, then re-raises. A bare
`await asyncio.shield(coro)` is not enough on its own: the shield keeps
cancellation off the inner coroutine, but the *awaiting* coroutine resumes
immediately, so the run propagates its `CancelledError` while its own cleanup
tasks are still live — leaking exactly the tasks the cleanup exists to
reconcile. The retry loop exists because cancelling a task propagates to
whatever future it is awaiting, so re-awaiting the cleanup task unshielded
would cancel the cleanup itself. It returns the cleanup's value and propagates
the cleanup's own failure when the caller was not cancelled; callers that must
absorb failures do so themselves — `_await_teardown_shielded` does this by
passing `return_exceptions=True`.

Callers routed through `_shielded_cleanup` now include
`_await_teardown_shielded`; the timeout, cancellation, and stdin-failure
cleanup paths in `_run_subprocess_with_streams` and
`_run_subprocess_without_streams` (`cuprum/_subprocess_execution.py`); the
spawn-failure, timeout, and run-failure paths, plus
`_finalize_pipeline_execution`, in `cuprum/_pipeline_internals.py`; and
`_execute_with_hooks` in `cuprum/sh.py`, which previously used a bare
`await asyncio.shield(...)`. Two further helpers keep multi-step cleanup as one
shielded unit: `_reconcile_run_tasks` in `cuprum/_subprocess_wait.py` cancels
the stdin writer, then drains the stream consumers, and
`_reconcile_pipeline_run_failure` in `cuprum/_pipeline_internals.py` cancels
the stream tasks, then drains the observe-hook tasks. Shielding the halves
separately would let a cancellation landing between them abandon the second.

The inter-stage pump tasks, created by `_create_pipe_tasks`, are created and
owned by `_collect_pipeline_inputs` rather than by `_wait_for_pipeline`. This
matters because a non-positive deadline gives `asyncio.wait_for` a zero
timeout, and that cancels `_wait_for_pipeline` before its body — and therefore
its `finally` — ever runs, so that `finally` cannot reconcile the pumps. On the
timeout route, `_reconcile_pipe_tasks` cancels and drains the pump tasks
instead.

## Subprocess timeout observability

Subprocess timeouts surface through the existing `ExecEvent` / `sh.observe()`
observe-hook stream (see the [users' guide](users-guide.md) for the public
event contract) and, mirroring the `cuprum.stdin` convention, through a
best-effort `cuprum.timeout` log record carrying the same stable `cuprum_*`
`extra` fields. Both accompany — never replace — the public `TimeoutExpired`
exception and the existing `start` and `exit` events.

Three observe events are added to `ExecPhase`:

- **`timeout`** — emitted before `TimeoutExpired` when a run exceeds its
  deadline. Fields: `operation` (`"wait"`), `pid`, `timeout_s` (the configured
  timeout), `error_type` (`"TimeoutError"`), and `timeout_mode`, which
  distinguishes an `"elapsed_deadline"` wall-clock expiry from a
  `"non_positive_immediate"` expiry taken for a non-positive (`timeout <= 0`)
  deadline that never awaits the process.
- **`teardown_error`** — emitted when cancelling and draining a stream
  consumer surfaces an unexpected exception during teardown. Fields: `operation`
  (`"drain"`), `pid`, and `error_type` (the comma-joined failure classes). The
  failure is absorbed to preserve the primary timeout or cancellation, but
  stays observable through this event.
- **`capture_eof_grace_expired`** — emitted when a capturing drain exhausts
  `_CAPTURE_EOF_GRACE_S` with one or two readers still pending. It carries the
  execution's `exec_id` and `pid`, `operation` (`"drain"`), `eof_grace_s`, and
  `pending_readers`. It is projected to the
  `cuprum_capture_eof_grace_expired_total` counter, labelled only by `program`
  and `project`, and to a `cuprum.capture_eof_grace_expired` event on the
  matching trace span. No captured stream payload is emitted; unexpected reader
  failures remain the separate `teardown_error` signal.

The two timeout-expiry routes report through `_report_timeout_expiry` in
`cuprum._timeout_reporting`, which pairs the log record with the observe event
from one set of values so the two channels cannot drift. The single-command
path calls it from `_wait_for_exit_code_within_timeout`; the pipeline path
calls it from `_report_pipeline_timeout_expiry`, once per stage, because a
pipeline enforces its deadline for the whole run and so never passes through
the single-command wait helper. Capture-grace expiry instead uses
`_report_capture_eof_grace_expiry`, which has no payload or timeout exception
to report.

`_timeout_reporting` is a module of its own rather than part of
`_subprocess_timeout` because this surface is shared by both paths: the
pipeline caller cannot import the timeout-translation module without closing an
import cycle. It imports `_EventDetails` from `_pipeline_types`, the module
that defines it, rather than from `_pipeline_internals`, which merely
re-exports it and would reintroduce that cycle. Splitting it out also brought
`_subprocess_timeout` back under the 400-line module ceiling.

Emission is best-effort and cannot alter control flow: a synchronous
observe-hook failure (which `_StageObservation.emit` otherwise re-raises) is
swallowed outright, so it can never mask `TimeoutExpired` or `CancelledError`.
A hook that returns an awaitable is scheduled as a background task instead;
those tasks are still tracked and drained during cleanup, and a failure there
is aggregated with the active error into a `BaseExceptionGroup` rather than
replacing it, so the primary error is preserved within the aggregate. The
metrics adapter counts timeout and teardown phases as `cuprum_timeouts_total`
and `cuprum_teardown_errors_total`, and counts `capture_eof_grace_expired` as
`cuprum_capture_eof_grace_expired_total`; these counters use only the `program`
and `project` labels. The tracing adapter records them as ancillary span events
that leave the span open for the subsequent `exit`.

The parallel `cuprum.timeout` log records use the same field names under the
`cuprum_` prefix (`cuprum_operation`, `cuprum_pid`, `cuprum_timeout_s`,
`cuprum_error_type`, `cuprum_timeout_mode`, and `cuprum_teardown_outcome`):
`subprocess_timeout_expired` (`WARNING`) for expiry and
`subprocess_teardown_drain_failed` (`ERROR`, `cuprum_teardown_outcome` of
`"drain_error"`) for a drain failure. A logging failure is likewise swallowed.

## Subprocess stdin injection

When `stdin: StdinInput` is passed to `SafeCmd.run()`, the following sequence
executes:

1. `StdinInput.resolve(ctx)` encodes `text` via the execution-context
   encoding/errors, or returns `data` bytes unchanged.  Mutual exclusion is
   enforced at `StdinInput` construction time by `__post_init__`.
2. The resolved bytes are stored on `_SubprocessExecution.stdin_data`.
3. `_spawn_subprocess` opens `stdin=asyncio.subprocess.PIPE` when
   `stdin_data is not None`; otherwise `stdin=None` (inherit parent).
4. `_spawn_stdin_writer` creates an `asyncio.Task` that calls `_write_stdin`,
   which writes the bytes, drains the pipe, and closes it.  `OSError` and
   `RuntimeError` failures are logged to `cuprum.stdin` and emitted as a
   `stdin_error` trace event so operators can observe early-close scenarios
   without execution disruption.  Successful writes emit a `stdin` event with a
   byte count.  The metrics adapter increments `cuprum_stdin_bytes_total` for
   successful writes and `cuprum_stdin_errors_total` for failure events.
5. In the streaming path (`_run_subprocess_with_streams`), the stdin writer
   task runs concurrently with the stdout/stderr consumer tasks. On
   `TimeoutError` or `asyncio.CancelledError`,
   `_wait_for_streamed_process_exit` reconciles the stdin writer and stream
   consumers exactly once via `_reconcile_run_tasks`. The timeout
   reconciliation passes the execution's capture setting so the bounded EOF
   grace preserves partial text before `_handle_stream_timeout` raises
   `_SubprocessTimeoutError`; cancellation passes `capture=False` and re-raises
   `CancelledError` after all tasks settle.
6. In the non-streaming path, `_execute_subprocess` delegates to
   `_run_subprocess_without_streams`, which creates the same writer task and
   awaits `_wait_for_exit_code` itself.  On `TimeoutError` or
   `asyncio.CancelledError` from that wait, `_run_subprocess_without_streams`
   itself cancels and drains the writer task via `_cancel_stdin_writer` before
   the timeout is translated or the cancellation propagates, so a stdin drain
   blocked on an unread pipe cannot delay completion.

The `tests/helpers/stream_pipes.py` module provides
`drain_blocking_payload_size()`, a shared helper returning a stdin payload size
that reliably wedges the writer's `drain()`.  It probes the real OS pipe
capacity (via `fcntl(F_GETPIPE_SZ)` on Linux) and adds a mebibyte of headroom,
falling back to a conservative default on platforms that cannot probe it so
callers need no platform guard.  It exists solely to make blocked-`drain()`
regression tests deterministic and is shared by `test_safe_cmd_stdin.py` and
`test_observe_stdin_early_close.py`; new blocked-writer tests should reuse it
rather than hardcoding a payload size.

`_execute_with_hooks(cmd, execution, tracking)` is the single site that runs
`_execute_subprocess`, iterates after-hooks, and co-ordinates cancellation-safe
cleanup of pending hook tasks via `_shielded_cleanup` (see "Pipeline timeout
and teardown" above). It replaces the try/except ladder that previously lived
inline in `SafeCmd.run`, keeping the public method to a minimal orchestration
skeleton (plan event, before-hooks dispatch, delegation).

`_build_stream_config(execution, discard_on_cancel)` centralizes construction
of the `_StreamConfig` used by the streaming execution path
(`_run_subprocess_with_streams`). Extracting it removes one branch from that
function, reducing its cyclomatic complexity below the CodeScene threshold, and
makes the stdout-sink resolution logic testable in isolation. The required
`discard_on_cancel` event is shared by both stream consumers and is set by
`_settle_consumers` only for cleanup paths that must discard retained output.

Passing no `StdinInput` leaves subprocess stdin inherited from the parent
process, preserving the pre-feature behaviour.
