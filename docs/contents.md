# Documentation contents

This index lists the long-lived documentation for Cuprum and explains when to
open each document.

## Documentation index

- [Documentation contents](contents.md) - the canonical index for project
  documentation.
- [Changelog](../CHANGELOG.md) - consumer-facing release notes and migration
  impact summaries.
- [Users' guide](users-guide.md) - user-facing command-building, catalogue,
  runtime, pipeline, and Rust backend behaviour.
- [Developers' guide](developers-guide.md) - maintainer workflows for profiling,
  linting, benchmarking, and internal development practices.
- [Repository layout](repository-layout.md) - path responsibilities and
  repository structure for contributors.
- [Documentation style guide](documentation-style-guide.md) - documentation
  spelling, structure, Markdown, roadmap, RFC, and ADR rules.
- [Scripting standards](scripting-standards.md) - conventions for project helper
  scripts, command execution, path handling, and command mocking.
- [CI cache ownership](ci-cache-ownership.md) - which job writes each CI cache
  family, why the compiler cache is split by interpreter and build shape, and
  how resource use is sampled on the paid Linux runners.

## Design and decisions

- [Cuprum design](cuprum-design.md) - system architecture, command model,
  pipeline design, Rust extension strategy, and benchmark policy.
- [ADR-001: Rust extension](adr-001-rust-extension.md) - accepted decision to
  add Rust acceleration through PyO3 and maturin.
- [ADR-002: Additional Rust components](adr-002-additional-rust-components.md) -
  accepted decision for extending Rust coverage beyond the initial stream
  backend.
- [ADR-003: Two-tier Python linting](adr-003-two-tier-python-linting.md) -
  accepted decision for combining Ruff with Pylint under PyPy.
- [ADR-004: Interrogate docstring-coverage gate][adr-004] - accepted decision
  to enforce 100% docstring coverage as a third lint tier.
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
  interleaved measurement that selected the Python stream read size for
  roadmap item 5.1.1.

[adr-004]: adr-004-interrogate-docstring-gate.md
[adr-005]: adr-005-unified-rust-availability-probe.md
[adr-006]: adr-006-context-package-split.md
[adr-007]: adr-007-subprocess-execution-module-boundaries.md
[adr-008]: adr-008-rust-pump-observation-channel.md
[adr-009]: adr-009-enforce-oxford-spelling-in-source.md
[local-validation]: local-validation-of-github-actions-with-act-and-pytest.md
[tee-baseline]: tee-hotpath-profiling-baseline-2026-06-12.md
[tee-read-size-sweep]: tee-hotpath-read-size-sweep-2026-08-29.md
