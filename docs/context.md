# Cuprum – domain context and design gaps

**Status:** Draft v0.1 companion to
[docs/terms-of-reference.md](terms-of-reference.md).
**Audience:** Maintainers, downstream integrators, and architectural reviewers.
**Assessed:** 26 September 2026, against [Cuprum commit][baseline]
`991dee6429ccd415df4cc184e8d558c68d4ccf92`.

This document defines the shared domain language, system boundary, and
baseline-to-target gaps. It follows the context role identified by the
[terms-of-reference skill][skill]. It is not the API reference, a replacement
for the design, or a claim that every requested feature belongs in scope.

## 1. Document responsibilities and evidence

| Document | Authority and purpose |
| --- | --- |
| [docs/terms-of-reference.md](terms-of-reference.md) | Reconstructed problem, audience, goals G-01–G-08, constraints C-01–C-05, success criteria S-01–S-08, and unresolved scope decisions Q-01–Q-08. The charter remains draft pending ratification. |
| `docs/context.md` | Shared terminology, boundaries, assessed baseline, and gap-to-requirement traceability. |
| [docs/cuprum-design.md][design] and [ADRs](contents.md) | Intended architecture, contracts, rationale, and recorded decisions. Conflicts require explicit reconciliation, not silent historical edits. |
| [docs/users-guide.md][guide] | Consumer workflows, API behaviour, platform qualifications, and operational reference. |
| [docs/roadmap.md][roadmap] | Delivery sequencing and task acceptance. An open feature issue does not automatically become a committed roadmap requirement. |
| [docs/execplans/](execplans/) | Scoped execution records, when separately commissioned, including acceptance evidence and deviations. |

_Table 1: Responsibilities of the documentation set._

'Baseline' below means the inspected default-branch revision, not the latest
release and not an open pull request. Some issue reports describe probes at
older commits; those reports are attributed, not represented as freshly
reproduced results. In particular, older issues link to `cuprum/sh.py`, while
the assessed tree uses the [cuprum/sh/ package][sh-package].

The charter supplies product intent; source and tests establish current
behaviour within their inspected or executed scope. Neither a requirement nor
a green badge establishes an unexamined guarantee. When they disagree, record
a gap. The commissioning instruction's no-compatibility rule, C-04, is binding
input, even though the broader reconstructed charter awaits ratification.

## 2. System boundary and stakeholder views

Cuprum sits inside a Python application between its command intent and local
operating-system process/stream facilities. The application owns why a tool
runs, which tools and inputs are trusted, and what their results mean. Cuprum
owns its declared construction, execution, stream, and observation contracts.
The host owns permissions and isolation. This boundary follows the
[design's local-runner scope][design] and the [guide's policy contracts][guide].

| Interaction | Information or resource crossing the boundary | Responsibility and limitation |
| --- | --- | --- |
| Application to catalogue/builder | Program identity, arguments, and project metadata. | The application approves tools and supplies domain validation. Catalogue membership does not certify the binary or every possible argument. |
| Application to execution | Command, scope, working directory, environment settings, output options, and deadline. | Cuprum applies its documented policy. The application must not assume an environment overlay removes inherited variables. |
| Runtime to operating system | Argument vector, standard streams, process identity, and termination requests. | The host resolves and runs the executable. Direct-child ownership does not imply descendant containment. |
| Child to runtime | Output bytes and termination status. | Capture, presentation, and observation have different contracts. Child-controlled text is not trusted log-control syntax. |
| Runtime to hooks and sinks | Execution metadata, decoded lines, measurements, and presentation sessions. | Consumers select destinations and information policy. Hook failure, blocking behaviour, and secret exposure require explicit contracts. |
| Runtime to optional native backend | Stream resources and a supported operation. | Eligibility and ownership must be established at the boundary; having the extension installed does not make every operation native. |
| Maintainer to distribution/CI systems | Package artefacts, verification jobs, and release evidence. | Installation support and actual contract execution must be demonstrated independently of source-tree success. |

_Table 2: External interactions and ownership boundaries._

### 2.1 Meaning of safety

