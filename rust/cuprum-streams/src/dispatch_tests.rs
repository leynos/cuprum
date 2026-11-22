//! Linux dispatcher tests for `pump_stream`'s transfer-strategy choice.
//!
//! `pump_stream_files` is the only place that decides whether a pump splices
//! or runs the read/write loop, and each strategy already has tests of its
//! own. These tests enter through the public [`crate::pump_stream`] so the
//! decision itself is under test: the selected strategy must leave its own
//! trace and the strategy not selected must leave none. A byte count alone
//! cannot tell the two branches apart, because either delivers the payload.
//!
//! The module is Linux-only, matching the `#[cfg(target_os = "linux")]` splice
//! arm, and is excluded under Miri, which cannot execute the unshimmed splice
//! syscall on real descriptors.

use std::{
    io::{self, Write},
    os::fd::OwnedFd,
};

use rstest::{fixture, rstest};
use tempfile::NamedTempFile;
use tracing::Level;

use crate::{
    BufferSize,
    errors::PumpError,
    pump_stream,
    test_support::{make_pipe, read_all_from, unwrap_ok, write_all_to},
    tracing_capture::{Captured, capture},
};

/// The diagnostic the splice path emits when it hands over to read/write.
const FALLBACK_MESSAGE: &str =
    "splice unsupported for these descriptors; using read/write fallback";

/// The `operation` field of the read/write fallback's span.
const READWRITE_OPERATION: &str = "pump_stream_readwrite";

/// Payload short enough for one pipe buffer and one transfer chunk.
const PAYLOAD: &[u8] = b"dispatch payload";

/// A connected `pipe(2)` pair: `(read_end, write_end)`.
type PipePair = (OwnedFd, OwnedFd);

/// A `pipe(2)` pair as an [`rstest`] fixture wrapping
/// [`crate::test_support::make_pipe`].
// `fn_single_line` in the pinned formatter turns this rstest fixture into a
// form that triggers `unused_braces` under Rust 1.87; `splice/tests.rs`
// carries the same skip for the same reason.
#[rustfmt::skip]
#[fixture]
fn pipe() -> io::Result<PipePair> {
    make_pipe()
}

/// The buffer size every dispatcher scenario pumps with.
fn buffer_size() -> BufferSize { unwrap_ok(BufferSize::new(65_536)) }

/// Assert which strategy the dispatcher selected, naming the scenario.
///
/// `fallback_events` is the exact number of support-detection diagnostics the
/// run emitted, and `readwrite` states whether the fallback's operation span
/// must be present. Both signals come from the trace, so a run that delivers
/// the payload through the wrong branch still fails.
fn assert_strategy(captured: &Captured, scenario: &str, fallback_events: usize, readwrite: bool) {
    assert_eq!(
        captured.event_count(Level::DEBUG, FALLBACK_MESSAGE),
        fallback_events,
        "{scenario}: the splice support-detection event must fire exactly {fallback_events} \
         time(s)",
    );

    let span_operation = captured.span_field(READWRITE_OPERATION, "operation");
    if readwrite {
        assert_eq!(
            span_operation.as_deref(),
            Some(READWRITE_OPERATION),
            "{scenario}: the read/write fallback must open its operation span",
        );
    } else {
        assert!(
            span_operation.is_none(),
            "{scenario}: the read/write fallback span must be absent, got {span_operation:?}",
        );
    }
}

