//! Direct tests for descriptor-backed I/O helper contracts.

use std::io;
use std::os::fd::{AsRawFd, OwnedFd};

use proptest::prelude::*;

use super::{
    PumpError, WriteOutcome, classify_write_outcome, handle_write, map_short_write_error,
    read_raw_fd, read_raw_fd_with, read_stream, write_all_unix_with,
};
use crate::pump_machine::WriteEvent;
use crate::test_support::{make_pipe, unwrap_err, unwrap_ok, write_all_to};
use rstest::{fixture, rstest};

/// A fresh `pipe(2)` pair (`read_end`, `write_end`) for descriptor-backed
/// tests, so the shared setup lives in one place rather than a repeated
/// `make_pipe()` call per test.
#[fixture]
fn pipe() -> (OwnedFd, OwnedFd) {
    make_pipe()
}

/// Representative error kinds spanning the non-fatal and fatal partitions.
const ERROR_KINDS: [io::ErrorKind; 7] = [
    io::ErrorKind::BrokenPipe,
    io::ErrorKind::ConnectionReset,
    io::ErrorKind::NotFound,
    io::ErrorKind::PermissionDenied,
    io::ErrorKind::WriteZero,
    io::ErrorKind::Interrupted,
    io::ErrorKind::Other,
];

const fn is_nonfatal_kind(kind: io::ErrorKind) -> bool {
    matches!(
        kind,
        io::ErrorKind::BrokenPipe | io::ErrorKind::ConnectionReset
    )
}

fn ssize(len: usize) -> libc::ssize_t {
    // Test buffers are a handful of bytes, so the saturating fallback is
    // never reached; it merely avoids an `expect`/`unwrap` in test code.
    libc::ssize_t::try_from(len).unwrap_or(libc::ssize_t::MAX)
}

#[rstest]
fn read_stream_reads_pipe_bytes(pipe: (OwnedFd, OwnedFd)) {
    let (mut read_end, write_end) = pipe;
    write_all_to(&write_end, b"chunk");
    drop(write_end);
    let mut buffer = [0_u8; 8];

    let read_len = unwrap_ok(read_stream(&mut read_end, &mut buffer));

    assert_eq!(read_len, 5);
    assert_eq!(buffer.get(..read_len), Some(&b"chunk"[..]));
}

#[rstest]
fn read_stream_reports_unreadable_descriptor(pipe: (OwnedFd, OwnedFd)) {
    let (_read_end, mut write_end) = pipe;
    let mut buffer = [0_u8; 8];

    let err = unwrap_err(read_stream(&mut write_end, &mut buffer));

    assert!(matches!(err, PumpError::Io(_)));
}

#[rstest]
fn read_raw_fd_reports_eof(pipe: (OwnedFd, OwnedFd)) {
    let (read_end, write_end) = pipe;
    drop(write_end);
    let mut buffer = [0_u8; 8];

    let read_len = unwrap_ok(read_raw_fd(read_end.as_raw_fd(), &mut buffer));

    assert_eq!(read_len, 0);
}

#[test]
fn read_raw_fd_retries_after_interruption() {
    let mut attempts = 0_u8;

    let read_len = unwrap_ok(read_raw_fd_with(|| {
        attempts = attempts.saturating_add(1);
        if attempts == 1 {
            return Err(io::Error::from(io::ErrorKind::Interrupted));
        }
        Ok(0)
    }));

    assert_eq!(read_len, 0);
    assert_eq!(attempts, 2);
}

#[rstest]
fn handle_write_returns_complete_outcome(pipe: (OwnedFd, OwnedFd)) {
    let (read_end, mut write_end) = pipe;

    let outcome = unwrap_ok(handle_write(&mut write_end, b"chunk"));

    assert_eq!(outcome, WriteOutcome::Complete(5));
    drop(read_end);
}

#[rstest]
fn handle_write_reports_unwritable_descriptor(pipe: (OwnedFd, OwnedFd)) {
    let (mut read_end, _write_end) = pipe;

    let err = unwrap_err(handle_write(&mut read_end, b"chunk"));

    assert!(matches!(err, PumpError::Io(_)));
}

#[test]
fn nonfatal_short_write_records_accepted_bytes() {
    let outcome = unwrap_ok(map_short_write_error(
        io::Error::from(io::ErrorKind::BrokenPipe),
        3,
    ));

    assert_eq!(outcome, WriteOutcome::NonFatalShortWrite(3));
}

#[test]
fn fatal_short_write_errors_propagate() {
    let err = unwrap_err(map_short_write_error(
        io::Error::from(io::ErrorKind::PermissionDenied),
        3,
    ));

    assert!(matches!(err, PumpError::Io(_)));
}