A `Program` or `SafeCmd` is not a security capability issued by the operating
system. An approved interpreter can execute code, and an approved tool can
interpret arguments, read files, or contact a remote service. Avoiding shell
parsing removes that particular interpretation step; it does not validate all
tool options, pin executable contents, or sandbox the child. Q-02 governs any
stronger identity, environment, or information-boundary requirement.

### 2.2 Concern-driven views

The requested [TOGAF discussion][discussion] motivates selecting views by
stakeholder concern, not adding diagrams for their own sake.

| Concern and stakeholder | View required for review | Existing evidence and missing piece |
| --- | --- | --- |
| Automation author: what may run? | Command construction, catalogue, and scope boundary. | Guide and design; GAP-01, GAP-02, and GAP-03 qualify typing, executable identity, and environment control. |
| Runner maintainer: who owns unfinished work? | Process/stream lifecycle and failure outcomes. | Runtime contracts and roadmap; GAP-04 and GAP-05 qualify descendants and terminal observation. |
| Operator: what happened, and what was disclosed? | Output routing, observation channels, and projection boundary. | Guide and presentation design; GAP-06–GAP-08 identify resource and information-policy gaps. |
| Native maintainer: what does the fast path prove? | Eligibility, resource ownership, fallback, and verification envelope. | ADR-001/002 and boundary inventory; GAP-09 and GAP-10 separate implemented helpers from integrated, verified paths. |
| Scope owner: can this increment be accepted? | Requirement, design, test, consumer, and residual-gap traceability. | Charter and gap register; GAP-11–GAP-13 identify policy, evidence, and prioritization work. |

_Table 3: Stakeholder concerns determine the required architectural views._

## 3. Ubiquitous language

These definitions apply across the charter, design, roadmap, and reviews.
[docs/users-guide.md][guide] remains the operational reference. Describing a
requested term does not make the corresponding capability available.

### 3.1 Command and policy language

| Term | Meaning in Cuprum | Distinction that must survive design changes |
| --- | --- | --- |
| Program | A nominal executable identifier represented by `Program`. | At the baseline, its string also identifies what is launched; separate logical identity and executable binding are GAP-02. |
| Catalogue | Explicit collection of programs and associated project metadata. | It defines known command-building choices; it is not automatic search-path discovery. |
| Project settings | Catalogue metadata grouping programs, documentation locations, and noise rules. | Noise rules are stored for downstream use, not automatically applied by Cuprum. |
| Builder | A callable constructing a command's argument vector for an approved program. | A generic builder is not a validator for every tool's argument semantics. |
| Argument vector | Ordered program arguments passed without shell interpretation. | One value containing spaces remains one argument; a keyword flag is not necessarily a boolean presence switch. |
| Command | A description of intended execution, represented by `SafeCmd`. | Building it does not spawn the process. A command is not the same as one execution attempt. |
| Allowlist | The set of program identities permitted by an applicable policy. | Matching a basename is not approval of an arbitrary executable path. |
| Scope | A logically bounded policy context that may narrow permissions and supply execution defaults or hooks. | Logical task isolation does not freeze all process-global state or make the host a sandbox. |
| Execution context | Per-call settings such as working directory, environment overlay, and sinks. | `ExecutionContext`, scoped `CuprumContext`, and this domain context document are three different concepts. |
| Environment overlay | Values layered over the live inherited environment. | An empty overlay is not an empty environment; omission does not delete an inherited value. |

_Table 4: Command and policy vocabulary, grounded in the guide and source._

### 3.2 Lifetime and stream language

