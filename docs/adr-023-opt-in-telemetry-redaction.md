# Architectural decision record (ADR) 023: Opt-in telemetry redaction

## Status

Accepted. Redaction is configured independently for each logging, tracing,
presentation-title, or explicitly wrapped observation destination.

## Date

2026-10-14.

## Context and problem statement

`ExecEvent` is the raw observation of one command execution. It can carry
argument values, an environment overlay, output lines, diagnostic notes, and
caller-provided tags. Structured logging and tracing project selected event
fields into records and spans, while the GitHub Actions presentation sink uses
the argument vector to title a log group. A consumer may choose to pass a
secret through any of those values.

The execution layer builds each raw event once in
`cuprum._pipeline_types._StageObservation.emit`. Mutating that event or the
command data would make redaction affect execution and captured results. The
policy therefore needs a clear boundary between raw observation and exported
projections, with consistent opt-in behaviour at each supported destination.

## Decision drivers

- Keep execution inputs, child environment, and captured results unchanged.
- Give each destination the same caller-configured literal and pattern rules.
- Make the default inert and avoid implying automatic secret discovery.
- Stop an export when an enabled rule fails.
- Preserve the existing bounded metric-label vocabulary.

## Requirements

### Functional requirements

- Redact configured literals and regular-expression matches in selected text.
- Apply event redaction to argv items, environment values, output lines,
  diagnostic notes, and string-valued tags.
- Apply title redaction to the argv-derived GitHub Actions group title.
- Let callers wrap other observation consumers before registering them.

### Technical requirements

- Keep the raw `ExecEvent` immutable and return a sanitized copy.
- Scope policy instances to an adapter, sink, or explicitly wrapped consumer.
- Propagate redaction failures to prevent any fallback to raw values.
- Leave metric adapters and their bounded labels unchanged.

## Options considered

### Redact at execution time

Changing arguments, environment overlays, or captured output before the child
uses them would alter command behaviour and returned results. This option was
rejected.

### Redact at each adapter's projection boundary

Each enabled adapter applies the same immutable policy to its raw observation
before building its output. This preserves execution semantics and keeps the
policy local to destinations that explicitly opt in. This option was selected.

### Redact globally during event emission

Replacing the raw event before dispatch would hide values from every observer,
including consumers that deliberately retain raw observations, and would mix
export policy into execution. This option was rejected.

## Decision outcome / proposed direction

Provide an inert-by-default `RedactionPolicy` with caller-supplied literal
strings, compiled text patterns, and a replacement mask. Other observation
consumers use the `redacting_hook` wrapper to receive sanitized `ExecEvent`
copies before registration with `observe()`. Rule normalization and application
remain private to the policy; adapters and sinks reuse the policy rather than
duplicating rule handling.

Structured logging, tracing, and plain-text logging accept the policy directly
through a `redaction` keyword argument. Event adapters sanitize a copy before
projection, redacting argv items, environment values, output lines, diagnostic
notes, and top-level string-valued tag values. Plain-text logging applies the
policy only to its argv projection. The GitHub Actions sink accepts the same
keyword and applies the policy only to argv-derived group titles before
workflow-command escaping; an explicit title, the annotation-label path, and
mirrored child output remain unchanged. The convenience group and annotation
flags do not accept a policy, so callers needing title redaction configure the
sink directly. Passing `None` or an inert policy preserves existing output.

Across all these integrations, the raw event, executed argv, child environment,
and captured result remain unchanged.

An error while applying a pattern raises `RedactionError` with rule metadata.
The error omits the source value and the underlying exception text. Export
paths do not catch a redaction failure and retry with the raw value.

Metric adapters remain unchanged. Their labels use bounded program, project,
phase, and outcome values and do not project argv, environment, output lines,
notes, or caller tag values.

## Goals and non-goals

- Goals:
  - Give opted-in destinations one reusable projection policy.
  - Keep policy application local and execution data untouched.
  - Preserve bounded metric labels.
- Non-goals:
  - Detecting arbitrary secrets without caller configuration.
  - Redacting every possible nested object in tag values.
  - Changing command arguments, child environment, output capture, or results.
  - Redacting metric labels or trusted program and project fields.

## Known risks and limitations

- Literal and pattern rules cover only values the caller configures. They do
  not guarantee that unrecognized or differently encoded secrets are absent.
- Tags are redacted only when their top-level value is a string; other values
  remain unchanged.
- Redaction is per destination. Every enabled logging, tracing, presentation,
  or custom observation export must receive a policy or a redacting wrapper.
- A rule failure propagates through the existing observe-hook error path and
  can fail a command call before, during, or after child execution.
