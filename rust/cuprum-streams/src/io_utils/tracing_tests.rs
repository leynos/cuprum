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

use proptest::prelude::*;
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
fn error_filter_keeps_ordinary_fatal_write_context() {
    let captured = capture(Level::ERROR, || {
        let span = operation_span("pump_stream_readwrite", 2048);
        let _guard = span.enter();
        // An ordinary, non-interrupted, non-broken-pipe error makes progress
        // impossible and routes through `map_short_write_error`, a distinct
        // fatal `error!` site from the zero-progress branch above.
        let outcome = write_all_unix_with(b"payload", |_chunk| {
            Err(io::Error::from(io::ErrorKind::Other))
        });
        assert!(outcome.is_err(), "a non-nonfatal write error is fatal",);
    });

    assert!(
        captured.event_has_fields(Level::ERROR, &["operation", "buffer_size"]),
        "map_short_write_error's fatal event must retain operation + \
         buffer_size context under an error filter",
    );
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

#[test]
fn debug_filter_captures_successful_read_event() {
    let captured = capture(Level::DEBUG, || {
        // A successful read logs a `debug` event with the byte count and
        // platform.
        let outcome = read_raw_fd_with(|| Ok(4));
        assert!(outcome.is_ok(), "the injected read should succeed");
    });

    assert!(
        captured.event_has_fields(Level::DEBUG, &["bytes", "platform"]),
        "a successful read must log a debug event carrying bytes + platform",
    );
}

#[test]
fn debug_filter_captures_successful_write_event() {
    let captured = capture(Level::DEBUG, || {
        // A completed write logs a `debug` event with the byte count and
        // platform.
        let outcome = write_all_unix_with(b"data", |chunk| {
            libc::ssize_t::try_from(chunk.len()).map_err(|_| io::Error::from(io::ErrorKind::Other))
        });
        assert!(outcome.is_ok(), "the injected write should succeed");
    });

    assert!(
        captured.event_has_fields(Level::DEBUG, &["bytes", "platform"]),
        "a successful write must log a debug event carrying bytes + platform",
    );
}

proptest! {
    /// The read counter equals the number of `EINTR` retries for any sequence
    /// of interruptions before the final successful read.
    #[test]
    fn read_seam_counts_arbitrary_interrupts(interrupts in 0_u32..64) {
        reset_retry_counters();
        let mut remaining = interrupts;
        let outcome = read_raw_fd_with(|| {
            if remaining > 0 {
                remaining -= 1;
                return Err(io::Error::from(io::ErrorKind::Interrupted));
            }
            Ok(0)
        });
        prop_assert!(outcome.is_ok());
        prop_assert_eq!(read_retry_count(), u64::from(interrupts));
    }

    /// The write counter equals the number of `EINTR` retries for any sequence
    /// of interruptions before the write completes.
    #[test]
    fn write_seam_counts_arbitrary_interrupts(interrupts in 0_u32..64) {
        reset_retry_counters();
        let mut remaining = interrupts;
        let outcome = write_all_unix_with(b"x", |chunk| {
            if remaining > 0 {
                remaining -= 1;
                return Err(io::Error::from(io::ErrorKind::Interrupted));
            }
            libc::ssize_t::try_from(chunk.len()).map_err(|_| io::Error::from(io::ErrorKind::Other))
        });
        prop_assert!(outcome.is_ok());
        prop_assert_eq!(write_retry_count(), u64::from(interrupts));
    }
}
