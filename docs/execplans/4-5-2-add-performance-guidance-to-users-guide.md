# Add performance guidance to the users' guide (4.5.2)

This ExecPlan (execution plan) is a living document. The sections
`Constraints`, `Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`,
`Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work
proceeds.

Status: DRAFT

Roadmap reference: `docs/roadmap.md` item `4.5.2`.

## Purpose / big picture

Roadmap item `4.5.2` requires consumer-facing guidance in
`docs/users-guide.md` that tells users when to choose the pure Python pathway
or the optional Rust pathway, how to configure selection with
`CUPRUM_STREAM_BACKEND`, and what throughput improvement they should expect in
practice.

This is not just a prose tidy-up. The users' guide is currently close to the
goal, but the guidance is spread across several subsections, and there is a
material mismatch between the design document and the current implementation:
`docs/users-guide.md` says stdout and stderr capture still uses the Python
consume path, while `docs/cuprum-design.md` still describes Rust consume-path
benefits. This task is complete only when the documentation is internally
consistent, reflects the shipped behaviour, and the documented consumer-facing
claims are backed by unit and behavioural tests.

After this work, a user should be able to:

1. Read one clear section in `docs/users-guide.md` and decide whether to leave
   backend selection on `auto`, force `python`, or force `rust`.
2. Understand that backend selection must be configured before first backend
   resolution in a process because the result is cached.
3. Know that current backend selection affects inter-stage pipeline pumping,
   not stdout and stderr capture.
4. See conservative throughput expectations for large versus small workloads,
   with explicit direction to run the benchmark commands on representative
   workloads.
5. Trust that the guide, design document, roadmap, and tests all describe the
   same behaviour.

This task is complete only when:

- `docs/users-guide.md` contains explicit performance guidance for pathway
  choice, environment configuration, and expected throughput improvements;
- `docs/cuprum-design.md` records any design clarification needed to reconcile
  the guide with the current code;
- `docs/roadmap.md` marks item `4.5.2` as done;
- unit tests (`pytest`) and behavioural tests (`pytest-bdd`) cover the
  documented consumer-visible behaviour that the guide depends on;
- quality gates pass:
  `make check-fmt`, `make typecheck`, `make lint`, `make test`,
  `make markdownlint`, and `make nixie`.

## Constraints

- Keep scope bounded to roadmap item `4.5.2`. This is a documentation-led task.
  Do not redesign stream backend architecture or benchmark CI behaviour.
- Treat pure Python as a first-class pathway in the final documentation. The
  guide must not imply that Rust is mandatory or universally better.
- Use the shipped implementation as the source of truth for consumer-facing
  behaviour. If the design document is aspirational or stale, update the design
  document to match the implementation rather than editing the guide to match
  outdated text.
- Follow repository test-first policy:
  add or modify tests first, confirm a failing state, then change the docs and
  any minimal support code needed to satisfy the tests.
- Provide both test styles:
  - unit tests in `cuprum/unittests/` with `pytest`;
  - behavioural tests in `tests/behaviour/` plus Gherkin in
    `tests/features/` using `pytest-bdd`.
- Do not introduce new runtime or documentation tooling dependencies.
- Keep performance claims conservative and evidence-based. Do not promise exact
  speedups that are not supported by existing benchmark infrastructure.
- Keep documentation aligned across:
  `docs/users-guide.md`, `docs/cuprum-design.md`, and `docs/roadmap.md`.
- Follow `docs/documentation-style-guide.md`, especially:
  British English, 80-column paragraph wrapping, explicit code block language
  tags, and sentence-case headings.
- Keep any Python changes compliant with `.rules/python-*.md`, Ruff, and type
  checks.

## Tolerances (exception triggers)

- Scope: if implementation requires edits to more than 8 files, including this
  ExecPlan, stop and escalate.
- Runtime behaviour: if completing this roadmap item requires substantive
  production code changes outside test hooks or documentation examples, stop
  and escalate with options. The expected change should be mostly docs and
  tests.
- Evidence: if no stable benchmark evidence exists for a numeric throughput
  range, stop and use qualitative wording only rather than inventing numbers.
- Documentation conflict: if the code path for stream capture versus pumping is
  ambiguous after inspection, stop and resolve the behaviour before editing the
  guide.
- Test design: if the only way to validate the documented behaviour is through
  timing-sensitive performance assertions in the default test suite, stop and
  design a deterministic contract test instead.
- Iterations: if the same failing test persists after 3 fix attempts, stop and
  escalate with the observed failure and candidate fixes.

## Risks

- Risk: the users' guide already contains much of the required material, so a
  naive update could duplicate or contradict existing guidance instead of
  clarifying it. Severity: high. Likelihood: high. Mitigation: consolidate the
  current scattered notes into one explicit "when to use each pathway" section
  and remove redundant wording nearby.

- Risk: the design document currently overstates Rust consume-path usage
  relative to the implementation. Severity: high. Likelihood: high.
  Mitigation: inspect the code path once, document the finding, and update
  `docs/cuprum-design.md` to match the actual behaviour.

- Risk: performance guidance can become marketing language if it uses broad or
  stale speedup claims. Severity: medium. Likelihood: medium. Mitigation: tie
  throughput wording to the existing benchmark suite and comparison-report
  workflow, and frame numbers as workload-dependent expectations rather than
  guarantees.

- Risk: users may misread backend selection as a dynamic per-call toggle, even
  though resolution is cached after first use. Severity: medium. Likelihood:
  medium. Mitigation: include an explicit environment-variable example and a
  warning that the variable must be set before first backend resolution in the
  current process.

- Risk: tests may already cover some documented behaviour, but not the exact
  consumer-facing claim the guide makes. Severity: medium. Likelihood: medium.
  Mitigation: add only the smallest missing unit and behavioural assertions,
  preferring existing test modules over new test infrastructure.

## Progress

- [x] (2026-03-25 00:00Z) Reviewed roadmap item `4.5.2`, the execplans skill,
  the users' guide, the design document, ADR-001, and neighbouring ExecPlans.
- [x] (2026-03-25 00:15Z) Confirmed the current implementation path in code:
  backend selection controls inter-stage pumping, while stdout and stderr
  capture still goes through `_consume_stream(...)` in Python.
- [x] (2026-03-25 00:20Z) Identified a documentation mismatch between
  `docs/users-guide.md` and `docs/cuprum-design.md` around Rust consume-path
  usage.
- [x] (2026-03-25 00:30Z) Drafted this ExecPlan.
- [ ] Stage A: add or adjust fail-first unit and behavioural tests for the
  consumer-visible backend-selection and capture behaviour that the guide will
  document.
- [ ] Stage B: update `docs/users-guide.md` to provide a clear decision
  framework, configuration examples, and conservative throughput expectations.
- [ ] Stage C: update `docs/cuprum-design.md` to record the final clarified
  design statements and remove outdated claims.
- [ ] Stage D: mark `docs/roadmap.md` item `4.5.2` done.
- [ ] Stage E: run full validation and record results.

## Surprises & discoveries

- Observation: `docs/users-guide.md` already documents
  `CUPRUM_STREAM_BACKEND`, fallback rules, and benchmark commands, but the
  explanation is fragmented across availability, backend selection, benchmark,
  and CI sections. Impact: this task should reorganize and sharpen existing
  guidance, not start from scratch.

- Observation: `docs/users-guide.md` says stream consumption currently always
  uses the Python pathway, while `docs/cuprum-design.md` section 13.5 still
  describes Rust consume-path benefits. Impact: `4.5.2` must reconcile the
  docs before it can be marked done.

- Observation: the implementation confirms the users' guide statement.
  `cuprum/_pipeline_streams.py` creates stdout and stderr capture tasks with
  `_consume_stream(...)`, and `_consume_stream(...)` is implemented in
  `cuprum/_streams.py`. Impact: the design document, not the guide, is the
  stale source here.

- Observation: benchmark infrastructure already exists locally and in CI:
  `make benchmark-micro`, `make benchmark-e2e`, the ratchet report, and the
  Python-versus-Rust comparison summary. Impact: performance guidance should
  point readers at these existing measurement paths instead of inventing a new
  workflow.

## Decision log

- Decision: treat roadmap item `4.5.2` as a documentation-and-contract task,
  not a new runtime feature. Rationale: the required backend-selection
  mechanics and benchmark workflow are already implemented. Date/Author:
  2026-03-25 / Codex.

- Decision: resolve the design/users-guide conflict in favour of the current
  implementation unless tests reveal otherwise. Rationale: the guide must
  describe what users get today, and the code inspection currently supports the
  guide's statement that capture remains Python-based. Date/Author:
  2026-03-25 / Codex.

- Decision: prefer conservative, workload-dependent throughput wording over a
  hard-coded global speedup claim. Rationale: benchmark results vary by payload
  size, pipeline depth, platform, and whether Linux `splice()` is active.
  Date/Author: 2026-03-25 / Codex.

## Outcomes & retrospective

Pending. This is a draft ExecPlan; implementation has not started.

Success for the eventual implementation should look like this:

- a reader can choose `auto`, `python`, or `rust` without cross-reading the
  design document;
- the users' guide clearly states that current Rust acceleration applies to
  inter-stage pumping and not stdout or stderr capture;
- any throughput expectation is explicitly qualified and linked to benchmark
  commands and CI comparison artefacts;
- the roadmap item is checked off only after tests and quality gates pass.

## Context and orientation

Relevant current files and behaviour:

- `docs/users-guide.md`
  - already documents Rust availability, `CUPRUM_STREAM_BACKEND`, benchmark
    commands, and CI comparison reports;
  - does not yet present this as one crisp decision-making workflow for a user.

- `docs/cuprum-design.md`
  - contains section 13 for Rust extension architecture and performance;
  - currently contains at least one stale statement about Rust consume-path
    benefits that conflicts with the shipped implementation.

- `docs/adr-001-rust-extension.md`
  - records the architectural rationale for optional Rust acceleration;
  - provides useful context for why pure Python must remain first-class.

- `cuprum/_backend.py`
  - resolves `CUPRUM_STREAM_BACKEND` with `auto`, `python`, and `rust`
    semantics, including caching and strict `ImportError` when Rust is forced
    but unavailable.

- `cuprum/_pipeline_streams.py`
  - dispatches inter-stage pumping through the selected backend;
  - still constructs stdout and stderr capture tasks with `_consume_stream(...)`
    from `cuprum/_streams.py`.

- `cuprum/_streams.py`
  - provides the current Python capture and decode path for stdout and stderr.

- Existing tests worth extending before creating new files:
  - `cuprum/unittests/test_backend.py`
  - `cuprum/unittests/test_pipeline_stream_backend_selection.py`
  - `tests/features/backend_dispatcher.feature`
  - `tests/behaviour/test_backend_dispatcher_behaviour.py`
  - `tests/features/stream_backend_pipeline.feature`
  - `tests/behaviour/test_stream_backend_pipeline.py`

Gap summary:

- Already implemented:
  backend selection, fallback behaviour, benchmark commands, benchmark CI
  reporting, and public Rust-availability probe.

- Still missing for roadmap `4.5.2`:
  a single, authoritative users-guide explanation of when to use each pathway,
  how to set the environment variable safely, and what throughput improvement a
  user should expect from large workloads.

## Plan of work

Stage A: add fail-first tests for the documented behaviour.

Before changing the guide, inspect current unit and behavioural coverage and
extend it only where the final documentation would otherwise outrun the test
suite. The target here is not "test the prose"; it is "test the consumer
behaviour that the prose is promising."

Go/no-go: proceed only after the new or adjusted tests fail in a way that
demonstrates a real documentation gap or missing behaviour assertion.

Stage B: rewrite the users' guide section around user decisions.

Create or reshape a section in `docs/users-guide.md` so a user can answer three
questions quickly:

1. When should I leave `auto` alone versus forcing `python` or `rust`?
2. How exactly do I configure `CUPRUM_STREAM_BACKEND`, and when must I set it?
3. What improvement should I expect for large throughput-heavy workloads, and
   when should I measure instead of guessing?

This stage should replace scattered explanation with a tighter narrative, not
append more redundant paragraphs.

Go/no-go: proceed only when the guide is clear without requiring the reader to
jump into the design document.

Stage C: reconcile and record design decisions.

Update `docs/cuprum-design.md` where the architecture text is stale or
ambiguous, especially around whether Rust currently accelerates consume-path
capture. If the final users-guide wording introduces a clearer rule of thumb or
clarifies a limit that belongs in the design document, record it there.

Go/no-go: proceed only when the guide and the design document make the same
claims about current runtime behaviour.

Stage D: complete roadmap and validate.

After docs and tests are aligned, mark roadmap item `4.5.2` done in
`docs/roadmap.md` and run the full validation suite, including Markdown checks.

Do not mark the roadmap item done until every required gate passes.

## Concrete steps

1. Confirm the exact user-visible contract to document.

   Inspect these code paths once and treat them as the implementation truth:

   - `cuprum/_backend.py`
   - `cuprum/_pipeline_streams.py`
   - `cuprum/_streams.py`
   - `benchmarks/pipeline_throughput.py`
   - `benchmarks/python_vs_rust_comparison_report.py`

   The implementation work should explicitly answer:

   - what `auto`, `python`, and `rust` do;
   - when `ImportError` is raised;
   - when fallback to Python is automatic;
   - whether stdout and stderr capture is backend-controlled today;
   - what benchmark evidence is already available for throughput guidance.

2. Add or adjust unit tests first.

   Preferred files:

   - `cuprum/unittests/test_backend.py`
   - `cuprum/unittests/test_pipeline_stream_backend_selection.py`

   Target assertions:

   - backend environment variable precedence is explicit and cached;
   - forced Rust still raises `ImportError` when unavailable;
   - pipeline pumping can fall back to Python when Rust pumping is infeasible;
   - stdout and stderr capture behaviour documented in the guide is backed by a
     unit-level assertion, especially if the final guide says capture still
     remains on the Python path.

3. Add or adjust behavioural tests next.

   Preferred files:

   - `tests/features/backend_dispatcher.feature`
   - `tests/behaviour/test_backend_dispatcher_behaviour.py`
   - `tests/features/stream_backend_pipeline.feature`
   - `tests/behaviour/test_stream_backend_pipeline.py`

   Target scenarios:

   - a user can force `python` or `rust` through
     `CUPRUM_STREAM_BACKEND`;
   - forced Rust fails loudly when the extension is unavailable;
   - the selected backend does not change observable pipeline output;
   - any documented fallback or capture caveat is demonstrated in an
     end-to-end scenario rather than left as an untested note.

4. Run targeted tests and capture the red phase.

```bash
set -o pipefail
uv run pytest -q cuprum/unittests/test_backend.py \
  cuprum/unittests/test_pipeline_stream_backend_selection.py \
  2>&1 | tee /tmp/4-5-2-targeted-unit-pre.log
