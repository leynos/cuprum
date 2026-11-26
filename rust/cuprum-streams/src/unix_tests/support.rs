//! Values and helpers shared by the public Unix stream scenarios.
//!
//! The payload table keeps raw invalid UTF-8 out of the feature file, where it
//! would be unreadable and fragile to edit, while the exact bytes stay visible
//! here beside the step that writes them. The rest of the module is the small
//! amount of pure machinery the steps share: slot accessors, the validated
//! buffer size, and the reader that injects a real unwind.

use std::{
    cell::Cell,
    os::fd::{AsFd, BorrowedFd, OwnedFd},
    time::Duration,
};

use rstest_bdd::{Slot, StepResult};

use crate::{BufferSize, PumpError};

/// How long a scenario waits for a pipe to reach end of input.
///
/// Every transfer here is local and in-process, so a correct owner closes in
/// microseconds; the margin exists only so a lost close reports as a failed
/// scenario instead of waiting out the job's own timeout.
pub(super) const EOF_DEADLINE: Duration = Duration::from_secs(10);

/// Environment marker the scenario guard sets for a single-scenario child run.
pub(super) const ISOLATED_CHILD_ENV: &str = "CUPRUM_STREAMS_ISOLATED_SCENARIO";

/// The fixed line an isolated child prints once a writer-probe scenario passes.
///
/// The guard reads this exact text from the child's stdout, so it is evidence
/// that the scenario ran with the marker *and* reached its final assertion.
pub(super) const WRITER_PROBE_WITNESS: &str =
    "cuprum-streams: isolated writer probe observed the owned writer closed";

/// A labelled payload: the bytes a pipe carries and the text consume decodes.
pub(super) struct Payload {
    pub(super) label: &'static str,
    pub(super) bytes: &'static [u8],
    pub(super) decoded: &'static str,
}

/// Every payload the feature file names, keyed by its label.
const PAYLOADS: &[Payload] = &[
    Payload {
        label: "transfer",
        bytes: b"pump transfer payload",
        decoded: "pump transfer payload",
    },
    Payload {
        label: "empty",
        bytes: b"",
        decoded: "",
    },
    Payload {
        label: "naive",
        bytes: "naïve".as_bytes(),
        decoded: "naïve",
    },
    Payload {
        label: "grinning",
        bytes: "😀".as_bytes(),
        decoded: "😀",
    },
    // Unreadable in Gherkin, explicit here: a lone continuation-free byte
    // has to decode to one replacement character.
    Payload {
        label: "invalid-byte",
        bytes: b"a\xFFb",
        decoded: "a\u{FFFD}b",
    },
    // The first two bytes of a three-byte sequence, cut at EOF.
    Payload {
        label: "truncated-tail",
        bytes: b"ok\xE2\x82",
        decoded: "ok\u{FFFD}",
    },
    Payload {
        label: "hangup",
        bytes: b"payload-written-after-the-downstream-hangs-up",
        decoded: "payload-written-after-the-downstream-hangs-up",
    },
];

/// Resolve a payload label the feature file names.
pub(super) fn payload_for(label: &str) -> StepResult<&'static Payload, String> {
    PAYLOADS
        .iter()
        .find(|payload| payload.label == label)
        .ok_or_else(|| format!("the feature file names an unknown payload label: {label:?}"))
}

/// The bytes a pump scenario's sink must hold once the pump returned.
///
/// `payload` is the labelled payload when the scenario preloaded one. Scenarios
/// that assert an empty sink (`nothing`) never preload a payload, so requiring
/// one here would fail on the slot rather than on the expectation.
pub(super) fn sink_expectation(
    contents: &str,
    payload: Option<&Payload>,
) -> StepResult<Vec<u8>, String> {
    match contents {
        "the payload" => payload.map(|named| named.bytes.to_vec()).ok_or_else(|| {
            "the feature file expects the payload, but no payload label was recorded".to_owned()
        }),
        "nothing" => Ok(Vec::new()),
        other => Err(format!(
            "the feature file names an unknown sink expectation: {other:?}"
        )),
    }
}

