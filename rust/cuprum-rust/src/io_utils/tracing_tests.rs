//! Tests that the pump operation span keeps `warn!`/`error!` context under the
//! production `warn`/`error` tracing filters, and that the read/write seams
//! count their EINTR retries.
//!
//! The read/write seams attach only local fields (`platform`, `error`) to their
//! EINTR `warn!` and fatal-I/O `error!` events; the operation name and
//! `buffer_size` come from the enclosing [`operation_span`]. If that span were
//! created at INFO level it would be disabled whenever a subscriber filters out
//! `info` — a common production configuration — and those events would lose
//! their operation context. These tests pin the span to a level that survives
//! `warn`- and `error`-only filters, and pin the retry accounting the span
//! reports.

use std::io;

use tracing::Level;

use super::{
    operation_span, read_raw_fd_with, read_retry_count, reset_retry_counters, write_all_unix_with,
    write_retry_count,
};
use crate::tracing_capture::capture;

#[test]
fn warn_filter_keeps_eintr_warning_context() {
    let captured = capture(Level::WARN, || {
        let span = operation_span("pump_stream_readwrite", 4096);
        let _guard = span.enter();
        // Interrupt once, then report EOF, so the read seam emits its EINTR
        // `warn!` inside the operation span.
        let mut attempts = 0_u8;
        let outcome = read_raw_fd_with(|| {
            attempts = attempts.saturating_add(1);
            if attempts == 1 {
                return Err(io::Error::from(io::ErrorKind::Interrupted));
            }
            Ok(0)
        });
        assert!(
            outcome.is_ok(),
            "the read seam should retry past the interrupt and reach EOF",
        );
    });

    assert!(
        captured.event_has_fields(Level::WARN, &["operation", "buffer_size"]),
        "EINTR warn event must retain operation + buffer_size context under a \
         warn filter",
    );
}

#[test]
fn error_filter_keeps_fatal_write_context() {
    let captured = capture(Level::ERROR, || {
        let span = operation_span("pump_stream_readwrite", 8192);
        let _guard = span.enter();
        // A write that makes zero progress is fatal and emits the seam's
        // `error!` inside the operation span.
        let outcome = write_all_unix_with(b"payload", |_chunk| Ok(0));
        assert!(outcome.is_err(), "a zero-progress write is fatal");
    });

    assert!(
        captured.event_has_fields(Level::ERROR, &["operation", "buffer_size"]),
        "fatal write error event must retain operation + buffer_size context \
         under an error filter",
    );
}

#[test]
fn read_seam_counts_eintr_retries() {
    reset_retry_counters();
    let mut attempts = 0_u8;
    let outcome = read_raw_fd_with(|| {
        attempts = attempts.saturating_add(1);
        if attempts <= 2 {
            return Err(io::Error::from(io::ErrorKind::Interrupted));
        }
        Ok(0)
    });

    assert!(
        outcome.is_ok(),
        "the read seam should reach EOF after retries"
    );
    assert_eq!(read_retry_count(), 2, "each EINTR retry must be counted");
}

#[test]
fn write_seam_counts_eintr_retries() {
    reset_retry_counters();
    let mut attempts = 0_u8;
    let outcome = write_all_unix_with(b"x", |chunk| {
        attempts = attempts.saturating_add(1);
        if attempts <= 2 {
            return Err(io::Error::from(io::ErrorKind::Interrupted));
        }
        libc::ssize_t::try_from(chunk.len()).map_err(|_| io::Error::from(io::ErrorKind::Other))
    });

    assert!(
        outcome.is_ok(),
        "the write seam should complete after retries"
    );
    assert_eq!(write_retry_count(), 2, "each EINTR retry must be counted");
}

#[test]
fn error_filter_keeps_read_overflow_context() {
    let captured = capture(Level::ERROR, || {
        let span = operation_span("consume_stream", 512);
        let _guard = span.enter();
        // A negative `ssize_t` cannot convert to `usize`, exercising the read
        // length-overflow branch, which emits a fatal `error!` before failing.
        let outcome = read_raw_fd_with(|| Ok(-1));
        assert!(
            outcome.is_err(),
            "a negative read length is a fatal overflow",
        );
    });

    assert!(
        captured.event_has_fields(Level::ERROR, &["operation", "buffer_size"]),
        "read length-overflow error event must retain operation + buffer_size \
         context under an error filter",
    );
}
