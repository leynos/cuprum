//! Tests for the borrowed file-descriptor ownership contract.

use crate::errors::PumpError;
use crate::io_utils::{classify_write, read_stream};
use crate::pump_machine::WriteEvent;
use crate::test_support::{make_pipe, read_all_from, write_all_to};
use crate::tracing_capture::capture;
use crate::{BufferSize, consume_stream_files, pump_stream_files_readwrite};
use rstest::rstest;
use tracing::Level;

/// Consuming a pipe records its byte total and zero read retries in the span.
#[rstest]
fn consume_records_total_bytes_and_retries_on_span() {
    let (read_end, write_end) = crate::test_support::unwrap_ok(make_pipe());
    let payload = b"boundary-check-payload";
    crate::test_support::unwrap_ok(write_all_to(&write_end, payload));
    // Close the write end so the read loop reaches EOF and terminates.
    drop(write_end);

    let reader = read_end;
    let expected = std::str::from_utf8(payload).unwrap_or("");
    let captured = capture(Level::INFO, || {
        match consume_stream_files(&reader, BufferSize(64)) {
            Ok(text) => assert_eq!(text, expected, "consume must decode the exact payload"),
            Err(err) => panic!("consume over a closed pipe failed: {err:?}"),
        }
    });

    // The completion `span.record` calls must surface the real byte total and
    // the (zero) retry count; removing or miscounting either fails here.
    assert_eq!(
        captured
            .span_field("consume_stream", "total_bytes")
            .as_deref(),
        Some(payload.len().to_string().as_str()),
        "the consume span must record the total bytes read",
    );
    assert_eq!(
        captured
            .span_field("consume_stream", "read_retries")
            .as_deref(),
        Some("0"),
        "no interruptions occurred, so read_retries must record as 0",
    );
}

/// The pump records completion fields even when its span filter allows errors.
#[rstest]
fn pump_records_span_fields_under_error_filter() {
    // Source pipe: the payload the pump reads. Sink pipe: where it writes.
    let (source_read, source_write) = crate::test_support::unwrap_ok(make_pipe());
    let (sink_read, sink_write) = crate::test_support::unwrap_ok(make_pipe());
    let payload = b"pump-span-check";
    crate::test_support::unwrap_ok(write_all_to(&source_write, payload));
    // Close the source's write end so the read loop reaches EOF.
    drop(source_write);

    let reader = source_read;
    let writer = sink_write;
    let captured = capture(Level::ERROR, || {
        match pump_stream_files_readwrite(&reader, &writer, BufferSize(64)) {
            Ok(total) => assert_eq!(total, u64::try_from(payload.len()).unwrap_or(u64::MAX)),
            Err(err) => panic!("pump over pipes failed: {err:?}"),
        }
    });
    // Close the write end, then confirm the sink received exactly the source
    // payload — a data oracle so same-length corruption cannot pass.
    drop(writer);
    assert_eq!(
        crate::test_support::unwrap_ok(read_all_from(&sink_read)).as_slice(),
        &payload[..],
        "the pump must deliver the source bytes unchanged to the sink",
    );

    // The pump completion `span.record` calls must surface the real byte total
    // and the (zero) retry counts even under an ERROR-only filter, where the
    // `error_span!` span still applies; removing any of them fails here.
    assert_eq!(
        captured
            .span_field("pump_stream_readwrite", "total_bytes")
            .as_deref(),
        Some(payload.len().to_string().as_str()),
        "the pump span must record the total bytes written",
    );
    assert_eq!(
        captured
            .span_field("pump_stream_readwrite", "read_retries")
            .as_deref(),
        Some("0"),
        "no interruptions occurred, so read_retries must record as 0",
    );
    assert_eq!(
        captured
            .span_field("pump_stream_readwrite", "write_retries")
            .as_deref(),
        Some("0"),
        "no interruptions occurred, so write_retries must record as 0",
    );
}

/// A successful write is classified with the number of bytes delivered.
#[rstest]
fn classify_write_reports_a_completed_write() {
    let (read_end, write_end) = crate::test_support::unwrap_ok(make_pipe());

    let event = match classify_write(&write_end, b"chunk") {
        Ok(event) => event,
        Err(err) => panic!("write to an open pipe failed: {err:?}"),
    };

    assert_eq!(event, WriteEvent::Complete { bytes: 5 });
    // Keep the read end open until after the write so the pipe never breaks.
    drop(read_end);
}

/// A fatal write error is returned instead of being treated as a closed writer.
#[rstest]
fn classify_write_propagates_a_fatal_error() {
    // Writing to the read end of a pipe is a fatal `EBADF`, which must
    // propagate rather than latch the writer closed.
    let (read_end, _write_end) = crate::test_support::unwrap_ok(make_pipe());

    match classify_write(&read_end, b"chunk") {
        Ok(event) => panic!("expected a fatal write error, got {event:?}"),
        Err(err) => assert!(matches!(err, PumpError::Io(_))),
    }
}

/// A broken writer still drains the reader before the pump reports completion.
#[rstest]
fn pump_drains_the_reader_after_the_writer_breaks() {
    // The downstream stage hangs up before the pump writes anything, which is
    // the real-world `head`-style early exit. The loop must latch the writer
    // closed, keep draining the upstream reader to EOF, and report success
    // with the bytes that actually reached the sink — none, here.
    let (source_read, source_write) = crate::test_support::unwrap_ok(make_pipe());
    let (sink_read, sink_write) = crate::test_support::unwrap_ok(make_pipe());
    let payload = b"payload-written-after-the-downstream-hangs-up";
    crate::test_support::unwrap_ok(write_all_to(&source_write, payload));
    // Close the source's write end so the read loop can reach EOF.
    drop(source_write);
    // Close the sink's read end so every write fails with a broken pipe.
    drop(sink_read);

    let reader = source_read;
    let writer = sink_write;
    // A small buffer forces several read iterations, so the drain path runs
    // repeatedly after the writer has latched closed rather than just once.
    let mut total = 0_u64;
    let captured = capture(Level::DEBUG, || {
        total = match pump_stream_files_readwrite(&reader, &writer, BufferSize(8)) {
            Ok(delivered) => delivered,
            Err(err) => panic!("a broken downstream pipe must not fail the pump: {err:?}"),
        };
    });

    assert_eq!(
        total, 0,
        "no bytes reach a downstream that hung up before the first write",
    );

    // The latch closing is an operational event, not just internal state: the
    // splice path reports it, so the read/write fallback must too, or the same
    // hang-up is diagnosable on one path and silent on the other.
    assert!(
        captured.event_matches(
            Level::DEBUG,
            "broken pipe; draining reader",
            &[("bytes_transferred", "0")],
        ),
        "the writer-close latch must report the hang-up by name, with the zero \
         byte total that reached the hung-up downstream",
    );

    // The reader must have been drained to EOF: a further read returns zero
    // rather than the bytes the pump skipped.
    let mut buffer = [0_u8; 8];
    let remaining = match read_stream(&reader, &mut buffer) {
        Ok(remaining) => remaining,
        Err(err) => panic!("reading the drained source failed: {err:?}"),
    };
    assert_eq!(
        remaining, 0,
        "the pump must drain the upstream reader to EOF after the writer breaks",
    );
}