#[test]
fn write_all_unix_with_retries_after_interruption() {
    let mut attempts = 0_u8;

    let outcome = unwrap_ok(write_all_unix_with(b"chunk", |buffer| {
        attempts = attempts.saturating_add(1);
        if attempts == 1 {
            return Err(io::Error::from(io::ErrorKind::Interrupted));
        }
        Ok(ssize(buffer.len()))
    }));

    assert_eq!(outcome, WriteOutcome::Complete(5));
    assert_eq!(attempts, 2);
}

#[test]
fn write_all_unix_with_reports_zero_progress() {
    let err = unwrap_err(write_all_unix_with(b"chunk", |_| Ok(0)));

    assert!(matches!(err, PumpError::Io(ref io_err) if io_err.kind() == io::ErrorKind::WriteZero));
}

#[test]
fn write_all_unix_with_accumulates_partial_writes() {
    let mut calls = 0_u8;

    let outcome = unwrap_ok(write_all_unix_with(b"chunk", |buffer| {
        calls = calls.saturating_add(1);
        let take = if calls == 1 { 2 } else { buffer.len() };
        Ok(ssize(take))
    }));

    assert_eq!(outcome, WriteOutcome::Complete(5));
    assert_eq!(calls, 2);
}

#[test]
fn write_all_unix_with_rejects_overlong_progress() {
    let err = unwrap_err(write_all_unix_with(b"ab", |buffer| {
        Ok(ssize(buffer.len() + 1))
    }));

    assert!(matches!(err, PumpError::BufferRangeExceeded));
}

#[test]
fn write_all_unix_with_treats_broken_pipe_as_nonfatal_short_write() {
    let mut calls = 0_u8;

    let outcome = unwrap_ok(write_all_unix_with(b"chunk", |_| {
        calls = calls.saturating_add(1);
        if calls == 1 {
            return Ok(2);
        }
        Err(io::Error::from(io::ErrorKind::BrokenPipe))
    }));

    assert_eq!(outcome, WriteOutcome::NonFatalShortWrite(2));
}

#[test]
fn write_all_unix_with_propagates_fatal_error() {
    let err = unwrap_err(write_all_unix_with(b"chunk", |_| {
        Err(io::Error::from(io::ErrorKind::PermissionDenied))
    }));

    assert!(matches!(err, PumpError::Io(_)));
}

proptest! {
    /// Only broken-pipe and connection-reset kinds are non-fatal writes.
    #[test]
    fn nonfatal_classification_matches_kind(
        kind in proptest::sample::select(ERROR_KINDS.to_vec()),
    ) {
        let err = PumpError::from(io::Error::from(kind));
        prop_assert_eq!(err.is_nonfatal_write(), is_nonfatal_kind(kind));
    }

    /// `map_short_write_error` suppresses exactly the non-fatal kinds and
    /// preserves the accepted byte total when it does.
    #[test]
    fn map_short_write_error_suppresses_only_nonfatal(
        kind in proptest::sample::select(ERROR_KINDS.to_vec()),
        total in any::<u64>(),
    ) {
        let result = map_short_write_error(io::Error::from(kind), total);
        match result {
            Ok(WriteOutcome::NonFatalShortWrite(bytes)) => {
                prop_assert!(is_nonfatal_kind(kind));
                prop_assert_eq!(bytes, total);
            }
            Ok(other) => prop_assert!(false, "unexpected outcome {:?}", other),
            Err(_) => prop_assert!(!is_nonfatal_kind(kind)),
        }
    }
}

#[rstest]
#[case::zero_bytes(0)]
#[case::positive_bytes(7)]
fn classify_write_outcome_maps_nonfatal_short_write_to_closed(#[case] accepted: u64) {
    // A non-fatal short write latches the writer closed while preserving the
    // bytes accepted before the pipe broke — including none at all.
    let event = unwrap_ok(classify_write_outcome(Ok(
        WriteOutcome::NonFatalShortWrite(accepted),
    )));

    assert_eq!(event, WriteEvent::Closed { bytes: accepted });
}

#[test]
fn classify_write_outcome_maps_complete_write_to_complete() {
    let event = unwrap_ok(classify_write_outcome(Ok(WriteOutcome::Complete(5))));

    assert_eq!(event, WriteEvent::Complete { bytes: 5 });
}

#[test]
fn classify_write_outcome_propagates_fatal_errors() {
    // map_short_write_error has already absorbed the non-fatal partition, so
    // every error arriving here is fatal and must not latch the writer closed.
    let err = unwrap_err(classify_write_outcome(Err(PumpError::from(
        io::Error::from(io::ErrorKind::PermissionDenied),
    ))));

    assert!(matches!(err, PumpError::Io(_)));
}