/// The public pump's return value, reduced to what a scenario asserts.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum ObservedPump {
    /// The pump returned the number of bytes it transferred.
    Transferred(u64),
    /// The pump returned the semantic I/O failure variant.
    IoFailure,
    /// The pump failed in a way no scenario expects; the rendering names it.
    OtherFailure(String),
    /// The pump unwound, carrying the marker payload's rendering.
    Panicked(Option<String>),
}

/// Reduce a pump result to its observable shape without asserting here.
///
/// Steps record this and the `Then` steps assert it, so a scenario whose steps
/// ran out of order fails with the slot that was missing rather than with a
/// bare unwrap.
pub(super) fn observe_pump(result: Result<u64, PumpError>) -> ObservedPump {
    match result {
        Ok(bytes) => ObservedPump::Transferred(bytes),
        Err(PumpError::Io(_)) => ObservedPump::IoFailure,
        Err(other) => ObservedPump::OtherFailure(format!("{other:?}")),
    }
}

/// Convert a feature-file size into the validated buffer-size contract.
pub(super) fn buffer_size(size: isize) -> StepResult<BufferSize, String> {
    let widened = i64::try_from(size)
        .map_err(|error| format!("buffer size {size} does not fit i64: {error}"))?;
    BufferSize::new(widened).map_err(|error| format!("buffer size {size} was rejected: {error}"))
}

/// Read a value an earlier step was required to record.
pub(super) fn recorded<T: Clone>(slot: &Slot<T>, label: &str) -> StepResult<T, String> {
    slot.get()
        .ok_or_else(|| format!("no {label} was recorded; a Given step must run first"))
}

/// Move a value an earlier step was required to record.
pub(super) fn take<T>(slot: &Slot<T>, label: &str) -> StepResult<T, String> {
    slot.take()
        .ok_or_else(|| format!("no {label} was available; a Given step must run first"))
}

/// Marker panicked by the unwinding reader, so the scenario can identify it.
pub(super) const UNWIND_MARKER: &str = "cuprum-streams unix_tests injected read unwind";

/// A reader whose first `as_fd` call unwinds, standing in for a caller bug.
///
/// It borrows a live descriptor so the unwind happens inside `pump_stream`'s
/// ownership scope with a real descriptor behind it. This models an unwind
/// before the pump touches the descriptor, not one during an in-flight read:
/// the property under test is that the consumed writer still closes.
pub(super) struct PanickingReader<'a> {
    descriptor: &'a OwnedFd,
    calls: &'a Cell<u32>,
}

impl<'a> PanickingReader<'a> {
    /// Borrow `descriptor` and count the pump's attempts through `calls`.
    pub(super) const fn new(descriptor: &'a OwnedFd, calls: &'a Cell<u32>) -> Self {
        Self { descriptor, calls }
    }
}

impl AsFd for PanickingReader<'_> {
    fn as_fd(&self) -> BorrowedFd<'_> {
        self.calls.set(self.calls.get().saturating_add(1));
        // Derive the borrow the pump asked for before unwinding, so the unwind
        // happens with a live descriptor in hand rather than over an unused
        // field. The borrow dies with the unwind.
        let _live = self.descriptor.as_fd();
        std::panic::panic_any(UNWIND_MARKER);
    }
}

/// Print the witness line an isolated guard run requires.
///
/// This goes to standard error on purpose. With `--nocapture`, libtest writes
/// `test <name> ... ` before the test body runs and `ok` after it, so anything
/// the body prints to standard output lands *inside* that result line and the
/// guard can no longer read it as evidence. Standard error keeps the result
/// line contiguous; the guard reads the witness from the child's failed-output
/// stream beside the panic text an unwinding scenario is expected to produce.
#[expect(
    clippy::print_stderr,
    reason = "The guard reads the child's stderr; this line is the child's evidence"
)]
pub(super) fn print_writer_probe_witness() {
    eprintln!("{WRITER_PROBE_WITNESS}");
}

#[cfg(test)]
#[path = "support_tests.rs"]
mod tests;