| Term | Meaning in Cuprum | Distinction that must survive design changes |
| --- | --- | --- |
| Execution attempt | One invocation of a command, including attempts that fail to spawn. | Its identity is not interchangeable with a reusable operating-system process identifier (PID). |
| Managed child | A process directly launched and owned by the execution operation. | A grandchild is not automatically owned merely because a managed child created it. |
| Pipeline stage | A command participating in a pipeline. | Interior stdout feeds the next stage; it is not ordinary parent-facing output. |
| Pipeline | Local stream-connected stages with per-stage outcomes and a documented fail-fast policy. | It is not a shell program, task scheduler, or distributed dependency graph. |
| Timeout | Expiry of the selected execution deadline, triggering documented cleanup and timeout reporting. | It is not a promise of immediate return despite blocked user code, slow sinks, or unowned descendants. |
| Cancellation | Caller-requested interruption, with cleanup obligations for owned work. | Silently swallowing cancellation is not successful execution. |
| Capture | Retaining output for a result or timeout report. | Baseline result capture is decoded text and complete in memory, not a byte-preserving or bounded-retention API. |
| Echo | Mirroring child output to parent-facing sinks. | Per-stream echo controls and echoed-line limits do not bound captured results. |
| Line observation | Delivery of decoded lines to a callback or the single-command line iterator. | Ordering is per stream, not a total order across stdout and stderr. |
| Backpressure | A producer waiting for a consumer to accept more data. | A bounded line queue or inter-stage pump does not prove bounded result retention or non-blocking presentation. |
| Idle heartbeat | Parent-side notification that no relevant child output has been observed for an interval. | It is not child progress, deadlock detection, captured child output, or permission to extend a deadline. |
| Presentation session | A sink-owned framing lifetime, such as a GitHub Actions log group. | Closing a session does not establish a complete public execution-event lifecycle. |

_Table 5: Process lifetime and stream vocabulary._

### 3.3 Observation, acceleration, and delivery language

| Term | Meaning in this documentation | Distinction that must survive design changes |
| --- | --- | --- |
| Execution event | Structured execution observation, distinct from pump, echo, stream-operation, or proposed idle channels. | The channels have different schemas and failure policies; not every hook is failure-isolated. |
| Projection | A representation of execution information for logs, traces, metrics, or presentation. | Sanitizing a projection must not change executed arguments, the child environment, or captured results. |
| Redaction | Explicitly configured removal or transformation of sensitive information at a projection boundary. | GAP-08 is an opt-in policy proposal, not a promise to discover arbitrary secrets. |
| Backend | Python or optional Rust implementation of an eligible stream operation. | Selection is operation-specific; a global preference is not universal native capability. |
| Pump | Transfer between connected stream endpoints, including pipeline stages. | Native pumping and parent-side capture consumption are separate operations. |
| Consume | Read and decode parent-facing output for capture or observation. | The native consume helper exists but is not integrated into production dispatch at the baseline. |
| Baseline | An identified, inspectable repository state and its evidence. | A roadmap checkbox, branch, release tag, and verified distribution are not interchangeable baselines. |
| Target | An explicitly accepted outcome or contract. | An issue proposing a feature is not by itself an accepted target. |
| Gap | A difference between the baseline and an accepted target, or a decision/evidence deficit preventing that comparison. | Missing implementation, unresolved scope, and missing evidence require different closure actions. |
| Plateau | A coherent, validated intermediate repository state for an accepted increment. | It need not retain an earlier API shape; C-04 forbids compatibility scaffolding in its excluded cases. |
| Compatibility machinery | Code retained solely to support a historical interface or behaviour. | A purposeful current adapter, public entrypoint, or capability fallback is not automatically compatibility machinery. |

_Table 6: Observation, backend, and governance vocabulary._

## 4. Assessed baseline

The following statements summarize inspected documentation and selected source
paths. They are not a fresh execution of the acceptance suite.