```

```bash
set -o pipefail
uv run pytest -q tests/behaviour/test_backend_dispatcher_behaviour.py \
  tests/behaviour/test_stream_backend_pipeline.py \
  2>&1 | tee /tmp/4-5-2-targeted-bdd-pre.log
```

   Expected before documentation updates:
   a new or adjusted assertion should fail if the documented contract is not
   already covered correctly.

1. Update `docs/users-guide.md`.

   The final guide should likely include:

   - a short "choose a backend" subsection or decision table;
   - a shell example showing
     `CUPRUM_STREAM_BACKEND=python|rust|auto`;
   - a note that the environment variable must be set before first backend
     resolution in the process;
   - an explicit statement that current Rust acceleration applies to inter-stage
     pumping, while stdout and stderr capture remains Python-based;
   - conservative throughput guidance, such as:
     small outputs often show negligible difference, while large multi-stage
     transfers can see substantial improvements and should be measured with
     `make benchmark-e2e`.

2. Update `docs/cuprum-design.md`.

   Reconcile section 13.5 and nearby text so the design document no longer
   implies that Rust consume-path acceleration is active if it is not.

   If this task introduces a sharper rule of thumb for users, record that rule
   in the design document as a design clarification rather than leaving it only
   in the guide.

3. Mark roadmap item `4.5.2` done.

   Update `docs/roadmap.md` only after the guide, design document, and tests
   all reflect the same final behaviour.

4. Run full validation.

```bash
set -o pipefail
make check-fmt 2>&1 | tee /tmp/4-5-2-check-fmt.log
```

```bash
set -o pipefail
make typecheck 2>&1 | tee /tmp/4-5-2-typecheck.log
```

```bash
set -o pipefail
make lint 2>&1 | tee /tmp/4-5-2-lint.log
```

```bash
set -o pipefail
make test 2>&1 | tee /tmp/4-5-2-test.log
```

```bash
set -o pipefail
MDLINT=/root/.bun/bin/markdownlint-cli2 make markdownlint \
  2>&1 | tee /tmp/4-5-2-markdownlint.log
```

```bash
set -o pipefail
make nixie 2>&1 | tee /tmp/4-5-2-nixie.log
```

1. Record the final outcome in this ExecPlan.

   Update:

   - `Progress`
   - `Surprises & Discoveries`
   - `Decision Log`
   - `Outcomes & Retrospective`

   Include the actual validation result summary and any wording trade-offs made
   for throughput expectations.
