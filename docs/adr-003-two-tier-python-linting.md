# Architectural decision record (ADR) 003: Two-tier Python linting

## Status

Accepted on 2026-05-15 and amended on 2026-07-31. Cuprum adopts Ruff as the
first lint tier, PyPy-backed Pylint for selected built-in checks, and a
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
- `PYLINT_VERSION`, pinned to a specific Pylint release; and
- `PYLINT`, the full `uv tool run` command.

The 2026-09-25 amendment below records that the `pylint-pypy-shim` this
Makefile once depended on has since been retired.

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
- The development dependency and Makefile's `DF12_PYTHON_LINTS_REF` select the
  controlled `df12-python-lints` v0.3.0 release tag.
- `ambrleaks.toml` records exact deterministic fixture values that resemble
  paths without weakening any scanner rule.

## Known Risks and Limitations

- The second tier requires PyPy to be resolvable by `uv tool run --python pypy`;
  `uv` 0.12.19 and later resolve that to PyPy 3.12.
- `PYLINT_VERSION` is a toolchain pin that must be maintained. The former
  `pylint-pypy-shim` revision pin was retired on 2026-09-25 (see the amendment
  below).
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
  Pylint tier.
- Toolchain updates must consider both Ruff and the `PYLINT_VERSION` pin that
  the PyPy-backed Pylint tier runs directly.

## Addendum (2026-08-31): Ruff, ty, and df12 toolchain pins

The lint estate was upgraded while preserving the two-tier decision above. Ruff
is pinned to 0.16.4 and ty is pinned to 0.0.74. The `df12-python-lints`
dependency and standalone lint pass use the controlled v0.3.0 release tag.

- `RUFF_VERSION` and `TY_VERSION` are synchronized across the Makefile
  defaults, the workflow-level environment in `.github/workflows/ci.yml`, and
  the `pyproject.toml` development dependency group. The contract test in
  `cuprum/unittests/test_toolchain_pins.py` enforces this parity.
- `$(RUFF)` invokes `uv tool run --from 'ruff==$(RUFF_VERSION)' ruff`, and
  `$(TY)` invokes `uv tool run --from 'ty==$(TY_VERSION)' ty`, with the shared
  local tool environment. The `typecheck` target runs
  `$(TY) check --python .venv` so ty analyses the project virtual environment
  explicitly.
- `DF12_PYTHON_LINTS_REF` and the development dependency use the same
  controlled df12 release tag. The enabled message list includes `R9112`
  (`prefer-type-statement`) so the Python 3.12-compatible codebase uses the
  modern type-alias statement syntax.

## Addendum (2026-09-04): Enforce the df12-python-lints release tag

The lint configuration now enforces the `df12-python-lints` v0.3.0 release tag
at every configured site. This keeps the standalone `make lint` command and the
development dependency on the same named release, making the effective tool
version explicit and reviewable.

- `DF12_PYTHON_LINTS_REF` in `Makefile` and the Git dependency in
  `pyproject.toml` both select `v0.3.0`.
- `cuprum/unittests/test_toolchain_pins.py` checks both references for parity
  and rejects any value other than the controlled release tag.
- The selected df12 message set, including `R9112`, is unchanged; this
  addendum records the version-selection contract rather than a lint-policy
  change.

The explicit tag keeps the two installation paths aligned, but a future release
update must change both references and the contract test together. The project
lock file should also be regenerated when the development dependency changes.

## Addendum — 2026-08-23: Skylos production dead-code stage

The original two-tier decision remains the foundation for Ruff and the
PyPy-backed Pylint checks. [ADR-004: Interrogate docstring-coverage gate]
subsequently added `interrogate`, while later addenda introduced the DF12 and
Ambrleaks stages. The effective Python lint order is now:

1. Ruff — fast, broad lint rules and docstring style.
2. `interrogate` — 100 per cent docstring presence.
3. PyPy-backed Pylint — focused selected messages.
4. `df12-python-lints` — shared Pylint rules under CPython 3.14.
5. `ambrleaks` — snapshot-secret scanning under CPython 3.14.
6. Skylos — strict production dead-code detection.

Skylos is a blocking sixth stage in `make lint`. It scans production targets,
excludes test folders, and uses the strict gate configuration in
`pyproject.toml`. Its standalone tool environment is pinned to Python 3.14 so
that Skylos parses the project's supported syntax with the intended `ast`
implementation. This addendum supersedes the original two-tier count and any
statement that `make lint` runs only Ruff and Pylint; ADR-004 remains the
decision record for the `interrogate` gate. The `skylos-allow` target uses an
ignored lock file and `flock` to serialize its read-modify-write update, so
concurrent false-positive recordings remain intact.

## Addendum — 2026-09-21: Skylos documentation liveness has a size ceiling

Skylos treats documentation as a liveness signal: a public method escapes
`SKY-U001` when its class-qualified name — `_Owner.method` — appears in a `.md`,
`.rst`, or `.txt` file under the scanned root. That signal has two ceilings,
both silent, and neither configurable in the pinned release:

- A document larger than 300000 bytes is skipped entirely, without a warning.
- Reading stops once the accumulated document total passes 2000000 bytes.

Both are engineering boundaries in Skylos rather than decisions taken here, so
this addendum records them as constraints the repository works within rather
than as policy it chose.

The practical consequence is that adding prose to a document near the per-file
ceiling can withdraw liveness credit from symbols that document has named for a
long time. `docs/developers-guide.md` is not near that ceiling but past it — it
crossed on 2026-09-21 — so the coverage timeout material this branch needed to
record lives in [Coverage timeout tiers](coverage-timeout-tiers.md) instead: a
guide that is skipped documents nothing, and the content is a CI-configuration
topic that sits naturally beside [CI cache ownership](ci-cache-ownership.md).