/// A pipe pair splices: neither fallback signal appears.
#[rstest]
fn pipe_pair_pumps_through_splice(
    #[from(pipe)] source_result: io::Result<PipePair>,
    #[from(pipe)] sink_result: io::Result<PipePair>,
) {
    let (source_read, source_write) = unwrap_ok(source_result);
    let (sink_read, sink_write) = unwrap_ok(sink_result);
    unwrap_ok(write_all_to(&source_write, PAYLOAD));
    // Close the source's write end so the splice loop reaches EOF.
    drop(source_write);

    let mut outcome = None;
    let captured = capture(Level::DEBUG, || {
        outcome = Some(pump_stream(&source_read, sink_write, buffer_size()));
    });
    let outcome = match outcome {
        Some(outcome) => outcome,
        None => panic!("the pump must run inside the capture closure"),
    };

    match outcome {
        Ok(total) => assert_eq!(
            total,
            unwrap_ok(u64::try_from(PAYLOAD.len())),
            "splice must count every payload byte",
        ),
        Err(error) => panic!("a pipe pair must pump through splice, got {error:?}"),
    }
    assert_eq!(
        unwrap_ok(read_all_from(&sink_read)).as_slice(),
        PAYLOAD,
        "the sink must receive the source payload unchanged",
    );
    assert_strategy(&captured, "pipe-to-pipe splice", 0, false);
}

/// Two regular files cannot splice: the dispatcher falls back exactly once.
#[rstest]
fn regular_files_pump_through_the_fallback() {
    let mut source_file = unwrap_ok(NamedTempFile::new());
    let destination_file = unwrap_ok(NamedTempFile::new());
    unwrap_ok(source_file.write_all(PAYLOAD));
    // `reopen()` yields an independent descriptor to the same inode at
    // offset zero; a racy path lookup would not.
    let reader = OwnedFd::from(unwrap_ok(source_file.reopen()));
    let writer = OwnedFd::from(unwrap_ok(destination_file.reopen()));

    let mut outcome = None;
    let captured = capture(Level::DEBUG, || {
        outcome = Some(pump_stream(&reader, writer, buffer_size()));
    });
    let outcome = match outcome {
        Some(outcome) => outcome,
        None => panic!("the pump must run inside the capture closure"),
    };

    match outcome {
        Ok(total) => assert_eq!(
            total,
            unwrap_ok(u64::try_from(PAYLOAD.len())),
            "the fallback must count every payload byte",
        ),
        Err(error) => panic!("two regular files must pump through the fallback, got {error:?}"),
    }

    let destination_reader = OwnedFd::from(unwrap_ok(destination_file.reopen()));
    assert_eq!(
        unwrap_ok(read_all_from(&destination_reader)).as_slice(),
        PAYLOAD,
        "the fallback must deliver the file payload intact",
    );
    assert_strategy(&captured, "regular-file fallback", 1, true);
}

/// A non-`EINVAL` splice failure propagates instead of falling back.
#[rstest]
fn bad_descriptor_propagates_without_the_fallback() {
    // Reading a pipe's write end is `EBADF`, which splice reports for a
    // descriptor without the mode it needs. Pair it with another pipe's write
    // end as the owned writer, and keep both read ends open so neither pipe is
    // broken before the syscall is attempted.
    let (_source_read, source_write) = unwrap_ok(make_pipe());
    let (sink_read, sink_write) = unwrap_ok(make_pipe());

    let mut outcome = None;
    let captured = capture(Level::DEBUG, || {
        outcome = Some(pump_stream(&source_write, sink_write, buffer_size()));
    });
    let outcome = match outcome {
        Some(outcome) => outcome,
        None => panic!("the pump must run inside the capture closure"),
    };

    match outcome {
        Ok(total) => panic!("splice must reject a write-end reader, moved {total} bytes"),
        Err(PumpError::Io(error)) => assert_eq!(
            error.raw_os_error(),
            Some(libc::EBADF),
            "the dispatcher must propagate the splice failure unchanged",
        ),
        Err(other) => panic!("expected PumpError::Io(EBADF), got {other:?}"),
    }

    assert_strategy(&captured, "bad-descriptor propagation", 0, false);
    let leaked = unwrap_ok(read_all_from(&sink_read));
    assert!(
        leaked.is_empty(),
        "no bytes may reach the sink when splice fails before transferring, got {leaked:?}",
    );
}
