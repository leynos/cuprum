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
- [Coverage timeout tiers](coverage-timeout-tiers.md) - the four nested
  timeouts that bound a coverage run, plus the separate boundary-harness
  compile tier and its per-test allowance.

## Vendored upstream documentation

These documents are imported from the `rstest-bdd` project and are not Cuprum's
own guidance. The [users' guide](rstest-bdd-users-guide.md) applies only where
a Cuprum change touches the Rust behaviour-testing integration that `AGENTS.md`
names as the project's validation framework. `cuprum-rust` depends on
`rstest-bdd` 0.5.0 today, so the imported guide describes the 0.6.0 release
rather than the version in use; consult the
[0.6.0 migration guide][rstest-bdd-migration-060] before raising that
dependency. The [provenance record](rstest-bdd-v0-6-0-import-provenance.md)
states the upstream commit, the checksums of both sides of the link rewriting,
and the content boundary.

- [`rstest-bdd` users' guide][rstest-bdd-guide] - upstream user-facing guide to
  the behaviour-driven development framework.
- [`rstest-bdd` 0.6.0 migration guide][rstest-bdd-migration-060] - the breaking
  changes between 0.5.0 and 0.6.0, with per-change migration steps.
- [`rstest-bdd` 0.5.0 migration guide][rstest-bdd-migration-050] - the
  intervening guide, retained because the target release's guidance builds on
  it.
- [`rstest-bdd` import provenance][rstest-bdd-provenance] - where the imported
  documents came from, how their cross-file links were rewritten, and what the
  import deliberately excludes.

## Design and decisions

- [Cuprum design](cuprum-design.md) - system architecture, command model,
  pipeline design, Rust extension strategy, and benchmark policy.
- [ADR-001: Rust extension](adrs/adr-0001-rust-extension.md) - accepted
  decision to add Rust acceleration through PyO3 and maturin.
- [ADR-002: Additional Rust components](adrs/adr-0002-additional-rust-components.md)
  - accepted decision for extending Rust coverage beyond the initial stream
  backend.
- [ADR-003: Seven-stage Python lint architecture][adr-003] -
  accepted decision for Ruff and PyPy-backed Pylint, with addenda covering the
  later `interrogate`, DF12, Ambrleaks, Skylos, and nose duplication stages.
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
- [ADR-017: Attested, reconciled release pipeline][adr-017] - accepted
  decision to reconcile PyPI and GitHub Release assets by filename precedence,
  with least-privilege jobs, build provenance and PEP 740 attestations, and
  fail-open telemetry.
- [ADR-018: Typed environment policies][adr-018] - accepted decision to make
  environment inheritance, replacement, and deletion explicit at execution
  boundaries.
- [ADR-019: Owned-handle control observation channel][adr-019] - accepted
  decision to report what a control operation did to its child on a channel
  separate from `ExecEvent`, recorded as an event on the run's existing span.
- [ADR-020: Typed argument contract for sh.make builders][adr-020] - accepted
  decision to replace the `Callable[..., SafeCmd]` annotation with a
  `SafeCmdBuilder` protocol over a published `ArgValue` alias.
- [ADR-021: Opt-in process-group ownership][adr-021] - accepted decision to
  give callers opt-in ownership of a child's process group, with POSIX group
  teardown and an explicit Windows refusal.
- [ADR-022: Adopt a nose code-duplication gate][adr-022] - accepted decision to
  block `make lint` on the pinned nose detector with reasoned location-keyed
  exceptions, adopting an already-benchmarked tool choice by reference rather
  than rerunning the comparison here.
- [ADR-023: Opt-in telemetry redaction][adr-023] -
  accepted decision to redact configured values at opted-in export boundaries
  while preserving execution inputs and bounded metric labels.
- [ADR-024: Type captured command output][adr-024] - accepted decision to make
  captured single-command text non-optional while preserving the optional
  `CommandResult` contract for explicit and uncaptured calls.
- [ADR-025: Bounded output delivery][adr-025] - accepted decision for
  opt-in, byte-bounded output delivery on a run-owned daemon worker.
- [ADR-026: Narrow captured results only for complete text][adr-026] - accepted
  decision to construct `CapturedResult` only when both settled streams are
  decoded text, reporting per-stream and spilled captures as a `CommandResult`
  that carries the same capture records.
- [ADR-027: Keep compile-time contracts in the build graph][adr-027] - accepted
  decision to replace `trybuild` UI tests with doctests, pinning each
  `compile_fail` case to an error code beside a positive control, so no test is
  bounded by a nested build.

## Requests for comments

- [RFC 0001: Execution interception][rfc-0001] - proposed backend seam so
  callers can be tested without a process, with a recording double and a
  passthrough path for executable-level mocking frameworks.
- [RFC 0002: Reusable command composition helpers][rfc-0002] - proposed
  vocabulary for presence flags, option values, repeated options, and separated
  positional operands, plus a guard against keyword flags landing after a `--`
  separator.

## Planning and validation references

- [Roadmap](roadmap.md) - phased delivery plan and implementation task
  breakdown.

- [Local validation guide][local-validation] -
  local Continuous Integration (CI) reproduction notes for workflow debugging.
- [Execution plans](execplans/) - task-specific implementation plans created
  when substantial work needs a durable plan.
  - [Line-event field hoisting plan](execplans/5-2-1-hoist-the-invariant-exec-event-and-event-details.md)
    - implementation and validation for roadmap item 5.2.1. The hoist is
      implemented and measured at a 29.9087% construction share against an
      approved 30% limit, with the evidence under
      `profiling/5-2-1-line-event-emission/`.
- [Tee hot-path profiling baseline (2026-06-12)][tee-baseline] - measured
  hotspot verdicts from the tee profiling harness, gating ADR-002 Phase 2.
- [Tee hot-path read-size sweep (2026-08-29)][tee-read-size-sweep] - the
  interleaved measurement that selected the Python stream read size for roadmap
  item 5.1.1.
- [Tee hot-path line-event emission (5.2.1)][tee-line-event-emission] - the
  construction-share analysis, its verdict against the 30% limit, and the
  forecast that item 5.2.2 inverts the same metric by succeeding.
- [5.2.1 line-event-emission profile evidence](profiling/5-2-1-line-event-emission/README.md)
  - the six matched captures, the thirty unprofiled runs, the committed
  classifier rules, and the 30% reclassification the report's arithmetic rests
  on.
- [Rust boundary verification and unsafe inventory][rust-boundary-verification]
  - unsafe inventory, crate contracts, verifier evidence, and trusted
  assumptions for the native stream boundaries.

[adr-003]: adrs/adr-0003-two-tier-python-linting.md
[adr-004]: adrs/adr-0004-interrogate-docstring-gate.md
[adr-005]: adrs/adr-0005-unified-rust-availability-probe.md
[adr-006]: adrs/adr-0006-context-package-split.md
[adr-007]: adrs/adr-0007-subprocess-execution-module-boundaries.md
[adr-008]: adrs/adr-0008-rust-pump-observation-channel.md
[adr-009]: adrs/adr-0009-enforce-oxford-spelling-in-source.md
[adr-010]: adrs/adr-0010-rust-pump-hop-span.md
[adr-011]: adrs/adr-0011-audited-rust-boundaries.md
[adr-012]: adrs/adr-0012-linux-dev-fast-routing.md
[adr-013]: adrs/adr-0013-opt-in-github-actions-presentation-sink.md
[adr-014]: adrs/adr-0014-benchmark-gate-telemetry-sink.md
[adr-015]: adrs/adr-0015-actions-runner-integration-harness.md
[adr-016]: adrs/adr-0016-stable-abi-native-wheels.md
[adr-017]: adrs/adr-0017-release-pipeline.md
[adr-018]: adrs/adr-0018-typed-environment-policies.md
[adr-019]: adrs/adr-0019-control-observation-channel.md
[adr-020]: adrs/adr-0020-typed-argument-contract-for-sh-make.md
[adr-021]: adrs/adr-0021-process-group-ownership.md
[adr-022]: adrs/adr-0022-adopt-nose-duplication-gate.md
[adr-023]: adrs/adr-0023-opt-in-telemetry-redaction.md
[adr-024]: adrs/adr-0024-captured-command-output-types.md
[adr-025]: adrs/adr-0025-bounded-output-delivery.md
[adr-026]: adrs/adr-0026-narrowed-results-only-for-complete-text-capture.md
[adr-027]: adrs/adr-0027-compile-contracts-in-the-build-graph.md
[local-validation]: local-validation-of-github-actions-with-act-and-pytest.md
[migration-020]: v0-2-0-migration-guide.md
[rstest-bdd-guide]: rstest-bdd-users-guide.md
[rstest-bdd-migration-050]: rstest-bdd-v0-5-0-migration-guide.md
[rstest-bdd-migration-060]: rstest-bdd-v0-6-0-migration-guide.md
[rstest-bdd-provenance]: rstest-bdd-v0-6-0-import-provenance.md
[rust-boundary-verification]: rust-boundary-verification.md
[tee-baseline]: tee-hotpath-profiling-baseline-2026-06-12.md
[tee-line-event-emission]: tee-hotpath-line-event-emission-5-2-1.md
[tee-read-size-sweep]: tee-hotpath-read-size-sweep-2026-08-29.md
[rfc-0001]: rfcs/rfc-0001-execution-interception.md
[rfc-0002]: rfcs/rfc-0002-command-composition-helpers.md
