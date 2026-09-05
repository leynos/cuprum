# Architectural decision record (ADR) 004: Interrogate docstring-coverage gate

## Status

Accepted on 2026-06-10 and amended on 2026-09-05. Cuprum adds `interrogate` as
a third Python lint tier that enforces complete docstring coverage across the
Python estate: the `cuprum` package, `tests`, `benchmarks`, `scripts`, and the
root `conftest.py`. This extends the two-tier policy recorded in
[ADR-003: Two-tier Python linting](adr-003-two-tier-python-linting.md).

## Date

2026-06-10.

## Context and Problem Statement

Ruff's `D` (pydocstyle) rules already require docstrings on public modules,
classes, and functions, but they do not require them everywhere. Nested
closures, dunder methods, properties, and private or stub classes can ship
without documentation while still passing `ruff check`. The result is uneven
docstring coverage: the public surface is documented, but the internal helpers
that are hardest to read at 3 a.m. often are not.

Cuprum wants a single, objective measure of docstring coverage that can fail
the lint gate when documentation regresses, without relying on contributors to
notice undocumented nested helpers by eye.

## Decision Drivers

- Guarantee that every documentable node carries a docstring, not only the
  public API surface that Ruff's `D` rules cover.
- Express the policy as an objective, reproducible percentage rather than a
  subjective review judgement.
- Keep the new tier inside the existing `make lint` workflow and ordering.
- Avoid a second docstring-style engine; `interrogate` measures presence, while
  Ruff continues to own docstring *style*.

## Options Considered

### Option A: Rely on Ruff's `D` rules alone

Keep the existing Ruff and Pylint tiers and accept that nested functions,
dunder methods, and stub classes can remain undocumented.

This is the lowest-effort option, but it leaves a real coverage gap: the
helpers most in need of explanation are exactly the ones Ruff does not require.

### Option B: Add `interrogate --fail-under 100` as a third tier

Run `interrogate --fail-under 100 cuprum` after `ruff check` and before the
PyPy-backed Pylint tier. Ruff continues to enforce docstring *style*;
`interrogate` enforces docstring *presence* at 100 per cent.

This closes the coverage gap with a small, focused tool and keeps the policy
objective. The cost is that every documentable node must carry a docstring,
including nested helpers and test doubles.

### Option C: Lower the bar with `--fail-under` below 100

Adopt `interrogate` but allow a sub-100 threshold so some nodes may remain
undocumented.

This softens the cost but reintroduces ambiguity about which nodes may skip
documentation, and a percentage ratchet is harder to reason about than a flat
"document everything" rule.

| Topic               | Ruff `D` only         | Interrogate at 100% | Interrogate below 100%   |
| ------------------- | --------------------- | ------------------- | ------------------------ |
| Coverage of helpers | Gaps on nested/dunder | Complete            | Partial, threshold-bound |
| Objectivity         | Style only            | High                | Lower (which nodes?)     |
| Contributor effort  | Lowest                | Highest             | Middle                   |
| Reproducibility     | High                  | High                | High                     |

*Table 1: Comparison of docstring-coverage options.*

## Decision Outcome / Proposed Direction

Choose Option B. The `lint` target runs `interrogate --fail-under 100` over
`benchmarks conftest.py cuprum scripts tests` immediately after `ruff check`
and before the PyPy-backed Pylint tier, using the Makefile's
`$(UV_RUN_ENV) uv run …` convention through the `$(INTERROGATE)` variable.
`interrogate` is added to the `dev` dependency group in `pyproject.toml`.

### Amendment (2026-09-05): coverage beyond the production package

The original decision scoped the gate to `cuprum` alone. The gate now covers
the whole Python estate. Triaging the 39 undocumented definitions outside
`cuprum` found every one of them in ordinary need of a one-line docstring:
nested test hooks and workers, scenario dataclasses, profiler adapters, and
contract-validation helpers. None warranted extraction, and interrogate's
`--ignore-nested-functions` or `--ignore-regex` escapes would have hidden
future undocumented helpers rather than enforced documentation. The gate
therefore extends as-is: `$(INTERROGATE)` interrogates
`benchmarks conftest.py cuprum scripts tests` at the same absolute
`--fail-under 100` bar, with no per-scope exclusions; no interrogate ignore
flags are used and no `[tool.interrogate]` configuration section exists.
The deliberate exclusions are none; every documentable node in every scope
must carry a docstring.

The three tiers now run in this order, each gating the next:

1. `ruff check` — fast, broad rules including docstring *style*.
2. `interrogate --fail-under 100 benchmarks conftest.py cuprum scripts tests` —
   docstring *presence* at 100 per cent across every Python scope.
3. PyPy-backed `pylint-pypy` — focused selected messages (see ADR-003).

## Known Risks and Limitations

- Every documentable node must carry a docstring, including nested closures,
  dunder methods, properties, and stub classes used only in tests. This raises
  the cost of new code and test doubles.
- Pushing docstrings onto large modules can take a file over the project's
  400-line ceiling enforced by Pylint's `too-many-lines`. When that happens,
  the module should be split by feature rather than suppressing the limit.
  Adding this gate required splitting the pipeline dataclasses out of
  `cuprum/_pipeline_internals.py` into `cuprum/_pipeline_types.py`, which
  re-exports nothing back; `_pipeline_internals` imports and re-exposes the
  moved types so existing import paths are unchanged.
- `interrogate` measures presence, not quality. A docstring that merely restates
  the code satisfies the gate, so review must still guard against noise per the
  "comment *why*, not *what*" guidance in `AGENTS.md`.

## Consequences

### Positive

- Docstring coverage is complete and objectively enforced, closing the gap left
  by Ruff's `D` rules.
- Test doubles, benchmark drivers, and lint scripts document to the same
  standard as the production package, so the estate's repositories can reuse
  one definition of "documented".
- The gate runs inside the existing `make lint` command with predictable
  ordering.
- Splitting oversized modules to satisfy the ceiling improves cohesion, as with
  the new `cuprum/_pipeline_types.py`.

### Negative

- The full lint target gains a third step and is marginally slower.
- New code and test doubles must be documented exhaustively, including nested
  helpers that previously needed no docstring.
- Documentation-driven growth of a module can force a refactor to stay under the
  400-line ceiling.
- Extending the gate to tests and scripts demands one-line docstrings on
  scenario fixtures and helpers. When a nested helper's docstring would only
  restate the enclosing function, extract or rename it, so the intent is
  expressible rather than suppressing the gate.
