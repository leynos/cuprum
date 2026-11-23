//! Step definitions for the public Unix stream scenarios.
//!
//! These live in a child module purely to keep this fixture file under the
//! project's per-file limit; `rstest-bdd` resolves fixtures by name at run
//! time, so a step in a child module reaches the parent's `context` fixture
//! exactly as a step beside it would. The parent owns the feature text, the
//! expectation table, the shared context, and the bindings.

use std::{
    cell::Cell,
    os::fd::AsRawFd,
    panic::{AssertUnwindSafe, catch_unwind},
};

use cuprum_native_io::fd_is_open;
use rstest_bdd::StepResult;
use rstest_bdd_macros::{given, then, when};
use tracing::Level;

use super::{
    StreamContext,
    support,
    support::{
        EOF_DEADLINE,
        ObservedPump,
        PanickingReader,
        UNWIND_MARKER,
        buffer_size,
        in_isolated_child,
        observe_pump,
        payload_for,
        print_writer_probe_witness,
        recorded,
        sink_expectation,
        take,
    },
};
use crate::{
    consume_stream,
    pump_stream,
    test_support::{make_pipe, read_to_eof_bounded, write_all_to},
    tracing_capture::capture,
};

/// Resolve the payload label the `Given` step recorded.
fn recorded_payload(context: &StreamContext) -> StepResult<&'static support::Payload, String> {
    let label = recorded(&context.payload_label, "payload label")?;
    payload_for(&label)
}

/// Resolve the payload label when one was recorded, and `None` otherwise.
fn optional_payload(
    context: &StreamContext,
) -> StepResult<Option<&'static support::Payload>, String> {
    match context.payload_label.get() {
        Some(label) => payload_for(&label).map(Some),
        None => Ok(None),
    }
}

#[given("a source pipe preloaded with the {label} payload")]
fn a_source_pipe_preloaded_with(context: &StreamContext, label: String) -> StepResult<(), String> {
    let payload = payload_for(&label)?;
    let (reader, writer) =
        make_pipe().map_err(|error| format!("source pipe creation failed: {error}"))?;
    // A pipe holds 64 KiB by default, so a payload far below that never blocks
    // this write; the suite deliberately stays small.
    write_all_to(&writer, payload.bytes)
        .map_err(|error| format!("source pipe write failed: {error}"))?;
    // Closing the write end is what lets both entry points reach EOF.
    drop(writer);
    context.reader_raw.set(reader.as_raw_fd());
    context.reader.set(reader);
    context.payload_label.set(label);
    Ok(())
}

#[given("a source write end serving as the reader")]
fn a_source_write_end_serving_as_the_reader(context: &StreamContext) -> StepResult<(), String> {
    let (reader, writer) =
        make_pipe().map_err(|error| format!("source pipe creation failed: {error}"))?;
    // Reading a pipe's write end fails with `EBADF`, which gives the pump a real
    // descriptor failure without a scripted seam.
    drop(reader);
    context.reader_raw.set(writer.as_raw_fd());
    context.reader.set(writer);
    Ok(())
}

#[given("a sink pipe")]
fn a_sink_pipe(context: &StreamContext) -> StepResult<(), String> {
    let (reader, writer) =
        make_pipe().map_err(|error| format!("sink pipe creation failed: {error}"))?;
    context.sink_reader.set(reader);
    context.sink_writer_raw.set(writer.as_raw_fd());
    context.sink_writer.set(writer);
    Ok(())
}

#[given("a sink pipe whose reader is closed")]
fn a_sink_pipe_whose_reader_is_closed(context: &StreamContext) -> StepResult<(), String> {
    let (reader, writer) =
        make_pipe().map_err(|error| format!("sink pipe creation failed: {error}"))?;
    // The hang-up: every write the pump attempts meets `EPIPE`, which the pump
    // must treat as a drain signal rather than a failure.
    drop(reader);
    context.sink_writer_raw.set(writer.as_raw_fd());
    context.sink_writer.set(writer);
    Ok(())
}

