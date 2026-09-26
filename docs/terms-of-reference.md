# Cuprum – terms of reference

**Status:** Draft v0.1, reconstructed from existing artefacts; not yet ratified.
**Audience:** Cuprum maintainers, downstream automation authors, reviewers, and
those deciding product scope for df12 Productions.
**Date:** 26 September 2026.
**Evidence baseline:** [Cuprum commit][baseline]
`991dee6429ccd415df4cc184e8d558c68d4ccf92`.

**Companion documents:** [docs/context.md](context.md) defines domain language,
current boundaries, and design gaps; [docs/cuprum-design.md][design] describes
the solution; [docs/roadmap.md][roadmap] sequences delivery;
[docs/users-guide.md][guide] documents usage. Architectural decision records
(ADRs) are indexed in [docs/contents.md](contents.md).

The design and implementation predate this charter. This document reconstructs
the problem they address rather than treating existing architecture as proof
of an agreed requirement. It follows the nine-section [terms-of-reference
skill][skill]. That skill assigns the ubiquitous-language role to
`docs/context.md`; it does not prescribe a separate context-document template.

Evidence labels apply throughout:

- **[KNOWN]** identifies an explicit source. Known *intent* is not evidence of
  implementation, release, or adoption.
- **[ASSUMED]** identifies a proposition requiring stakeholder confirmation.
- **[OPEN]** identifies a missing decision or missing evidence. Draft acceptance
  criteria are proposals, not reports of successful validation.

Identifiers remain stable when sections move. The context document maps goals
and constraints to baseline evidence, gaps, and closure criteria. No issue is
closed, roadmap task completed, or implementation authorized by this draft.

## 1. Background and motivation

[KNOWN] Cuprum addresses Python automation that otherwise accumulates shell
scripts or repeated subprocess plumbing: deployment helpers, Continuous
Integration (CI) glue, maintenance scripts, and administrative tools. Its
[README][readme] and [design overview][design] identify explicit command
selection, typed construction, output handling, scoped policy, and execution
visibility as the reasons to use it.

