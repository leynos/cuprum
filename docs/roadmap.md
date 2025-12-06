# Cuprum roadmap

This roadmap translates the design into actionable increments without calendar
commitments. Phases are ordered and build on one another. Steps group related
workstreams. Tasks are measurable and must meet acceptance criteria to count as
done.

## Phase 1 – foundation

Focus: establish the typed core, scoped safety model, and reliable single
command execution.

### Step: typed command core

- [x] Introduce the `Program` NewType and a curated catalogue module with a
      default allowlist; block unknown executables by default and cover with
      unit tests.
- [x] Add `sh.make` to construct `SafeCmd` instances with typed argv handling
      and minimal builder examples; document the expected builder pattern in
      `docs/users-guide.md`.

### Step: execution runtime

- [x] Implement async `SafeCmd.run` with capture/echo toggles, env/cwd
      overrides, structured result object, and cancellation that sends
      terminate then kill after a grace period; add integration tests that
      assert cleanup on cancellation.
- [ ] Provide `run_sync` that mirrors async behaviour by driving the event loop
      and ensures identical error and result semantics; cover parity with
      tests.

### Step: context and hooks

- [ ] Add `CuprumContext` backed by a `ContextVar`, supporting allowlist
      narrowing plus before/after hooks with deterministic ordering and
      `.detach()`; test nesting across threads and async tasks.
- [ ] Ship a basic logging hook emitting start/exit events compatible with
      `logging` and wire it through context registration; document hook usage
      patterns.

## Phase 2 – pipelines and observability depth

Focus: provide pipeline execution, richer events, and concurrency helpers.

### Step: pipeline execution

- [ ] Implement `Pipeline` composition via the `|` operator with streaming
      between stages and backpressure handling; expose exit metadata per stage.
- [ ] Define and implement failure policy (fail fast, terminate downstream,
      surface failing stage) and cover with tests for early, middle, and late
      stage errors.

### Step: structured events and telemetry

- [ ] Expand execution events to include stdout/stderr line emissions, timings,
      and tag metadata; add `sh.observe` (or equivalent) registration.
- [ ] Provide example adapters for `logging`, Prometheus-style metrics, and
      OpenTelemetry traces, ensuring hooks remain optional and non-blocking.

### Step: concurrency helpers

- [ ] Add helper to run multiple `SafeCmd` instances concurrently with optional
      concurrency limits, while preserving hook semantics and aggregated
      results; document common patterns alongside examples.

## Phase 3 – builders and ergonomics

Focus: enrich the ecosystem around builders and optional ergonomics without
compromising explicitness.

### Step: builder library and validation

- [ ] Publish a core builder set for common tools (git, rsync, tar) using
      typed argument helpers such as `SafePath` and `GitRef`, with validation
      and unit tests.
- [ ] Provide a scaffold and guidance for project-specific builders, including
      a template module and checklist in `docs/users-guide.md`.

### Step: optional sugar and compatibility

- [ ] Introduce an opt-in registration API for attribute-style access (for
      example, `sh.register("ls", LS)`), preserving allowlist enforcement and
      documenting when to avoid the sugar.
- [ ] Supply static-analysis support (type stubs or a mypy plugin) that keeps
      attribute-style commands type-safe; include sample configuration.

### Step: configuration and policy

- [ ] Add policy toggles for allowlist narrowing, unsafe namespace warnings,
      and default echo behaviour; ensure defaults remain safe and test policy
      interactions.
- [ ] Document policy switches and recommended defaults in
      `docs/users-guide.md` and add release notes describing the migration path
      for existing users.
