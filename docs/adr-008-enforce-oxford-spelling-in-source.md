# Architectural decision record (ADR) 008: Enforce Oxford spelling in source

## Status

Accepted on 2026-07-30. Cuprum applies en-GB-oxendict spelling to code
identifiers and source prose as well as maintained Markdown.

## Date

2026-07-30.

## Context and problem statement

Cuprum requires en-GB-oxendict spelling, including Oxford `-ize` and `-yse`
forms, but the policy previously named only comments and the mechanical gate
scanned only Markdown. Python identifiers consequently drifted to `-ise` forms
without a failing check. Reviewers could identify the inconsistency, but the
governing text did not say whether an internal rename was required.

Scanning whole source files also covers comments, docstrings, and string
fixtures. Existing source therefore has to comply before the wider gate can be
enabled. Literal names imposed by external APIs and deliberate test fixtures
cannot always be changed without breaking their contracts.

## Decision

Apply en-GB-oxendict spelling to code identifiers, comments, docstrings, string
fixtures, and prose in maintained Markdown, Python, and Rust files. Extend the
single `Makefile` `spelling` recipe to pass tracked `*.md`, `*.py`, and `*.rs`
files to the pinned `typos` version. Keep `make lint` and `make markdownlint`
wired to that recipe so local and Continuous Integration (CI) enforcement use
one policy.

Correct repository-owned source spelling before enabling the wider gate. When
an external wire format, API, command-line option, or deliberate test fixture
must retain a spelling that the shared dictionary rejects, add a narrowly
anchored ignore pattern to `typos.local.toml` and document the specific
contract beside it. Regenerate tracked `typos.toml` through
`scripts/generate_typos_config.py`; do not accept globally incorrect forms or
edit the generated file by hand.

## Consequences

- Identifier and source-prose drift now fails both lint entry points.
- Reviewers can apply one explicit spelling rule without distinguishing
  comments from internal names.
- Existing private helpers and tests use Oxford forms consistently; no public
  API compatibility shim is required.
- Contributors may see new spelling failures in Python or Rust files even when
  Markdown is unchanged.
- External contracts and deliberate fixtures require documented, narrowly
  scoped patterns rather than global word acceptance.
- Whole-source scanning may surface unrelated repository-owned spelling debt
  when the shared dictionary gains new corrections; that debt must be fixed or
  justified through the same exception process.
