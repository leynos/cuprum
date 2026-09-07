# Debugging plan: tracing capture interest race

- Generated: 2026-09-07.
- Issue: [#366](https://github.com/leynos/cuprum/issues/366).
- Falsification sub-agent: `alchemist`.
- Planning boundary: the planning agent prepares the experiment; `alchemist`
  executes it independently and reports the result.

## Observed failure

The proposed `FilterCapture::register_callsite` override returning
`Interest::sometimes()` passes the repository gates and nextest but does not
eliminate parallel tracing failures. A six-thread plain Cargo unit-suite run
failed the EINTR warning assertion at repetitions 100 and 174 and the DEBUG
read-event assertion at repetition 136. Existing assertions remain unchanged.
The baseline also contains a separate descriptor-close assertion flake, which
is outside this investigation.

## Hypothesis

Tracing-core 0.1.36's single-dispatch registration fast path consults the
registering thread's default subscriber. An uncaptured thread can therefore
register a shared callsite with `never`, even while another thread has an
active capture. The capture's `register_callsite` override is bypassed in this
case. Its later event uses the cached `never` and disappears.

Prediction: a fresh warning callsite first executed on an uncaptured child
thread, while the parent holds a WARN capture, remains disabled when the parent
executes the same callsite. No timing delay is needed: joining the child fixes
the order. A successful parent capture on the existing override-only harness
would falsify this specific explanation.

## Minimal falsification experiment

Run only the new test, in a fresh process, from `rust/`:

```bash
cargo test -p cuprum-rust --lib \
  tracing_capture::tests::warn_capture_records_callsite_first_seen_without_subscriber \
  -- --exact
```

Capture output with `tee` and preserve the command's exit status. The expected
failure is the parent warning assertion, not a thread panic or a build error.
Do not run repository gates or other tests concurrently. The executing agent
must not edit tracked files.

## Candidate mitigation

Retain a dormant dispatch before constructing any capture dispatch. The dormant
subscriber enables nothing and reports `LevelFilter::OFF`, keeping tracing
disabled during initialization. Every subsequent capture then registers beside
the retained dispatch, selecting tracing-core's synchronized registry path
instead of the thread-local single-dispatch fast path. It must never be
installed as a thread-local or global default.

After the planning agent adds this mitigation, repeat the exact experiment. The
parent must capture exactly one warning, and the child must remain uncaptured.
Any missing or extra event falsifies the candidate mitigation. Then run the
required gates and parallel stress verification through `scrutineer` before
review or publication.

## Results

The isolated experiment failed at the parent warning assertion with only the
`Interest::sometimes()` override. The child joined successfully. After adding
the dormant dispatch, the same experiment passed, including the assertion that
exactly one event was captured. Both runs were executed by `alchemist`; this
did not falsify the hypothesis or candidate mitigation.

Source inspection explains why one dormant dispatch suffices. Tracing-core
updates its single-dispatch flag during dispatch registration, not removal.
Each capture registers beside the retained guard, keeping that flag false.
Unlike `NoSubscriber`, which supplies no level hint and thereby enables TRACE
during initialization, the dormant subscriber reports OFF. Neither
initialization nor later callsite registration needs to consult an uncaptured
thread's default subscriber.

The earlier same-thread ERROR then WARN test passes on the old harness because
creating the second dispatch rebuilds callsite interest. Separate
registration-policy checks fail on the old harness and pass with
`Interest::sometimes()`, but do not cover registration from a thread without a
subscriber. The cross-thread regression is therefore required alongside them.
