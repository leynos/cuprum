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

## Planning and validation references

- [Roadmap](roadmap.md) - phased delivery plan and implementation task
  breakdown.

- [Local validation guide][local-validation] -
  local Continuous Integration (CI) reproduction notes for workflow debugging.
- [Execution plans](execplans/) - task-specific implementation plans created
  when substantial work needs a durable plan.
- [Tee hot-path profiling baseline (2026-06-12)][tee-baseline] - measured
  hotspot verdicts from the tee profiling harness, gating ADR-002 Phase 2.

[adr-003]: adr-003-two-tier-python-linting.md
[adr-004]: adr-004-interrogate-docstring-gate.md
[adr-005]: adr-005-unified-rust-availability-probe.md
[adr-006]: adr-006-context-package-split.md
[adr-007]: adr-007-subprocess-execution-module-boundaries.md
[local-validation]: local-validation-of-github-actions-with-act-and-pytest.md
[tee-baseline]: tee-hotpath-profiling-baseline-2026-06-12.md