[KNOWN] The immediate adoption pressure is concrete. [Lading feedback
#361][adoption] records large machine-readable output obscuring CI progress,
Windows output-encoding failures, and insufficient timing information. It
also states an adoption outcome: replace Lading's bespoke subprocess runner
and stream relay without losing behaviour covered by its tests. This is
reported downstream evidence, not a migration completed by this document.

[KNOWN] The assessed source declares version `0.2.0-beta1` in
[pyproject.toml][metadata]. That is source metadata, not a statement about the
latest published package. Existing capabilities and current adoption gaps now
need a common scope and acceptance basis before further optimization or
application programming interface (API) expansion.

[OPEN] No quantified market demand, maintenance-cost baseline, or agreed
commercial return was found in the inspected artefacts. The defensible
motivation is the documented automation problem and downstream feedback, not
an invented market opportunity.

## 2. Domain

[KNOWN] The domain is **local command execution within Python automation**.
Authors select external tools, construct arguments, connect streams, apply
execution policy, observe progress, and interpret outcomes. Cuprum does not
own the tools' business semantics or the host's security policy. The existing
[design][design] explicitly excludes general shell interpretation and remote
execution.

The distinctions in [docs/context.md](context.md) are material to scope:
command construction is not execution; catalogue membership is not executable
integrity; cancellation of a direct child is not containment of descendants;
capture is not echo; and an idle notification is not a deadlock diagnosis.

[KNOWN] The package declares Python 3.12 or newer, the ISC licence, and no
mandatory runtime dependencies in its [metadata][metadata]. Optional native
acceleration is an existing design choice, not the purpose of the product.

[OPEN] The inspected material establishes no regulated-industry certification,
audit-retention obligation, contractual service level, or mandated commercial
support window. None is implied by the words 'safe' or 'observable'. Any such
obligation needs explicit scope approval and supporting evidence.

## 3. Market context

This is a positioning comparison of alternatives named in the repository, not
a feature audit of their latest releases or a market-share assessment.

| Alternative | Job it already occupies | Reason to consider Cuprum |
| --- | --- | --- |
| Bash and existing shell scripts | Concise local automation and pipelines. | Move application logic into Python while making command policy, results, and failure handling explicit. |
| Python `subprocess` and `asyncio.create_subprocess_exec` | Direct process execution, including argument-vector execution without a shell. | Reuse a common command, policy, output, and lifecycle contract rather than reconstructing their composition in each application. |
| Plumbum and the Python `sh` package | Python-based command scripting; both are named as prior art in the design. | Evaluate Cuprum's stated emphasis on explicit registration, scoped policy, typing, and async execution against the application's needs. |
| Existing application-specific runners | Already encode local requirements and have downstream tests. | Remove duplicated lifecycle and stream machinery only after demonstrated contract coverage. |
| Keep the current implementation | Avoid migration and dependency costs. | Migration must improve a documented outcome enough to justify those costs. |

_Table 1: Existing alternatives and Cuprum's intended differentiation._

Sources: [README][readme], [design §§1–3][design], and
[Lading feedback][adoption].
Cuprum's argument-vector approach does not make argument-vector use of
`subprocess` inherently unsafe. The proposed value is the combined contract,
not a claim that Python previously lacked safe subprocess primitives.

[ASSUMED] Downstream teams value that consolidation more than preserving every
existing wrapper convention. A migration that merely places Cuprum beneath an
unchanged bespoke runner has not demonstrated the intended benefit.

## 4. Users and stakeholders

The following roles are reconstructed from the [README][readme],
[design][design], and [adoption feedback][adoption]. Role descriptions are not
claims about interview findings. Named decision owners remain open in Q-01.

| Role | Context and concern | Unwanted trade-off or current alternative | Required view and evidence | Proposed decision authority |
| --- | --- | --- | --- | --- |
| Primary: Python automation author | Builds deployment, CI, or maintenance tooling; needs inspectable commands and predictable outcomes. | Repeated subprocess plumbing, implicit executable discovery, or mandatory native tooling; currently uses scripts or a local runner. | Usage and policy view: guide examples, typing checks, and rejected-command tests. | Accepts whether the workflow solves the job; cannot redefine library-wide scope alone. |
| Primary: downstream runner maintainer | Integrates Cuprum into an existing tool, with Lading as the evidenced case. | Lost output, uncontrolled lifetime, or another permanent compatibility layer. | Migration and failure view: downstream contract inventory and end-to-end results. | Accepts the downstream migration against its own tests. |
| Secondary: operator or code reviewer | Reads execution logs or reviews automation rather than designing the runner. | Silent work, ambiguous failures, exposed credentials, or unreadable output. | Outcome and information-boundary view: failure traces, projection policy, and readable diagnostics. | Supplies operational acceptance evidence and reviews information exposure. |
| Stakeholder: Cuprum maintainer and scope owner | Balances product boundaries, maintainability, portability, and verification cost. | Speculative features, unsupported guarantees, and retained historical interfaces without a valid obligation. | Conformance view: requirement-to-design-to-test mapping, ADRs, and remaining gaps. | Ratifies scope and accepts architectural decisions; named owner to be confirmed. |
| Non-user: remote orchestration or hostile-code sandbox operator | Needs distributed scheduling, isolation, or long-lived supervision. | A local library presented as a security boundary or fleet controller. | Boundary view: explicit exclusions and external responsibilities. | Uses a separate system for those requirements. |

_Table 2: Stakeholders, concerns, evidence, and proposed authority._

## 5. Job to be done

### 5.1 Automation author

> When a Python workflow needs several external tools, the automation author
> wants execution policy, progress, and failure handling to remain explicit,
> so the workflow can be reviewed and operated without reconstructing shell
> behaviour or subprocess machinery at every call site.

[KNOWN] This functional job follows the [design's target use cases][design].
[ASSUMED] Confidence when debugging and confidence during review are relevant
emotional and social outcomes; no satisfaction measurement is available.

### 5.2 Downstream runner maintainer

> When an application already owns command-running and stream-relay code, its
> maintainer wants a shared library to cover the application's actual execution
> contracts, so the bespoke machinery can be removed without losing required
> behaviour or weakening tests.

[KNOWN] [Lading #361][adoption] supplies this job's concrete acceptance intent.
Keeping the existing runner is a valid alternative until coverage is proven.

### 5.3 Evidenced operating scenario

In the reported Lading scenario, a release helper launches a local tool that
produces a large machine-readable response. The maintainer needs the complete
response for a decision, useful progress in the CI log, and an attributable
outcome. The response should not monopolize presentation or turn text-encoding
mismatches into lost execution results. The reported incidents motivate G-02,
G-03, G-04, and G-08; they do not establish an entitlement to every proposed
binary, storage, or process-control feature.

## 6. Scope

### 6.1 Goals

These are target outcomes reconstructed from explicit intent. Their delivery
status is recorded separately in the [gap register](context.md#6-design-gaps).

| ID | Intended outcome | Evidence for intent |
| --- | --- | --- |
| G-01 | On the catalogue-backed path, make permitted command construction inspectable, reject unknown programs, and enforce applicable execution scopes. | README; design §§2 and 4. |
| G-02 | Complete, cancel, or time out local commands and pipelines without abandoning directly managed children or concealing failure outcomes. | Design §§2 and 4; roadmap §§1.2 and 2.1. |
| G-03 | Retain requested output for later decisions while allowing progress reporting to be controlled independently. | README; guide output contracts; Lading feedback. |
| G-04 | Explain execution attempts, outcomes, and measurements through optional observation with explicit information and failure boundaries. | Design observability principles; Lading feedback. |
| G-05 | Make command policy and execution settings predictable across synchronous use, asynchronous use, nested scopes, and concurrent tasks. | Design §§4.3–4.4; roadmap §§1.2–1.3 and 2.3. |
| G-06 | Make the intended command and argument contracts usable by consumer type checkers, rather than relying only on runtime errors. | Design static-first principle; generic typing gap #483. |
| G-07 | Keep the pure Python path usable without native prerequisites, and justify optional acceleration with semantic parity and measured benefit. | README; ADR-001; ADR-002. |
| G-08 | Replace evidenced downstream runner responsibilities with shared, documented contracts and remove redundant application machinery. | Lading #361. |

_Table 3: Product goals; sources are listed in Appendix A._

### 6.2 Non-goals

| ID | Exclusion and boundary |
| --- | --- |
| N-01 | General shell grammar, expansion, globbing, and shell control flow are outside the library. Applications needing shell interpretation must own that choice explicitly. |
| N-02 | Implicit discovery of executable builders from the host search path is outside scope. Executable selection remains an explicit application decision. |
| N-03 | Remote execution, fleet scheduling, and distributed orchestration belong to separate systems. Launching a local program does not make its remote effects a Cuprum guarantee. |
| N-04 | Hostile-code containment, privilege separation, binary authenticity, and a guarantee that an approved program is harmless are outside scope. Host security controls remain necessary. |
| N-05 | Tool-specific business correctness and complete static encoding of shell or pipeline semantics are outside the generic library. Project builders may impose narrower domain rules. |
| N-06 | Long-lived service supervision is not an assumed product capability. Owned local handles and descendant cleanup remain bounded proposals requiring Q-04, not an implicit expansion into a supervisor. |
| N-07 | Mandatory telemetry infrastructure, automatic detection of arbitrary secrets, and guarantees of globally ordered independent output streams are not product promises. Explicit projection and ordering contracts are required. |
| N-08 | Speculative compatibility surfaces, optional syntactic sugar as a prerequisite for adoption, and native acceleration at the expense of correctness are excluded. C-04 governs compatibility; sugar and acceleration require their own evidence. |

_Table 4: Explicit exclusions and responsibility boundaries._

[KNOWN] N-01, N-02, N-03, and the pipeline-typing part of N-05 restate the
[design's exclusions][design]. N-04 follows the documented policy boundary,
not a sandbox implementation. [ASSUMED] The remaining scope refinements need
ratification through Q-01 to Q-05; the explicit compatibility prohibition in
C-04 is already supplied by the commissioning instruction.

## 7. Success criteria

The following acceptance signals are proposed unless an existing decision is
cited. A test listed in the repository is not a test rerun for this assessment.
'Open' means evidence is still required, not that a production defect has been
reproduced.

| ID and class | Signal and closure evidence | Goals | Assessment |
| --- | --- | --- | --- |
| S-01: user-facing | A consumer can install an intended distribution, type-check the supported builder calls, reject invalid calls, and execute the documented first-command example. | G-01, G-06 | Runtime examples exist; consumer typing and packaging work remain in GAP-01. No onboarding-time target has been agreed. |
| S-02: user-facing | The selected downstream migration passes its agreed contract inventory and removes redundant runner and relay implementations. | G-03, G-05, G-08 | Lading supplies the acceptance intent; successful full migration is not established. |
| S-03: operational | Rejection, spawn failure, success, non-zero exit, timeout, and cancellation have specified outcomes; owned direct children are settled and observed executions can be accounted for. | G-01, G-02, G-04 | Partial baseline coverage; terminal observation and test-evidence gaps remain. |
| S-04: operational | Agreed output workloads preserve the selected result semantics while respecting declared presentation, queue, and retention bounds. | G-03, G-05 | Echo limits exist; retained-capture limits and slow-sink guarantees must not be inferred. Q-03 defines additional acceptance scope. |
| S-05: operational | Every promised platform and backend combination executes its applicable contract tests; unsupported combinations and verifier exclusions are explicit. | G-02, G-05, G-07 | A support/evidence matrix still needs consolidation through Q-06. |
| S-06: operational | A proposed native consume path meets ADR-002's targeted gate: at least 20% lower median heavy-scenario wall time, at most 5% small-output/fallback regression, and its parity and memory conditions. | G-07 | An existing experiment gate, not a general speed claim. GAP-09 remains open; failure of the experiment permits retaining Python. |
| S-07: strategic | A reviewed downstream acceptance record demonstrates shared ownership of previously duplicated execution behaviour, with remaining exceptions explicitly scoped. | G-08 | Adoption evidence is open. Download counts alone would not demonstrate this outcome. |
| S-08: strategic | Accepted changes identify their governing goals, contracts, evidence, and remaining gaps; private, pre-1.0, and unreleased interfaces acquire no compatibility scaffolding. | G-01–G-08 | This draft starts traceability; GAP-11 records existing conflicts and missing governance. |

_Table 5: Proposed acceptance signals and their present evidential limits._

[OPEN] Numerical targets for onboarding, maintenance effort, supported workload
sizes, and recurring verification cost need baseline measurements and an owner
(Q-07). No revenue, availability, or latency objective is invented here.

## 8. Constraints and assumptions

### 8.1 Hard constraints

| ID | Constraint | Authority or source |
| --- | --- | --- |
| C-01 | The declared Python floor is 3.12; the pure Python package has no mandatory runtime dependencies. Changing either requires explicit scope review. | Project metadata and README. |
| C-02 | Command arguments on the normal execution path are not interpreted as a shell program; catalogue and scope checks must not be weakened to accommodate a migration. | README and design principles. |
| C-03 | Published guarantees must distinguish local process ownership, platform support, observation policy, and host responsibilities. Acceleration must preserve supported semantics. | Design; guide; ADR-001 and ADR-002. |
| C-04 | Never add or retain a compatibility shim, wrapper, or facade solely to preserve a private API, any pre-1.0 API, or code not yet formally released. | Explicit instruction in the requested TOGAF discussion. |
| C-05 | The project is distributed under the ISC licence; no additional contractual or regulatory obligation is presumed. | Project metadata and LICENSE. |

_Table 6: Constraints governing this reconstruction._

For C-04, **private** includes apparently public surfaces internal to an
application and interfaces exposed only for tests. **Unreleased** includes
changes ahead of a release tag. Update affected interfaces and callers in the
same coherent change; a migration plateau is not a reason to preserve the old
shape. Evidence of consumers does not override these three exclusions.

Only an actually released, public contract at version 1.0 or later with an
evidenced external obligation can justify considering compatibility machinery.
This is not
an automatic entitlement. Intentional primary APIs, current cross-component
adapters, and a native-to-Python capability fallback are not historical
compatibility machinery merely because they wrap another implementation.

The assessed tree retains deprecated output APIs and historical private
re-exports. [GAP-11](context.md#gap-11-compatibility-and-document-conformance)
records the conflict rather than retroactively legitimizing it. This document
neither removes those interfaces nor rewrites accepted ADR history.

### 8.2 Assumptions

| ID | Assumption requiring confirmation | Consequence if false | Resolution |
| --- | --- | --- | --- |
| A-01 | Python automation authors and downstream runner maintainers are the initial product audience. | Feature priorities and acceptance scenarios may target the wrong users. | Q-01: ratify the audience using concrete consumers. |
| A-02 | Policy authors and approved programs operate within a trusted application and host boundary. | A catalogue alone cannot meet the required containment or integrity guarantee. | Q-02: document the trust boundary and any external controls. |
| A-03 | A supported workload envelope can be stated for output volume, sink behaviour, and child lifetime. | Memory use and cancellation responsiveness cannot be promised meaningfully. | Q-03 and Q-04: choose bounded contracts and tests. |
| A-04 | Downstream maintainers will provide acceptance tests and remove superseded runner code when the contract is met. | Reuse increases layering rather than reducing maintenance responsibility. | Q-07: agree a migration evidence record. |
| A-05 | Maintainer capacity can sustain the selected platform and verification matrix. | Optional optimization or platform expansion may make releases unsustainable. | Q-06 and Q-07: measure cost and set support boundaries. |

_Table 7: Assumptions, consequences, and resolution routes._

### 8.3 Dependencies

[KNOWN] Successful execution depends on installed approved tools, their
exit-status and argument contracts, host permissions, and operating-system
process and stream facilities. These are external conditions, not properties
proved by command construction. Optional native builds additionally depend on
the existing Rust/Python integration and distribution pipeline described in
[ADR-001][adr1] and the [users' guide][guide].

[KNOWN] Closing specific adoption gaps depends on their tracked implementation
and downstream validation. An open pull request is not a dependency already
satisfied. The [context gap register](context.md#6-design-gaps) identifies the
relevant work without assigning a new release date or duplicating its scope.

[OPEN] Named ownership of external-tool versions, supported installation
routes, release acceptance, and CI resource ceilings remains Q-06/Q-07. No
unverified numeric budget or deadline is imposed by this charter.

## 9. Open questions

The roles below are proposed resolution owners, not appointments. C-04 is not
an open question: Q-08 concerns reconciliation with it, not permission to waive
it.

| ID | Question and downstream impact | Evidence or decision that closes it | Proposed owner and route |
| --- | --- | --- | --- |
| Q-01 | Are the reconstructed audiences, eight goals, and exclusions the agreed product boundary? This gates prioritization and charter acceptance. | Recorded maintainer and downstream review, with accepted or rejected scope refinements and a named scope authority. | Scope owner and downstream maintainers; charter review. |
| Q-02 | What exactly may policy authors trust about program identity, executable resolution, environment inheritance, and exported observations? This gates stronger safety claims. | A threat-boundary statement and decisions on #440, #434, and #442, with explicit external responsibilities. | Maintainer and security reviewer; design review and ADR candidates. |
| Q-03 | Which bounded-capture, binary-result, streaming-input, redirection, and slow-sink contracts are necessary for the selected adopters? This gates G-03's workload envelope. | Representative downstream cases, resource bounds, ownership/failure semantics, and explicit deferrals for #436 and #443–#445. | Downstream maintainer and runtime maintainer; contract inventory and focused experiments. |
| Q-04 | Does local lifetime ownership extend to independently controlled handles or descendants? This gates #437/#438 without assuming a supervisor product. | Approved process-ownership boundary, platform-specific limits, and acceptance cases for cancellation and partial startup. | Scope owner and runtime maintainer; ADR candidate. |
| Q-05 | Which observations may affect execution, and which must be failure-isolated? This gates terminal accounting, idle reporting, and presentation policies. | Channel-by-channel contract covering #441, #424, #485, and #435, without weakening primary exceptions or silently broadening an event schema. | Runtime and observability maintainers; design review. |
| Q-06 | Which interpreter, operating-system, architecture, installation, and backend combinations are promised, and with what evidence? | A versioned support matrix separating functional support, wheel availability, native eligibility, and actual gate execution. | Release and runtime maintainers; support-policy review. |
| Q-07 | What adoption, workload, maintenance-cost, and verification-cost targets justify completion? | Baseline measurements, a named downstream acceptance owner, and agreed thresholds rather than retrospective success claims. | Scope owner, downstream maintainer, and CI owner; measurement and acceptance review. |
| Q-08 | How will existing compatibility debt and contradictory documents be reconciled with C-04? | An inventory of historical surfaces, coordinated caller updates, and explicit superseding decisions where required; no forbidden shim survives solely for compatibility. | Cuprum maintainer; GAP-11 reconciliation work. |

_Table 8: Open questions and evidence needed for resolution._

## Appendix A. Discovery record and references

All sources were consulted on 26 September 2026. Repository claims refer to the
baseline above unless another revision is stated. Issue reports retain their
original probe revisions; this reconstruction does not repeat their runtime
experiments. Current issue and pull-request states are dated observations.

| Source | Contribution | Limitation or conflict |
| --- | --- | --- |
| [README.md][readme], [docs/cuprum-design.md][design] | Motivation, audience, core goals, principles, and explicit exclusions. | Design intent is broader than verified consumer-facing guarantees. |
| [docs/users-guide.md][guide], [docs/roadmap.md][roadmap] | Operational contracts and delivered/planned distinctions. | Checked roadmap items are not fresh acceptance evidence; deprecated output compatibility conflicts with C-04. |
| [pyproject.toml][metadata], [LICENSE](../LICENSE) | Source version, Python floor, dependencies, and licence. | No inference about the latest published distribution. |
| [Lading feedback #361][adoption] and linked gap issues | Concrete downstream job and acceptance intent. | Tracker checkboxes can lag implementation; old source links can name modules since moved. |
| [ADR-001][adr1], [ADR-002][adr2], and [ADR index](contents.md) | Existing acceleration and other architectural decisions. | Decisions are inputs for reconciliation, not evidence that experiments passed or every path is integrated. |
| [Documentation skill][skill] and its [editing checklist][editing] | Nine-section charter structure, evidence labels, open questions, and context handoff. | No distinct context-document template is prescribed. |
| [Requested TOGAF discussion][discussion] | Stakeholder concerns, traceability, baseline-to-target gaps, conformance, and the compatibility exclusions. | Tailoring guidance recovered from the earlier conversation; the shared page did not expose a readable transcript during this assessment. |

_Table 9: Prior-art inventory, contribution, and evidential limitations._

The inspected documentation index contained no upstream charter or domain
context companion. The missing stakeholder sign-off, quantified success
baseline, and consolidated requirement-to-evidence mapping are retained as
open work rather than filled with inferred certainty.

## Appendix B. Handoff and downstream readiness

**Ready for bounded design reconciliation:** this draft provides stable goals,
constraints, questions, and the
[context gap register](context.md#6-design-gaps).
It is not evidence that all of G-01–G-08 are satisfied, that Lading can already
delete its runner, or that the public API is stable.

The requested TOGAF (The Open Group Architecture Framework) learnings are used
as lightweight review practices, not a claim of framework compliance. The
terms of reference, design, and execution plans have different jobs; they need
linked evidence, not a duplicated enterprise-architecture document set.

Candidate ADR topics are executable identity and environment authority,
process ownership, output retention and delivery, observation/projection
failure boundaries, and compatibility-policy reconciliation. Existing ADRs
should be amended through their established history-preserving process when
applicable, not replaced by decisions hidden in this charter.

Before accepting a scoped design or marking its execution plan complete, the
maintainer should identify which G/C/S identifiers it satisfies, which existing
components it reuses, its tests and measurements, and every remaining gap.
Changes to audience, goals, constraints, or domain language return to this
charter or `docs/context.md`. Changes to sequencing belong in `docs/roadmap.md`.
A deferred product gap can remain open when outside the accepted increment;
an unresolved acceptance obligation inside that increment cannot be labelled
complete. No new execution plan or roadmap commitment is created here.

[baseline]: https://github.com/leynos/cuprum/commit/991dee6429ccd415df4cc184e8d558c68d4ccf92
[readme]: ../README.md
[design]: cuprum-design.md
[guide]: users-guide.md
[roadmap]: roadmap.md
[metadata]: ../pyproject.toml
[adoption]: https://github.com/leynos/cuprum/issues/361
[adr1]: adr-001-rust-extension.md
[adr2]: adr-002-additional-rust-components.md
[skill]: https://github.com/leynos/df12-documentation-skills/blob/1a4d519b9267b95181174895d2cbca60bda43c20/skills/terms-of-reference-doc/SKILL.md
[editing]: https://github.com/leynos/df12-documentation-skills/blob/1a4d519b9267b95181174895d2cbca60bda43c20/skills/tech-design-doc/references/editing-checklist.md
[discussion]: https://chatgpt.com/share/6ab7f58f-9858-83eb-b12e-281020877d90?ogimg=plain
