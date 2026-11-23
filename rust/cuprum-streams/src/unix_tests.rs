//! Behavioural coverage for the public Unix stream entry points.
//!
//! `windows_tests` pins the synchronous-handle boundary; this sibling pins the
//! borrowed-descriptor boundary Unix callers actually reach. Both drive the
//! public [`crate::pump_stream`] and [`crate::consume_stream`] rather than the
//! crate-private seams the rest of the suite uses, because the ownership
//! contract lives at that surface: the pump consumes the writer and must close
//! it on success, on error, and on unwind, while the borrowed reader stays
//! under its caller's ownership.
//!
//! Writer closure is always proved behaviourally, by reading the sink to EOF:
//! a pipe reports end of input once its last writer drops. Raw descriptor
//! liveness after the pump returns is meaningful only in a process where no
//! other thread can allocate the freed number, so the `fd_is_open` writer probe
//! runs only when [`scenario_guard`] re-runs a scenario alone in a child.

mod scenario_guard;
mod support;

use std::os::fd::{OwnedFd, RawFd};

use rstest::fixture;
use rstest_bdd::Slot;
use rstest_bdd_macros::scenario;
use support::ObservedPump;

/// The feature file, included so editing it rebuilds this module and the guard.
const FEATURE_TEXT: &str = include_str!("../tests/features/public_stream_entry_points.feature");

/// The path every `#[scenario]` binding must name, relative to the crate root.
const FEATURE_PATH: &str = "tests/features/public_stream_entry_points.feature";

/// One scenario this module declares, binds, and executes.
struct ScenarioExpectation {
    /// The exact `Scenario:` title in the feature file.
    title: &'static str,
    /// The binding function carrying the `#[scenario]` attribute.
    binding: &'static str,
    /// Whether an isolated run must observe the consumed writer's descriptor.
    requires_writer_probe: bool,
}

/// Every scenario this module declares, binds, and executes.
///
/// [`scenario_guard`] compares this table against the feature declarations and
/// the binding attributes, so a scenario that is declared but never bound — the
/// failure mode that once left three of five scenarios unexecuted — fails here.
const SCENARIOS: &[ScenarioExpectation] = &[
    ScenarioExpectation {
        title: "Pump transfers a preloaded payload to the sink",
        binding: "pump_transfers_a_preloaded_payload_to_the_sink",
        requires_writer_probe: true,
    },
    ScenarioExpectation {
        title: "Consume decodes an empty stream",
        binding: "consume_decodes_an_empty_stream",
        requires_writer_probe: false,
    },
    ScenarioExpectation {
        title: "Consume decodes a two-byte sequence split across reads",
        binding: "consume_decodes_a_two_byte_sequence_split_across_reads",
        requires_writer_probe: false,
    },
    ScenarioExpectation {
        title: "Consume decodes a four-byte sequence split across reads",
        binding: "consume_decodes_a_four_byte_sequence_split_across_reads",
        requires_writer_probe: false,
    },
    ScenarioExpectation {
        title: "Consume replaces an invalid byte between reads",
        binding: "consume_replaces_an_invalid_byte_between_reads",
        requires_writer_probe: false,
    },
    ScenarioExpectation {
        title: "Consume replaces a truncated trailing sequence",
        binding: "consume_replaces_a_truncated_trailing_sequence",
        requires_writer_probe: false,
    },
    ScenarioExpectation {
        title: "Pump drains the reader after the destination hangs up",
        binding: "pump_drains_the_reader_after_the_destination_hangs_up",
        requires_writer_probe: false,
    },
    ScenarioExpectation {
        title: "Pump reports a read failure from the source",
        binding: "pump_reports_a_read_failure_from_the_source",
        requires_writer_probe: true,
    },
    ScenarioExpectation {
        title: "Pump closes its writer when the read unwinds",
        binding: "pump_closes_its_writer_when_the_read_unwinds",
        requires_writer_probe: true,
    },
];

/// Scenario state shared between steps through `rstest-bdd` slots.
#[derive(Default)]
struct StreamContext {
    /// The borrowed reader, held open across the public call.
    reader: Slot<OwnedFd>,
    /// Raw number of the borrowed reader, for liveness observation.
    reader_raw: Slot<RawFd>,
    /// The sink read end, retained to observe what the pump delivered.
    sink_reader: Slot<OwnedFd>,
    /// The owned writer, moved into [`crate::pump_stream`].
    sink_writer: Slot<OwnedFd>,
    /// Raw number of the owned writer, for the isolated liveness probe.
    sink_writer_raw: Slot<RawFd>,
    /// The label of the payload the `Given` step preloaded.
    payload_label: Slot<String>,
    /// The pump's return value, reduced to its observable shape.
    pump_outcome: Slot<ObservedPump>,
    /// The text [`crate::consume_stream`] returned.
    consumed: Slot<String>,
    /// Whether the pump's `DEBUG` capture carried the hang-up diagnostic.
    hangup_logged: Slot<bool>,
    /// How often the unwinding reader's `as_fd` was called.
    read_calls: Slot<u32>,
}

#[fixture]
fn context() -> StreamContext {
    // A fresh context per scenario; steps populate its slots as they run.
    StreamContext::default()
}

mod steps;

// One `#[scenario]` binds exactly one scenario, and a binding that omits
// `name` silently takes the first declaration in the file. Each title below is
// therefore named explicitly; `scenario_guard` fails if any declaration, named
// binding, or table entry loses its counterpart.

#[scenario(
    path = "tests/features/public_stream_entry_points.feature",
    name = "Pump transfers a preloaded payload to the sink"
)]
fn pump_transfers_a_preloaded_payload_to_the_sink(context: StreamContext) { let _ = context; }

#[scenario(
    path = "tests/features/public_stream_entry_points.feature",
    name = "Consume decodes an empty stream"
)]
fn consume_decodes_an_empty_stream(context: StreamContext) { let _ = context; }

#[scenario(
    path = "tests/features/public_stream_entry_points.feature",
    name = "Consume decodes a two-byte sequence split across reads"
)]
fn consume_decodes_a_two_byte_sequence_split_across_reads(context: StreamContext) {
    let _ = context;
}

#[scenario(
    path = "tests/features/public_stream_entry_points.feature",
    name = "Consume decodes a four-byte sequence split across reads"
)]
fn consume_decodes_a_four_byte_sequence_split_across_reads(context: StreamContext) {
    let _ = context;
}

#[scenario(
    path = "tests/features/public_stream_entry_points.feature",
    name = "Consume replaces an invalid byte between reads"
)]
fn consume_replaces_an_invalid_byte_between_reads(context: StreamContext) { let _ = context; }

#[scenario(
    path = "tests/features/public_stream_entry_points.feature",
    name = "Consume replaces a truncated trailing sequence"
)]
fn consume_replaces_a_truncated_trailing_sequence(context: StreamContext) { let _ = context; }

#[scenario(
    path = "tests/features/public_stream_entry_points.feature",
    name = "Pump drains the reader after the destination hangs up"
)]
fn pump_drains_the_reader_after_the_destination_hangs_up(context: StreamContext) {
    let _ = context;
}

#[scenario(
    path = "tests/features/public_stream_entry_points.feature",
    name = "Pump reports a read failure from the source"
)]
fn pump_reports_a_read_failure_from_the_source(context: StreamContext) { let _ = context; }

#[scenario(
    path = "tests/features/public_stream_entry_points.feature",
    name = "Pump closes its writer when the read unwinds"
)]
fn pump_closes_its_writer_when_the_read_unwinds(context: StreamContext) { let _ = context; }