A `SKY-U001` reported after a docs-only change is therefore a question about
the symbol, not a finding to silence. Check which of three cases applies.

If a required runtime caller is missing, a refactor removed it: restore the
caller. That is a real defect the ceiling has merely exposed.

If the symbol is live but Skylos cannot resolve its caller — a framework
callback, a protocol implementation, or another implicit caller — verify that
and record it as an entry point in `[tool.skylos.dead_code]`, naming the caller
in the reason, as the Skylos dead-code policy in the developers' guide
describes.

If the symbol is genuinely dead, remove it.

Only a verified false positive that no entry-point record can describe reaches
`make skylos-allow`, and only once the check above has been made and its
outcome is worth naming.

## Amendment (2026-09-25): the pylint-pypy-shim is retired

CI's `uv` (0.12.19) now resolves `--python pypy` to PyPy 3.12 directly. Pylint
4.0.9 runs on PyPy 3.12 without the former `leynos/pylint-pypy-shim` patch that
Option C originally adopted, so the `Makefile` no longer defines
`PYLINT_PYPY_SHIM_REF` or `PYLINT_PYPY_SHIM`. `PYLINT_VERSION` moved from 4.0.7
to 4.0.9 as part of the same change.

This closes a real gap rather than only removing a dependency. PyPy 3.11 could
not parse some Python 3.12 syntax, and `syntax-error` is disabled in
`pyproject.toml`, so Pylint silently skipped the files it could not parse. PyPy
3.12 parses them, and the gate now covers the whole configured tree.

The historical rationale for Option C — isolating the second lint tier from the
project virtual environment and aligning with `leynos/episodic` — still holds;
only the shim's parser patch is gone. The Known Risks and Consequences sections
above now describe the direct PyPy invocation: the remaining toolchain pin is
`PYLINT_VERSION` itself, maintained like any other pinned lint tool.

[ADR-004: Interrogate docstring-coverage gate]:
  adr-004-interrogate-docstring-gate.md

## Addendum (2026-09-26): a verified PyPy runtime for the classic pass

The classic Pylint pass runs vanilla Pylint on PyPy 8.0.0's Python 3.12.14
binary. PyPy 3.12 is beta quality. `uv` does not yet catalogue that build, so
the Makefile downloads the pinned official Linux x86_64 archive, verifies its
SHA-256 digest, and passes its explicit executable path to an isolated
`uv tool run` environment. The pass verifies its interpreter and package
identities before Pylint runs, uses one worker, and stores state separately
from the CPython 3.14 DF12 pass.

The retired `pylint-pypy-shim` is absent. Pylint's `syntax-error` diagnostic is
explicitly enabled despite the focused `disable = ["all"]` configuration, so a
run that parses nothing can no longer report a clean 10.00/10. The 2026-09-25
amendment above records the same gap while describing `syntax-error` as
disabled; that described the state it was written against, where the fix was
the newer interpreter alone. This addendum additionally enables the diagnostic,
so a future parse failure is reported rather than skipped silently. The classic
target lists `cuprum/unittests`, `tests/behaviour`, `tests/features`, and
`scripts/tests` directly because Pylint does not recurse into those non-package
directories from their broad roots. Python 3.12 remains Cuprum's source
baseline; CPython 3.14 is only the execution interpreter for DF12 and
Ambrleaks, including any tooling that requires newer syntax.

Pylint 4.0.9's published Astroid range excludes Astroid 4.3.1, so the latter
upgrade remains deferred pending a released compatible Pylint version. The
integration contract exercises PEP 695 parsing, syntax failures, enabled
diagnostics, real PyPy descriptor inspection, DF12 isolation, and failure
propagation without changing the project virtual environment.

The `leynos/pylint-pypy-shim` retirement described in the amendment above and
this verified-runtime work landed independently: the amendment removed the shim
by moving to a `uv`-catalogued PyPy, while this addendum pins the runtime
explicitly. Both are retained here. The explicit pin supersedes the
catalogue-dependent invocation for the classic pass, because `uv`'s
`--python pypy` resolves to whichever PyPy release the catalogue currently
prefers, and the pass must fail loudly rather than silently lint under an older
interpreter. `PYLINT_VERSION` is shared by both tiers and is not duplicated.

This supersedes the Known Risks bullet that anticipates "narrow suppressions for
`too-many-lines`". The 400-line limit now applies without exemption to
production and benchmark modules: the six modules that previously needed local
suppressions were decomposed instead — `cuprum/sh` and `cuprum/_line_stream`
into packages, and `benchmarks/tee_profile_worker`, `cuprum/_streams`,
`cuprum/adapters/metrics_adapter`, and `cuprum/context/core` into extracted
sibling modules — so the limit is enforced by structure rather than by comment.
The classic pass runs the non-package test roots separately with only
`too-many-lines` disabled: 34 existing unit-test modules exceed the limit and
will be split in follow-up work. All other selected classic diagnostics remain
blocking in those modules, so the exception does not hide parse or analysis
failures.

The classic pass owns complete Python source coverage. DF12 retains its
established package-root target discovery and continues to run only its
configured house-policy messages on CPython 3.14; it does not replace classic
coverage of the directly targeted non-package roots.

Ambrleaks also runs with an isolated CPython 3.14 environment, so neither
Pylint pass nor snapshot scanning recreates Cuprum's project virtual
environment.