#[given("a panicking reader over a live descriptor")]
fn a_panicking_reader_over_a_live_descriptor(context: &StreamContext) -> StepResult<(), String> {
    let (reader, writer) =
        make_pipe().map_err(|error| format!("source pipe creation failed: {error}"))?;
    // The write end is irrelevant here; dropping it keeps the descriptor table
    // as small as the unwind probe needs.
    drop(writer);
    context.reader_raw.set(reader.as_raw_fd());
    context.reader.set(reader);
    Ok(())
}

#[when("the public pump runs with buffer size {size}")]
fn the_public_pump_runs(context: &StreamContext, size: isize) -> StepResult<(), String> {
    let buffer_size = buffer_size(size)?;
    let writer = take(&context.sink_writer, "sink writer")?;
    let mut result = None;
    let captured = capture(Level::DEBUG, || {
        result = context
            .reader
            .with_ref(|reader| pump_stream(reader, writer, buffer_size));
    });
    let result = result
        .ok_or_else(|| "no borrowed reader was recorded; a Given step must run first".to_owned())?;
    context.hangup_logged.set(captured.event_matches(
        Level::DEBUG,
        "broken pipe; draining reader",
        &[("bytes_transferred", "0")],
    ));
    context.pump_outcome.set(observe_pump(result));
    Ok(())
}

#[when("the public consume runs with buffer size {size}")]
fn the_public_consume_runs(context: &StreamContext, size: isize) -> StepResult<(), String> {
    let buffer_size = buffer_size(size)?;
    let text = context
        .reader
        .with_ref(|reader| consume_stream(reader, buffer_size))
        .ok_or_else(|| "no borrowed reader was recorded; a Given step must run first".to_owned())?
        .map_err(|error| format!("consume over a real pipe failed: {error:?}"))?;
    context.consumed.set(text);
    Ok(())
}

#[when("the public pump unwinds during its read")]
fn the_public_pump_unwinds(context: &StreamContext) -> StepResult<(), String> {
    let buffer_size = buffer_size(8)?;
    let writer = take(&context.sink_writer, "sink writer")?;
    let calls = Cell::new(0_u32);
    let outcome = context
        .reader
        .with_ref(|descriptor| {
            let reader = PanickingReader::new(descriptor, &calls);
            catch_unwind(AssertUnwindSafe(|| {
                pump_stream(&reader, writer, buffer_size)
            }))
        })
        .ok_or_else(|| "no borrowed reader was recorded; a Given step must run first".to_owned())?;
    context.read_calls.set(calls.get());
    context.pump_outcome.set(match outcome {
        Ok(result) => observe_pump(result),
        Err(payload) => ObservedPump::Panicked(
            payload
                .downcast_ref::<&'static str>()
                .map(|marker| (*marker).to_owned()),
        ),
    });
    Ok(())
}

#[then("the pump reports the payload length as transferred")]
fn the_pump_reports_the_payload_length(context: &StreamContext) -> StepResult<(), String> {
    let payload = recorded_payload(context)?;
    let expected = u64::try_from(payload.bytes.len())
        .map_err(|error| format!("payload length does not fit u64: {error}"))?;
    match recorded(&context.pump_outcome, "pump outcome")? {
        ObservedPump::Transferred(bytes) if bytes == expected => Ok(()),
        other => Err(format!(
            "the pump must report {expected} transferred bytes, found {other:?}"
        )),
    }
}

#[then("the pump reports nothing transferred")]
fn the_pump_reports_nothing_transferred(context: &StreamContext) -> StepResult<(), String> {
    match recorded(&context.pump_outcome, "pump outcome")? {
        ObservedPump::Transferred(0) => Ok(()),
        other => Err(format!(
            "no bytes may reach a downstream that hung up, found {other:?}"
        )),
    }
}

#[then("the pump reports a stream I/O failure")]
fn the_pump_reports_a_stream_io_failure(context: &StreamContext) -> StepResult<(), String> {
    match recorded(&context.pump_outcome, "pump outcome")? {
        // The errno is deliberately not asserted: which call fails and with
        // what code is a platform detail, while `PumpError::Io` is the contract.
        ObservedPump::IoFailure => Ok(()),
        other => Err(format!(
            "the pump must surface a descriptor failure as PumpError::Io, found {other:?}"
        )),
    }
}

