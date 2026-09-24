# Documentation contents

This index lists the long-lived documentation for Cuprum and explains when to
open each document.

## Documentation index

- [Documentation contents](contents.md) - the canonical index for project
  documentation.
- [Changelog](../CHANGELOG.md) - consumer-facing release notes and migration
  impact summaries.
- [Users' guide](users-guide.md) - user-facing command-building, catalogue,
  runtime, pipeline, and Rust backend behaviour, with task recipes, an
  operational reference, and a glossary.
- [0.2.0 migration guide][migration-020] - upgrade guidance for catalogue
  construction, line observation, result measurements, stream metrics,
  diagnostics, idle heartbeats, presentation sinks and their group and annotate
  flags, and the benchmark ratchet's measurement protocol.
- [Developers' guide](developers-guide.md) - maintainer workflows for native
  builds, profiling, linting, benchmarking, and internal development practices.
- [Repository layout](repository-layout.md) - path responsibilities and
  repository structure for contributors.
- [Documentation style guide](documentation-style-guide.md) - documentation
  spelling, structure, Markdown, roadmap, RFC, and ADR rules.
- [Scripting standards](scripting-standards.md) - conventions for project helper
  scripts, command execution, path handling, and command mocking.
- [CI cache ownership](ci-cache-ownership.md) - which job writes each CI cache
  family, why the compiler cache is split by interpreter and build shape, and
  how resource use is sampled on the paid Linux runners.
- [Native-pump Loom model](design-loom-native-pump-model.md) - the bounded
  production correspondence, assumptions, and safety claims for native-pump
  concurrency checks.
- [CI benchmark-gate telemetry](ci-benchmark-gate-telemetry.md) - the
  benchmark-gate JSONL artefact schema, retention, retrieval, analysis recipe,
  and fail-open delivery contract.

## Design and decisions

- [Cuprum design](cuprum-design.md) - system architecture, command model,
  pipeline design, Rust extension strategy, and benchmark policy.
- [ADR-001: Rust extension](adr-001-rust-extension.md) - accepted decision to
  add Rust acceleration through PyO3 and maturin.
- [ADR-002: Additional Rust components](adr-002-additional-rust-components.md) -
  accepted decision for extending Rust coverage beyond the initial stream
  backend.
- [ADR-003: Six-stage Python lint architecture][adr-003] -
  accepted decision for Ruff and PyPy-backed Pylint, with an addendum covering
  the later `interrogate`, DF12, Ambrleaks, and Skylos lint stages.
- [ADR-004: Interrogate docstring-coverage gate][adr-004] - accepted decision
  to enforce 100% docstring coverage through `interrogate`.
- [ADR-005: Unified Rust availability probe][adr-005] - accepted decision to
  unify Rust availability probing behind a cached dispatch resolver.
- [ADR-006: Context package split][adr-006] - accepted decision to split
  `cuprum/context.py` into a `cuprum/context/` package.
- [ADR-007: Subprocess execution module boundaries][adr-007] - accepted
  decision to split private subprocess execution by lifecycle concern.
- [ADR-008: Rust-pump observation channel][adr-008] - accepted decision to
  report Rust-pump routing decisions on a channel separate from `ExecEvent`.
- [ADR-009: Enforce Oxford spelling in source][adr-009] - accepted decision to
  govern identifiers and source prose with the repository spelling gate.
- [ADR-010: Rust-pump executor-hop spans][adr-010] - accepted decision to add
  opt-in executor-hop tracing without extending the pump event channel.
- [ADR-011: Audited Rust safety boundaries][adr-011] - accepted decision to
  isolate native resource operations from safe stream policy.
- [ADR-012: Linux dev-fast routing][adr-012] - accepted decision to confine
  accelerated Cargo builds to explicit, supported Linux debug routes.
- [ADR-013: Opt-in GitHub Actions presentation sink][adr-013] - accepted
  decision to frame runs in Actions log groups and annotate failures through an
  opt-in presentation sink.
- [ADR-014: Durable benchmark-gate telemetry][adr-014] - accepted decision to
  retain bounded benchmark-gate observations in GitHub Actions artefacts with
  fail-open delivery and no external sink or secret.
- [ADR-015: Actions-runner integration harness][adr-015] - accepted decision to
    verify the `changes` job boundary with a pytest-driven `act` harness rather
  than by reading the workflow source.
- [ADR-016: Stable-ABI native wheels][adr-016] - accepted decision to build
  one native wheel per platform against the CPython 3.12 stable ABI.

## Planning and validation references

- [Roadmap](roadmap.md) - phased delivery plan and implementation task
  breakdown.

- [Local validation guide][local-validation] -
  local Continuous Integration (CI) reproduction notes for workflow debugging.
- [Execution plans](execplans/) - task-specific implementation plans created
  when substantial work needs a durable plan.
- [Tee hot-path profiling baseline (2026-06-12)][tee-baseline] - measured
  hotspot verdicts from the tee profiling harness, gating ADR-002 Phase 2.
- [Tee hot-path read-size sweep (2026-08-29)][tee-read-size-sweep] - the
  interleaved measurement that selected the Python stream read size for roadmap
  item 5.1.1.
- [Rust boundary verification and unsafe inventory][rust-boundary-verification]
  - unsafe inventory, crate contracts, verifier evidence, and trusted
  assumptions for the native stream boundaries.

[adr-003]: adr-003-two-tier-python-linting.md
[adr-004]: adr-004-interrogate-docstring-gate.md
[adr-005]: adr-005-unified-rust-availability-probe.md
[adr-006]: adr-006-context-package-split.md
[adr-007]: adr-007-subprocess-execution-module-boundaries.md
[adr-008]: adr-008-rust-pump-observation-channel.md
[adr-009]: adr-009-enforce-oxford-spelling-in-source.md
[adr-010]: adr-010-rust-pump-hop-span.md
[adr-011]: adr-011-audited-rust-boundaries.md
[adr-012]: adr-012-linux-dev-fast-routing.md
[adr-013]: adr-013-opt-in-github-actions-presentation-sink.md
[adr-014]: adr-014-benchmark-gate-telemetry-sink.md
[adr-015]: adr-015-actions-runner-integration-harness.md
[adr-016]: adr-016-stable-abi-native-wheels.md
[local-validation]: local-validation-of-github-actions-with-act-and-pytest.md
[migration-020]: v0-2-0-migration-guide.md
[rust-boundary-verification]: rust-boundary-verification.md
[tee-baseline]: tee-hotpath-profiling-baseline-2026-06-12.md
[tee-read-size-sweep]: tee-hotpath-read-size-sweep-2026-08-29.md
