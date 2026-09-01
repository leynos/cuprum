# Architectural decision record (ADR) 003: Two-tier Python linting

## Status

Accepted on 2026-05-15 and amended on 2026-07-31. Cuprum adopts Ruff as the
first lint tier, PyPy-backed Pylint for selected built-in checks, and a pinned
`df12-python-lints` pass under CPython 3.14. The companion `ambrleaks` scanner
covers Syrupy snapshots.

## Date

2026-05-15.

## Context and Problem Statement

Cuprum already used Ruff for Python linting. Ruff provides fast feedback and a
broad rule set, including Pyflakes, pycodestyle, pydocstyle, security checks,
import conventions, and Ruff's native Pylint-derived rules. The project also
wants the lint policy used by `leynos/episodic`, including rules that
discourage deprecated `typing.*` aliases and selected Pylint messages that Ruff
does not fully cover.

Running full Pylint directly inside the project virtual environment would make
the lint gate slower, broader, and more coupled to project dependencies than
necessary. Cuprum needs a second lint tier that is focused, reproducible, and
easy to run from the existing `make lint` workflow.

## Decision Drivers

- Preserve Ruff as the fast first-line lint tool.
- Reuse the lint policy already proven in `leynos/episodic`.
- Add selected Pylint checks without enabling full Pylint by default.
- Keep Pylint isolated from the project virtual environment.
- Make the complete gate available through one command: `make lint`.
- Keep the lint runtime reproducible by pinning the shim revision.
- Run the df12 house checks against Cuprum's real syntax under CPython 3.14.
- Detect secret-like values and unredacted paths in Syrupy snapshots.

## Options Considered

### Option A: Ruff only

Keep the existing Ruff-only lint target and import only the Ruff-side rules from
`leynos/episodic`.

This would preserve speed and simplicity, but would omit selected Pylint checks
for logging interpolation, pattern matching, generator behaviour, environment
handling, subprocess safety, and several readability checks.

### Option B: Ruff plus full Pylint in the project environment

Run `uv run pylint` after Ruff and install Pylint as a project development
dependency.

This would avoid an extra shim, but it would couple Pylint to the project
environment and expose Cuprum to the full Pylint surface unless the Makefile
and configuration carefully disabled it. It would also diverge from the
approach used by `leynos/episodic`.

### Option C: Ruff plus focused Pylint through the PyPy shim

Run Ruff first, then run `pylint-pypy` through `uv tool run --python pypy` and
the pinned `leynos/pylint-pypy-shim` repository.

This keeps Ruff as the fast gate, adds a focused Pylint pass, and isolates the
second-tier runtime from the project virtual environment. Pinning the shim
revision makes toolchain changes explicit.

| Topic                            | Ruff only                     | Ruff plus project Pylint         | Ruff plus PyPy shim        |
| -------------------------------- | ----------------------------- | -------------------------------- | -------------------------- |
| Speed                            | Fastest                       | Slower                           | Slower than Ruff, isolated |
| Coverage                         | Misses selected Pylint checks | Broad, unless heavily configured | Focused selected messages  |
| Environment coupling             | Low                           | High                             | Low                        |
| Alignment with `leynos/episodic` | Partial                       | Partial                          | Full                       |
| Reproducibility                  | High                          | Depends on dev dependencies      | High through pinned shim   |

_Table 1: Comparison of Python linting options._

## Decision Outcome / Proposed Direction

Choose Option C. The root `Makefile` defines the Pylint command in terms of:

- `PYLINT_PYTHON`, defaulting to `pypy`;
- `PYLINT_TARGETS`, defaulting to `benchmarks conftest.py cuprum tests`;
- `PYLINT_PYPY_SHIM_REF`, pinned to a specific shim revision;
- `PYLINT_PYPY_SHIM`, the Git source for the shim; and
- `PYLINT`, the full `uv tool run` command.

The `lint` target runs `ruff check` first and the PyPy-backed Pylint tier after
`interrogate`. It then runs all `df12-python-lints` v0.3.0 messages under
CPython 3.14 and scans both snapshot roots with `ambrleaks`. Earlier failures
stop the target before later stages, keeping the feedback order predictable.

The canonical policy lives in `pyproject.toml`:

- `[tool.ruff]` and `[tool.ruff.lint]` define the Ruff tier.
- `[tool.ruff.lint.flake8-tidy-imports.banned-api]` bans deprecated
  `typing.*` aliases.
- `[tool.pylint.main]`, `[tool.pylint.design]`, and
  `[tool.pylint."messages control"]` define the focused second tier.
- The development dependency pins the `df12-python-lints` v0.3.0 release at an
  immutable commit; the Makefile's `DF12_PYTHON_LINTS_REF` pins the standalone
  `ambrleaks` tool to the same commit.
- `ambrleaks.toml` records exact deterministic fixture values that resemble
  paths without weakening any scanner rule.

## Known Risks and Limitations

- The second tier requires PyPy to be resolvable by `uv tool run --python pypy`.
- The shim revision is another toolchain pin that must be maintained.
- The project dependency and standalone `ambrleaks` pins must move together.
  When adopting a new release, resolve its tag to an immutable commit and use
  that revision for both pins.
- CPython 3.14 must be resolvable by `uv` for the df12 checks.
- Pylint is intentionally focused; messages outside the selected set remain out
  of scope unless the policy is updated deliberately.
- Some existing large modules need narrow suppressions for `too-many-lines`
  until they are split by separate design work.

## Consequences

### Positive

- Contributors run one command, `make lint`, to exercise the full Python lint
  policy.
- Ruff continues to provide fast, high-signal feedback before the slower tier.
- Pylint adds checks that catch issues outside Ruff's current coverage.
- The df12 plugin enforces the shared house policy on assertions, aliases,
  suppressions, snapshots, type dispatch, and type-alias syntax.
- `ambrleaks` catches unredacted values outside Python's lintable source tree.
- The lint policy remains aligned with `leynos/episodic`.

### Negative

- The full lint target is slower than Ruff alone.
- Local machines may need `uv` to download or locate a PyPy interpreter for the
  shim.
- Toolchain updates must consider both Ruff and the shim-backed Pylint tier.

## Addendum (2026-08-31): Ruff, ty, and df12 toolchain pins

The lint estate was upgraded while preserving the two-tier decision above.
Ruff is pinned to 0.16.4 and ty is pinned to 0.0.74. The
`df12-python-lints` dependency and standalone lint pass use v0.3.0 at commit
`4cf41736cce2f7ba2778882a5c629c044568a0e5`.

- `RUFF_VERSION` and `TY_VERSION` are synchronized across the Makefile
  defaults, the workflow-level environment in `.github/workflows/ci.yml`, and
  the `pyproject.toml` development dependency group. The contract test in
  `cuprum/unittests/test_toolchain_pins.py` enforces this parity.
- `$(RUFF)` invokes `uv tool run --from 'ruff==$(RUFF_VERSION)' ruff`, and
  `$(TY)` invokes `uv tool run --from 'ty==$(TY_VERSION)' ty`, with the shared
  local tool environment. The `typecheck` target runs `$(TY) check --python
  .venv` so ty analyses the project virtual environment explicitly.
- `DF12_PYTHON_LINTS_REF` and the development dependency use the same
  immutable df12 commit. The enabled message list includes `R9112`
  (`prefer-type-statement`) so the Python 3.12-compatible codebase uses the
  modern type-alias statement syntax.