#[then("the pump logged the broken-pipe drain")]
fn the_pump_logged_the_broken_pipe_drain(context: &StreamContext) -> StepResult<(), String> {
    if recorded(&context.hangup_logged, "hang-up capture")? {
        Ok(())
    } else {
        Err(concat!(
            "the pump must log \"broken pipe; draining reader\" with ",
            "bytes_transferred = 0 at DEBUG",
        )
        .to_owned())
    }
}

#[then("the borrowed reader is still open")]
fn the_borrowed_reader_is_still_open(context: &StreamContext) -> StepResult<(), String> {
    let raw = recorded(&context.reader_raw, "borrowed reader descriptor")?;
    match fd_is_open(raw) {
        Ok(true) => Ok(()),
        Ok(false) => Err(format!(
            "the pump must not close the caller's reader, but descriptor {raw} is closed"
        )),
        Err(error) => Err(format!("descriptor {raw} observation failed: {error}")),
    }
}

#[then("the consume output equals the decoded payload")]
fn the_consume_output_equals_the_decoded_payload(
    context: &StreamContext,
) -> StepResult<(), String> {
    let payload = recorded_payload(context)?;
    let output = recorded(&context.consumed, "consume output")?;
    if output == payload.decoded {
        Ok(())
    } else {
        Err(format!(
            "consume must decode the {} payload to {:?}, found {output:?}",
            payload.label, payload.decoded,
        ))
    }
}

#[then("the source is drained to end of input")]
fn the_source_is_drained_to_end_of_input(context: &StreamContext) -> StepResult<(), String> {
    let remaining = context
        .reader
        .with_ref(|reader| read_to_eof_bounded(reader, EOF_DEADLINE))
        .ok_or_else(|| "no borrowed reader was recorded; a Given step must run first".to_owned())?
        .map_err(|error| format!("the source EOF observation failed: {error}"))?;
    if remaining.is_empty() {
        Ok(())
    } else {
        Err(format!(
            "the pump must drain the source to EOF, but {} bytes remained",
            remaining.len(),
        ))
    }
}

#[then("the read unwinds exactly once")]
fn the_read_unwinds_exactly_once(context: &StreamContext) -> StepResult<(), String> {
    match recorded(&context.pump_outcome, "pump outcome")? {
        ObservedPump::Panicked(Some(marker)) if marker == UNWIND_MARKER => {}
        other => {
            return Err(format!(
                "the pump must propagate the injected unwind, found {other:?}"
            ));
        }
    }
    let calls = recorded(&context.read_calls, "reader call count")?;
    if calls == 1 {
        Ok(())
    } else {
        Err(format!(
            "the pump must ask the reader for its descriptor exactly once, saw {calls}"
        ))
    }
}

#[then("the pump closed its writer with the sink holding {contents}")]
fn the_pump_closed_its_writer(context: &StreamContext, contents: String) -> StepResult<(), String> {
    let expected = sink_expectation(&contents, optional_payload(context)?)?;

    // The liveness probe runs before any descriptor is duplicated below: the
    // sink read allocates a duplicate, which could reuse the closed writer's
    // number and turn a closed descriptor into a false open one.
    if in_isolated_child() {
        let raw = recorded(&context.sink_writer_raw, "owned writer descriptor")?;
        match fd_is_open(raw) {
            Ok(false) => {}
            Ok(true) => {
                return Err(format!(
                    "the pump must close its owned writer, but descriptor {raw} is open"
                ));
            }
            Err(error) => return Err(format!("descriptor {raw} observation failed: {error}")),
        }
    }

    let observed = context
        .sink_reader
        .with_ref(|reader| read_to_eof_bounded(reader, EOF_DEADLINE))
        .ok_or_else(|| "no sink reader was recorded; a Given step must run first".to_owned())?
        .map_err(|error| format!("the sink EOF observation failed: {error}"))?;

    if observed != expected {
        return Err(format!(
            "the sink must hold {expected:?} once the writer closes, found {observed:?}"
        ));
    }

    if in_isolated_child() {
        print_writer_probe_witness();
    }
    Ok(())
}