| Area | Baseline and evidence | Limit on the claim |
| --- | --- | --- |
| Construction and policy | The [guide][guide] documents catalogue-backed builders, unknown-program rejection, scoped allowlists, and typed Git/rsync/tar helpers. | The generic `SafeCmdBuilder` remains `Callable[..., SafeCmd]` in [safe_cmd.py][safe-cmd]; packaged typing is also under review. |
| Runtime | Async execution, sync convenience, timeouts, direct-child termination/escalation, pipelines, and bounded command concurrency are documented in the [guide][guide] and [roadmap][roadmap]. | No general managed process handle or descendant-containment contract is established. |
| Environment | [env_overlay.py][env-source] resolves non-empty overlays over the live parent environment and treats all-empty layers as inheritance. | No replacement or explicit deletion mode exists in this baseline. |
| Output | [output.py][output-source] and the guide separate capture, per-stream echo, lines, idle notification, and presentation. | Results are decoded text; capture is not bounded by the echoed-line limit. Ordinary synchronous sinks can block the execution loop. |
| Line iteration | The guide documents `SafeCmd.lines()` with owned cleanup through an async context manager or `aclose()`. | Breaking iteration alone does not settle the child. Pipelines expose `on_line`, not a `lines()` method. |
| Observation | The design and guide describe execution events, hooks, and logging/metrics/tracing adapters; separate diagnostic channels exist. | Complete terminal accounting, common redaction, and uniformly isolated observer disposal are not established. |
| Presentation | The opt-in GitHub Actions sink frames output independently of capture. [output.py][output-source] warns against overlapping grouped runs sharing parent stderr. | Bounded command concurrency does not imply safe interleaving of presentation frames. Encoding fallback does not imply general sink-failure recovery. |
| Acceleration | Native inter-stage pumping exists. [The stream bridge][stream-source] explicitly marks `rust_consume_stream()` as implemented but not integrated. | Installing a native wheel does not accelerate every parent-side capture operation. |
| Verification | The [repository layout][layout] separates the PyO3 integration, safe stream policy, and audited native-I/O crates; [the verification inventory][verification] records their evidence. | Tool-specific assumptions, platform exclusions, selected targets, and actual CI collection limit what any verifier establishes. |
| Release state | [Project metadata][metadata] declares `0.2.0-beta1`, Python 3.12+, and no runtime dependencies. | This does not establish the latest published version or successful validation of every installation route. |

_Table 7: Current architecture and the limits of its evidence._

## 5. Principles and protected constraints

Principles guide choices; constraints prohibit choices. The following
principles are distilled from [design §4][design] and the requested
[TOGAF discussion][discussion]. They do not soften C-01–C-05 in the charter.

| ID | Principle | Review consequence |
| --- | --- | --- |
| P-01 | Explicit command intent and policy. | Avoid implicit discovery or permissive identity substitution to make adoption easier. |
| P-02 | Static structure backed by runtime enforcement. | Test installed consumer typing as well as runtime behaviour; a type annotation alone is insufficient evidence. |
| P-03 | Owned lifetimes and explicit failure boundaries. | Name what is owned, how it settles, and what happens when cleanup or observers fail. |
| P-04 | Observation follows information and resource policy. | Separate raw observation, exported projections, capture, and presentation; state cost and failure semantics. |
| P-05 | Optimize only a measured, semantically equivalent path. | Keep the Python baseline useful; record negative optimization results rather than weaken acceptance gates. |
| P-06 | Trace intent through implementation to acceptance. | Identify satisfied requirements, reused components, tests, assumptions, and remaining gaps. |

_Table 8: Architectural principles and their concrete review consequences._

## 6. Design gaps

The register distinguishes **contract**, **scope**, **evidence**, and
**conformance** gaps. A contract gap concerns an intended guarantee not fully
specified or delivered. A scope gap needs a product decision before its target
can be assumed. An evidence gap must not be described as a reproduced runtime
defect. A conformance gap requires documents or existing decisions to be
reconciled with governing constraints.

All referenced pull requests were open when inspected on 26 September 2026.
They are implementation or planning leads, not delivered baseline behaviour.
Closure requires merged code or a recorded scope decision, reconciled
consumer-facing documentation, and the stated acceptance evidence. An issue's
open state alone does not prove its original report still matches the code.

### GAP-01. Consumer-visible typing

