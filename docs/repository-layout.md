# Repository layout

This document describes the major paths in the Cuprum repository and the
responsibilities attached to each area. It is an orientation guide, not an
exhaustive file listing.

## Top-level structure

```plaintext
.
├── .github/
├── .rules/
├── benchmarks/
├── cuprum/
├── docs/
├── rust/
├── test-wheelhouse/
├── tests/
├── AGENTS.md
├── Makefile
├── pyproject.toml
└── uv.lock
```

_Figure 1: Simplified repository tree for contributor orientation._

## Path responsibilities

| Path                | Responsibility                                                                                                                |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `.github/`          | GitHub Actions workflows, Dependabot configuration, and reusable workflow actions.                                            |
| `.rules/`           | Python coding rules referenced by `AGENTS.md` and maintainer guidance.                                                        |
| `benchmarks/`       | Benchmark drivers, profiling helpers, deterministic fixtures, and benchmark validation code.                                  |
| `cuprum/`           | Python package source for the command catalogue, safe command builders, execution runtime, streams, and Rust backend adapter. |
| `docs/`             | User, maintainer, design, decision, roadmap, and reference documentation.                                                     |
| `docs/execplans/`   | Durable execution plans for non-trivial implementation work.                                                                  |
| `rust/`             | Cargo workspace for Rust extension code and Rust-specific build/test targets.                                                 |
| `rust/cuprum-rust/` | PyO3-backed Rust crate that provides accelerated Cuprum components.                                                           |
| `test-wheelhouse/`  | Local wheel artefacts used by validation workflows and compatibility tests.                                                   |
| `tests/`            | Behavioural, integration, and regression tests for the Python package and user-visible workflows.                             |
| `AGENTS.md`         | Repository-specific assistant and contributor instructions.                                                                   |
| `Makefile`          | Canonical entry point for build, format, lint, typecheck, test, documentation lint, and diagram validation gates.             |
| `pyproject.toml`    | Python project metadata, dependency declarations, and tool configuration.                                                     |
| `uv.lock`           | Locked Python dependency graph for reproducible `uv` environments.                                                            |

_Table 1: Major repository paths and their ownership boundaries._

## Conventions

Source code for the Python package belongs under `cuprum/`; source code for the
Rust extension belongs under `rust/cuprum-rust/`. Cross-language behaviour
should be documented in [Cuprum design](cuprum-design.md) and surfaced in the
[users' guide](users-guide.md) when it affects public behaviour.

Long-lived documentation belongs under `docs/`. New architectural decisions
should be recorded as ADRs in `docs/` and linked from
[documentation contents](contents.md). New roadmap or planning work should use
the `docs/execplans/` directory only when an execution plan is explicitly
requested.

Repository-wide quality gates should be run through `Makefile` targets rather
than invoking underlying tools directly. Documentation-only changes should pass
`make markdownlint` and `make nixie`; code changes should additionally use the
relevant build, format, lint, typecheck, and test targets.
