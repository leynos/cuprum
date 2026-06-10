# Documentation contents

This index lists the long-lived documentation for Cuprum and explains when to
open each document.

## Documentation index

- [Documentation contents](contents.md) - the canonical index for project
  documentation.
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
- [ADR-003: Two-tier Python linting](adr-003-two-tier-python-linting.md) -
  accepted decision for combining Ruff with Pylint under PyPy.
- [ADR-004: Interrogate docstring-coverage gate][adr-004] - accepted decision
  to enforce 100% docstring coverage as a third lint tier.

## Planning and validation references

- [Roadmap](roadmap.md) - phased delivery plan and implementation task
  breakdown.

- [Local validation guide][local-validation] -
  local Continuous Integration (CI) reproduction notes for workflow debugging.
- [Execution plans](execplans/) - task-specific implementation plans created
  when substantial work needs a durable plan.

[adr-004]: adr-004-interrogate-docstring-gate.md
[local-validation]: local-validation-of-github-actions-with-act-and-pytest.md