**Contract/evidence; G-01, G-06; S-01.** The generic builder's ellipsis callable
hides its argument domain from consumer type checkers, despite the design's
static-first intent. [Issue #483][i483] and [PR #502][p502] cover that contract;
[PR #501][p501] separately addresses distribution of the `py.typed` marker.
These are different problems from optional attribute-style command typing.

**Closure:** installed-distribution positive and negative typing examples,
runtime argument-domain tests, and packaging evidence across supported build
routes. A source-tree typecheck alone does not close the gap. Proposed owner:
API and release maintainers.

### GAP-02. Program identity and executable authority

**Scope/contract; G-01, G-05; C-02, C-03; Q-02.** At the baseline, program
identity also names the executable. A validated configured path can be
registered, but a separate logical identity-to-executable binding is not
specified; [#440][i440] records the distinction. No basename-equivalence
workaround is acceptable.

**Closure:** accept or defer the separate binding requirement; if accepted,
define validation authority, scope lifetime, path resolution, telemetry, and
filesystem-replacement limits. Tests must reject an unapproved same-basename
path and preserve nested/concurrent isolation. Proposed owner: policy
maintainer with a downstream reviewer.

### GAP-03. Environment replacement and deletion

**Contract/adoption; G-05, G-08; Q-02.** Current overlays cannot express an
empty
replacement environment or remove inherited credentials/settings. The guide
and [env_overlay.py][env-source] make the baseline clear; [#434][i434] requests
the additional contract and [PR #466][p466] proposes it.

**Closure:** explicit inheritance, overlay, replacement, and unset semantics;
precedence across scopes and calls; single-command/pipeline tests; and proof
that rendering leaves the parent environment unchanged. Proposed owner:
execution-policy maintainer. The request is not permission to bypass policy
through an additional bootstrap executable.

### GAP-04. Extent of process ownership

**Scope; G-02, G-08; N-06; Q-04.** Direct-child cancellation exists, but an
independently controlled running handle ([#437][i437]) and optional descendant
cleanup ([#438][i438]) are not part of that guarantee. The descendant issue is
a source-backed missing contract, not a reproduced leak or a claim that
direct-child teardown is broken.

**Closure:** first decide the ownership boundary. Any accepted extension needs
startup, early-exit, repeated termination, partial-pipeline startup, retained
pipes, cancellation-during-cleanup, and platform-specific acceptance cases.
State that descendants escaping a process group are outside that group's
containment. Proposed owner: scope owner and runtime maintainer.

### GAP-05. Complete outcomes and observation failure policy

**Contract; G-02, G-04; S-03; Q-05.** [#441][i441] reports missing terminal
execution outcomes for spawn failure and cancellation. Presentation-session
cleanup does not supply that public event contract. [#424][i424] requests a
separate failure-isolated idle channel; [#485][i485] reports exceptions while
disposing echo/stream observer coroutines that can skip subsequent observers.

**Closure:** specify observation entry and exactly one correlated terminal
outcome for every observed attempt, keeping pre-observation rejection distinct.
Cover command and pipeline startup failures, timeout, repeated cancellation,
and failing hooks. Preserve primary exceptions and define each channel's
failure policy; do not silently extend a closed event vocabulary or assume all
hooks are fail-open. Proposed owner: observability and runtime maintainers.

### GAP-06. Workload bounds and stream modes

**Scope/contract; G-03, G-05, G-08; S-04; Q-03.** Echo limits and line-queue
backpressure do not bound retained results. [#443][i443] proposes per-stream
bounded/file-backed capture; [#444][i444] proposes typed byte-preserving
results; [#445][i445] proposes streaming input and explicit file bindings.
Separately, [#436][i436] addresses blocking output delivery on the execution
loop. A bytes stdin option is not bytes output, and another task on the same
loop does not isolate a blocked synchronous sink.

**Closure:** accept the required workload envelope before choosing APIs. For
accepted modes, specify byte accounting, overflow/backpressure, decoding,
ordering, storage/descriptor ownership, timeout partial output, and cleanup.
Exercise a slow child, blocked sink, producer failure, invalid UTF-8, early
pipe closure, and output larger than the declared bounds. Do not promise
forcible termination of an uninterruptible Python writer thread. Proposed
owner: stream maintainer and downstream acceptance owner.

### GAP-07. Presentation failure and composition

**Scope/contract; G-02, G-03, G-04; Q-05.** A non-encoding sink error can still
propagate despite successful capture. [#435][i435] and [PR #503][p503] propose
an opt-in policy for a broken presentation pipe. The assessed output contract
also excludes overlapping grouped sessions sharing parent stderr; ordinary
command concurrency does not remove that restriction.

**Closure:** choose strict versus best-effort presentation deliberately; test
write/flush failures, per-stream isolation, retained capture, line observation,
and child reaping. Do not suppress inter-stage pipeline errors. Preserve the
concurrency restriction unless a separately reviewed framing contract replaces
it. Proposed owner: presentation-sink maintainer.

### GAP-08. Sensitive-information projections

**Scope/contract; G-04; N-07; Q-02.** Raw observations and documented logging
can carry arguments, environment values, or output. [#442][i442] proposes a
shared, opt-in projection/redaction policy. This is not an allegation of an
undisclosed vulnerability or a current guarantee of automatic secret removal.

**Closure:** define raw-versus-exported trust boundaries and redactor failure
behaviour. Sentinel-secret tests must cover configured logs, trace attributes,
presentation, and observer exports without changing execution or results.
Failure must not silently fall back to raw export. Proposed owner:
observability maintainer and security reviewer.

### GAP-09. Native consume integration and measured benefit

**Contract/evidence; G-07; S-06.** The native consume helper remains
deliberately
unintegrated in [the production bridge][stream-source]. [#314][i314] and
[ADR-002][adr2] require a narrow semantic envelope and end-to-end evidence
before production dispatch. [PR #432][p432] and [PR #433][p433] are plans for
boundary-error and Python event-cost work, not implementation evidence.

**Closure:** demonstrate all eligible production call sites, real-extension
parity tests, supported fallback, and S-06's measured gate against the tuned
Python baseline. Record an unsuccessful experiment and retain Python rather
than treating native execution as a product obligation regardless of benefit.
Proposed owner: stream/performance maintainer.

### GAP-10. Native boundary and liveness evidence

**Contract/evidence; G-02, G-07; C-03; S-05.** [#428][i428] distinguishes a live
Windows handle from one suitable for synchronous I/O; [PR #458][p458] proposes
a constrained boundary. The normal Python Proactor fallback must not be
confused with proof that every low-level native call is suitable.
[#427][i427]/[PR #468][p468] extend Miri interpretation into selected stream
composition; [#431][i431] requests operational Loom-run evidence.
[#425][i425]/[PR #456][p456] distinguish native hand-off failure from host
starvation. None establishes that a reported timing failure is already a
proven production race.

**Closure:** reconcile the [verification inventory][verification] with actual
production paths, tool limitations, accepted Windows capabilities, and retained
workflow evidence. Use discriminating liveness evidence, not a larger timeout
or a weakened assertion alone. Proposed owner: native-boundary maintainer.

### GAP-11. Compatibility and document conformance

**Conformance; C-04; S-08; Q-08.** The [sh package][sh-package] promises to
preserve names imported or defined by the former module, including private
and incidental imports. [output.py][output-source] retains deprecated
`IOOptions` and flat output-argument handling; the [roadmap][roadmap] explicitly
records retaining the alias. Those historical compatibility motives conflict
with C-04 because Cuprum is pre-1.0, with additional private-surface exclusions.

**Closure:** inventory each retained surface and its purpose, remove those
whose sole purpose is forbidden compatibility, and update all affected callers
and tests coherently. Keep intentional primary public entrypoints and useful
current adapters on their own merits. Reconcile the [ADR-007 record][adr7],
guide, roadmap, and design through explicit addenda or superseding decisions;
do not erase decision history. Generic issue boilerplate allowing downstream
shims does not override C-04. Proposed owner: Cuprum maintainer and scope owner.

No compatibility waiver is available for private, pre-1.0, or unreleased code.
The open decision is how to perform reconciliation, not whether to apply the
rule. This documentation change does not itself remove runtime APIs.

### GAP-12. Acceptance evidence reaches the consumer and the gate

**Evidence; G-06, G-07, G-08; S-01, S-02, S-05, S-07; Q-06, Q-07.** Lading's
reported runner-removal outcome remains unverified here. Source-tree tests do
not establish installed-package typing or all supported backend/platform
combinations. [#499][i499]/[PR #505][p505] identify tests outside default
collection, and [#446][i446]/[PR #462][p462] address lint coverage. Their scope
is gate evidence, not a claim that every excluded module contains a defect.

**Closure:** select a downstream acceptance inventory; record installation,
interpreter, platform, backend, collected targets, skips, and outcomes; and
confirm that promised gates actually execute. Keep downstream deletion and
maintenance benefit separate from library test counts. Proposed owner:
downstream acceptance owner with release/CI maintainers.

### GAP-13. Remaining roadmap work needs product priority

**Scope/conformance; G-01, G-05, G-06, G-08; Q-01.** [Roadmap §3][roadmap]
leaves a project-builder scaffold/checklist, registered attribute-style sugar
and its typing, and additional policy switches open. Existing guide recipes
mean this is not an absence of all builder guidance. Optional sugar is not a
prerequisite for fixing the generic builder's typing contract in GAP-01.

**Closure:** trace each remaining task to an accepted adopter need and G/S
identifiers, or explicitly defer it. Do not implement a roadmap idea merely
because it predates the charter. Proposed owner: scope owner and API maintainer.

## 7. Conformance and validated increments

The [requested discussion][discussion] treats the terms of reference as the
problem/scope input, design as baseline-to-target reasoning, and execution
plans as delivery and validation records. This is useful tailoring of TOGAF
(The Open Group Architecture Framework), not a claim of formal compliance or
a requirement to create a separate enterprise-architecture repository.

### 7.1 Minimum traceability record

An accepted design or execution increment should record the following fields
in its existing document, rather than create another tracker solely to hold
them.

| Field | Required content |
| --- | --- |
| Basis | Exact baseline revision, relevant G/C/S/Q identifiers, and governing ADRs. |
| Satisfies | The accepted contracts addressed, linked to their tests and measurements. |
| Reuses | Existing catalogue, lifecycle, stream, observation, or verification components, with their retained limitations. |
| Changes | Affected API consumers, scope assumptions, ownership boundaries, and information flows. |
| Remaining gaps | Explicit GAP identifiers, distinguishing outside-scope deferrals from blockers inside the increment. |
| Acceptance | Named accountable reviewer, observed results, deviations, and upstream documentation reconciliation. |

_Table 9: Minimal conformance record for downstream work._

An approved deviation should state the rule affected, rationale, risk, owner,
review trigger, and closure condition. A deviation cannot waive C-04's
excluded compatibility cases. A scope change returns to the charter; an
architectural choice belongs in the design or an ADR; a language change
returns here. Renaming a requirement must not silently break its traceability.

### 7.2 Plateaus are evidence boundaries, not compatibility layers

A plateau is a coherent repository state in which the selected increment can
be inspected and validated. It should record its entry conditions, acceptance
evidence, remaining gaps, and a credible way to revert or recover. Repository
revertability is distinct from a production rollback guarantee.

| Example increment, not a delivery commitment | Evidence before acceptance | Work explicitly not implied |
| --- | --- | --- |
| Consumer typing and policy clarity | Selected GAP-01/GAP-03 contracts, installed-consumer checks, and updated guide. | Attribute-style sugar, every optional stream mode, or full downstream migration. |
| Selected downstream execution contract | Agreed lifetime/output cases, complete required observations, and the downstream runner-removal record. | Long-lived supervision, arbitrary descendant containment, or requirements explicitly deferred by scope review. |
| Eligible native consume path | Parity, supported fallback, ownership evidence, and ADR-002's performance gate. | Native handling of unsupported callbacks, encodings, sinks, or platforms. |

_Table 10: Illustrative validated increments and their scope boundaries._

None requires an old interface to survive. For private, pre-1.0, and
unreleased interfaces, change the interface and its callers together. Do not
construct an adapter to make an intermediate state resemble the previous one.
A purposeful Python backend fallback remains valid because it serves a current
capability, not a historical interface.

Execution records should distinguish draft, approved, in-progress, complete,
and blocked states. Complete means the accepted scope and reconciliation are
finished. A blocked acceptance obligation is not made complete by moving it to
'follow-up'. Conversely, unrelated product gaps need not prevent acceptance of
a correctly bounded increment when its deferrals are explicit and approved.

## 8. Maintaining this context

Reassess a gap when its implementation merges, its requirement changes, or new
acceptance evidence appears. Record the new revision and evidence; do not
replace an old issue's probe revision with the current commit implicitly.
Resolved gaps may retain their identifiers and resolution references so that
links from decisions and execution records remain useful.

The [charter's discovery record][discovery]
provides the source inventory and consultation date. Repository-relative links
below are interpreted at the assessed baseline for this version. The requested
shared discussion did not yield a readable page transcript; its relevant
recommendations and explicit compatibility exclusions were recovered from the
earlier conversation, not treated as independently retrieved standards text.

[baseline]: https://github.com/leynos/cuprum/commit/991dee6429ccd415df4cc184e8d558c68d4ccf92
[skill]: https://github.com/leynos/df12-documentation-skills/blob/1a4d519b9267b95181174895d2cbca60bda43c20/skills/terms-of-reference-doc/SKILL.md
[discussion]: https://chatgpt.com/share/6ab7f58f-9858-83eb-b12e-281020877d90?ogimg=plain
[design]: cuprum-design.md
[guide]: users-guide.md
[roadmap]: roadmap.md
[layout]: repository-layout.md
[metadata]: ../pyproject.toml
[adr2]: adr-002-additional-rust-components.md
[adr7]: adr-007-subprocess-execution-module-boundaries.md
[verification]: rust-boundary-verification.md
[sh-package]: ../cuprum/sh/__init__.py
[safe-cmd]: ../cuprum/sh/safe_cmd.py
[output-source]: ../cuprum/sh/output.py
[env-source]: ../cuprum/context/env_overlay.py
[stream-source]: ../cuprum/_streams_rs.py
[i314]: https://github.com/leynos/cuprum/issues/314
[i424]: https://github.com/leynos/cuprum/issues/424
[i425]: https://github.com/leynos/cuprum/issues/425
[i427]: https://github.com/leynos/cuprum/issues/427
[i428]: https://github.com/leynos/cuprum/issues/428
[i431]: https://github.com/leynos/cuprum/issues/431
[i434]: https://github.com/leynos/cuprum/issues/434
[i435]: https://github.com/leynos/cuprum/issues/435
[i436]: https://github.com/leynos/cuprum/issues/436
[i437]: https://github.com/leynos/cuprum/issues/437
[i438]: https://github.com/leynos/cuprum/issues/438
[i440]: https://github.com/leynos/cuprum/issues/440
[i441]: https://github.com/leynos/cuprum/issues/441
[i442]: https://github.com/leynos/cuprum/issues/442
[i443]: https://github.com/leynos/cuprum/issues/443
[i444]: https://github.com/leynos/cuprum/issues/444
[i445]: https://github.com/leynos/cuprum/issues/445
[i446]: https://github.com/leynos/cuprum/issues/446
[i483]: https://github.com/leynos/cuprum/issues/483
[i485]: https://github.com/leynos/cuprum/issues/485
[i499]: https://github.com/leynos/cuprum/issues/499
[p432]: https://github.com/leynos/cuprum/pull/432
[p433]: https://github.com/leynos/cuprum/pull/433
[p456]: https://github.com/leynos/cuprum/pull/456
[p458]: https://github.com/leynos/cuprum/pull/458
[p462]: https://github.com/leynos/cuprum/pull/462
[p466]: https://github.com/leynos/cuprum/pull/466
[p468]: https://github.com/leynos/cuprum/pull/468
[p501]: https://github.com/leynos/cuprum/pull/501
[p502]: https://github.com/leynos/cuprum/pull/502
[p503]: https://github.com/leynos/cuprum/pull/503
[p505]: https://github.com/leynos/cuprum/pull/505
[discovery]: terms-of-reference.md#appendix-a-discovery-record-and-references
