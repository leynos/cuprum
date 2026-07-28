//! Tests for the borrowed file-descriptor ownership contract.

use std::io::Read;
use std::os::fd::{AsRawFd, FromRawFd, IntoRawFd, OwnedFd};
use std::panic::{AssertUnwindSafe, catch_unwind};

use crate::io_utils::read_stream;
use crate::test_support::{fd_is_open, make_pipe, write_all_to};
use crate::tracing_capture::capture;
use crate::{
    BufferSize, consume_stream_files, pump_stream_files_readwrite, stream_from_raw,
    with_borrowed_reader,
};
use rstest::{fixture, rstest};
use tracing::Level;

struct BorrowedReaderPipe {
    read_end: OwnedFd,
    write_end: OwnedFd,
    raw_fd: i32,
}

enum BorrowedReaderScenario {
    Panic,
    Success,
}

#[fixture]
fn borrowed_reader_pipe() -> BorrowedReaderPipe {
    let (read_end, write_end) = make_pipe();
    let raw_fd = read_end.as_raw_fd();

    BorrowedReaderPipe {
        read_end,
        write_end,
        raw_fd,
    }
}

#[rstest]
#[case::panicking_operation(BorrowedReaderScenario::Panic)]
#[case::successful_operation(BorrowedReaderScenario::Success)]
fn borrowed_reader_stays_open_after_operation(
    borrowed_reader_pipe: BorrowedReaderPipe,
    #[case] scenario: BorrowedReaderScenario,
) {
    let BorrowedReaderPipe {
        read_end,
        write_end,
        raw_fd,
    } = borrowed_reader_pipe;

    match scenario {
        BorrowedReaderScenario::Panic => assert_panicking_reader_keeps_fd_open(raw_fd),
        BorrowedReaderScenario::Success => {
            assert_successful_reader_keeps_fd_usable(raw_fd, write_end);
        }
    }

    assert!(fd_is_open(raw_fd), "the borrowed FD must remain open");
    drop(read_end);
}

#[rstest]
fn stream_from_raw_owns_and_reads_the_descriptor() {
    let (read_end, write_end) = make_pipe();
    write_all_to(&write_end, b"pong");
    drop(write_end);

    // Transfer ownership of the read descriptor into the constructed handle
    // exactly once, then read the pending bytes back through it.
    let raw_fd = read_end.into_raw_fd();
    let mut handle = stream_from_raw(raw_fd);
    let mut buffer = [0_u8; 8];

    let read_len = match read_stream(&mut handle, &mut buffer) {
        Ok(read_len) => read_len,
        Err(err) => panic!("read through stream_from_raw handle failed: {err:?}"),
    };

    assert_eq!(buffer.get(..read_len), Some(&b"pong"[..]));
    // Dropping `handle` closes the owned descriptor.
    drop(handle);
    assert!(!fd_is_open(raw_fd), "the owned FD must be closed on drop");
}

#[rstest]
fn consume_records_total_bytes_and_retries_on_span() {
    let (read_end, write_end) = make_pipe();
    let payload = b"boundary-check-payload";
    write_all_to(&write_end, payload);
    // Close the write end so the read loop reaches EOF and terminates.
    drop(write_end);

    let mut reader = read_end;
    let captured = capture(Level::INFO, || {
        match consume_stream_files(&mut reader, BufferSize(64)) {
            Ok(text) => assert_eq!(text.len(), payload.len()),
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

#[rstest]
fn pump_records_span_fields_under_error_filter() {
    // Source pipe: the payload the pump reads. Sink pipe: where it writes.
    let (source_read, source_write) = make_pipe();
    let (sink_read, sink_write) = make_pipe();
    let payload = b"pump-span-check";
    write_all_to(&source_write, payload);
    // Close the source's write end so the read loop reaches EOF.
    drop(source_write);

    let mut reader = source_read;
    let mut writer = sink_write;
    let captured = capture(Level::ERROR, || {
        match pump_stream_files_readwrite(&mut reader, &mut writer, BufferSize(64)) {
            Ok(total) => assert_eq!(total, u64::try_from(payload.len()).unwrap_or(u64::MAX)),
            Err(err) => panic!("pump over pipes failed: {err:?}"),
        }
    });
    // The sink read end is held open through the pump so writes do not break.
    drop(sink_read);

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

fn assert_panicking_reader_keeps_fd_open(raw_fd: i32) {
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        with_borrowed_reader(raw_fd, |_reader| {
            panic!("simulated failure inside the borrowed scope");
        });
    }));

    assert!(outcome.is_err(), "the panic must propagate to the caller");
}

fn assert_successful_reader_keeps_fd_usable(raw_fd: i32, write_end: OwnedFd) {
    write_all_to(&write_end, b"ping");
    drop(write_end);

    let collected = with_borrowed_reader(raw_fd, |reader| {
        // SAFETY: reading through the borrowed handle's raw descriptor.
        let mut file =
            unsafe { std::mem::ManuallyDrop::new(std::fs::File::from_raw_fd(reader.as_raw_fd())) };
        let mut data = Vec::new();
        match file.read_to_end(&mut data) {
            Ok(_) => {}
            Err(err) => panic!("pipe read failed: {err}"),
        }
        data
    });

    assert_eq!(collected, b"ping");
}
