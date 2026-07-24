//! Behavioural tests for the unified splice loop and the EINTR-correct
//! drain: full transfer between pipes, fallback signalling for
//! unsupported descriptor types, and broken-pipe draining.

use std::fs::File;
use std::io;
use std::os::fd::{AsRawFd, OwnedFd};

use proptest::prelude::*;
use rstest::{fixture, rstest};
use tempfile::NamedTempFile;

use super::{accumulate_splices, drain_reader, splice_once_with, try_splice_pump};
use crate::errors::PumpError;
use crate::test_support::{make_pipe, read_all_from, unwrap_ok, write_all_to};

/// A connected `pipe(2)` pair: `(read_end, write_end)`.
type PipePair = (OwnedFd, OwnedFd);

/// A `pipe(2)` pair as an [`rstest`] fixture wrapping
/// [`crate::test_support::make_pipe`].
///
/// Exposing the shared pipe setup as a fixture keeps its construction in
/// one place; tests needing several independent pipes request it once per
/// `#[from(pipe)]` parameter.
#[fixture]
fn pipe() -> PipePair {
    make_pipe()
}

#[rstest]
fn splice_transfers_all_bytes_between_pipes(
    #[from(pipe)] source: PipePair,
    #[from(pipe)] sink: PipePair,
) {
    let (source_read, source_write) = source;
    let (sink_read, sink_write) = sink;
    let payload = b"unified splice loop payload";

    write_all_to(&source_write, payload);
    drop(source_write); // EOF for the splice loop.

    let outcome = try_splice_pump(&source_read, &sink_write, 4096);
    drop(sink_write); // EOF for the verification read.

    let expected_len = unwrap_ok(u64::try_from(payload.len()));
    match outcome {
        Some(Ok(transferred)) => {
            assert_eq!(transferred, expected_len);
        }
        other => panic!("expected Some(Ok(_)) from pipe splice, got {other:?}"),
    }
    assert_eq!(read_all_from(&sink_read), payload);
}

#[rstest]
fn unsupported_descriptors_signal_fallback() {
    // Unique temp files avoid name collisions between concurrent test
    // runs; each `NamedTempFile` removes itself on drop, so a mid-test
    // panic leaves nothing behind.
    let reader_file = unwrap_ok(NamedTempFile::new());
    let writer_file = unwrap_ok(NamedTempFile::new());
    unwrap_ok(std::fs::write(reader_file.path(), b"file payload"));
    let reader = OwnedFd::from(unwrap_ok(File::open(reader_file.path())));
    let writer = OwnedFd::from(unwrap_ok(File::create(writer_file.path())));

    // Two regular files cannot splice; the first call must signal the
    // read/write fallback rather than erroring.
    let outcome = try_splice_pump(&reader, &writer, 4096);
    assert!(
        outcome.is_none(),
        "expected fallback signal, got {outcome:?}"
    );
}

#[rstest]
fn broken_pipe_drains_reader_and_reports_transferred_bytes(
    #[from(pipe)] source: PipePair,
    #[from(pipe)] sink: PipePair,
) {
    let (source_read, source_write) = source;
    let (sink_read, sink_write) = sink;
    let payload = b"bytes that can no longer be delivered";

    write_all_to(&source_write, payload);
    drop(source_write);
    drop(sink_read); // Break the downstream pipe before pumping.

    let outcome = try_splice_pump(&source_read, &sink_write, 4096);
    match outcome {
        Some(Ok(0)) => {}
        other => panic!("expected Some(Ok(0)) after broken pipe, got {other:?}"),
    }

    // The drain must have consumed the source to EOF so upstream writers
    // cannot block on a full pipe buffer.
    let leftover = read_all_from(&source_read);
    assert!(
        leftover.is_empty(),
        "reader must be drained, got {leftover:?}"
    );
}

#[rstest]
fn drain_reader_consumes_to_eof(pipe: PipePair) {
    let (read_end, write_end) = pipe;
    write_all_to(&write_end, b"residual data");
    drop(write_end);

    unwrap_ok(drain_reader(read_end.as_raw_fd(), 8));

    let leftover = read_all_from(&read_end);
    assert!(leftover.is_empty(), "drain must consume the pipe to EOF");
}

#[rstest]
fn splice_once_retries_after_interruption() {
    // A signal delivered mid-transfer surfaces as `EINTR`; the syscall
    // wrapper must re-issue the splice rather than fail. Inject one
    // interruption, then a successful transfer, and confirm the retry.
    let mut attempts = 0_u8;

    let transferred = unwrap_ok(splice_once_with(|| {
        attempts = attempts.saturating_add(1);
        if attempts == 1 {
            return Err(io::Error::from(io::ErrorKind::Interrupted));
        }
        Ok(7)
    }));

    assert_eq!(transferred, 7);
    assert_eq!(attempts, 2);
}

proptest! {
    /// Across any number of leading `EINTR`s and either terminal outcome,
    /// `splice_once_with` issues exactly one syscall per attempt and
    /// returns the first non-interrupted result. This exercises the retry
    /// state space the deterministic case above only samples at one point.
    #[test]
    fn splice_once_with_retries_through_interruptions(
        interruptions in 0_u32..64,
        terminal_ok in any::<bool>(),
    ) {
        let mut remaining = interruptions;
        let mut calls = 0_u32;

        let result = splice_once_with(|| {
            calls = calls.saturating_add(1);
            if remaining > 0 {
                remaining = remaining.saturating_sub(1);
                return Err(io::Error::from(io::ErrorKind::Interrupted));
            }
            if terminal_ok {
                Ok(7)
            } else {
                Err(io::Error::from(io::ErrorKind::BrokenPipe))
            }
        });

        prop_assert_eq!(calls, interruptions.saturating_add(1));
        match result {
            Ok(transferred) => {
                prop_assert!(terminal_ok);
                prop_assert_eq!(transferred, 7);
            }
            Err(err) => {
                prop_assert!(!terminal_ok);
                prop_assert!(matches!(err, PumpError::Io(_)));
            }
        }
    }
}

/// How a generated splice sequence ends, for the accumulation property.
#[derive(Debug, Clone, Copy)]
enum Terminal {
    /// `Ok(0)`: end of input.
    Eof,
    /// Non-fatal write error: the reader must be drained, bytes so far
    /// reported.
    BrokenPipe,
    /// Fatal error: propagate, no drain.
    Fatal,
}

proptest! {
    /// The unified loop sums every `Ok(n)` before the terminal outcome,
    /// drains exactly on a broken pipe, and propagates a fatal error
    /// without draining. Covers the `Ok(0)` / `Ok(n)` / non-fatal / fatal
    /// transitions the production loop feeds from `splice_once`.
    #[test]
    fn accumulate_splices_sums_until_terminal(
        chunks in prop::collection::vec(1_u16..4096, 0..32),
        terminal in prop_oneof![
            Just(Terminal::Eof),
            Just(Terminal::BrokenPipe),
            Just(Terminal::Fatal),
        ],
    ) {
        let mut outcomes: Vec<Result<usize, PumpError>> =
            chunks.iter().map(|&n| Ok(usize::from(n))).collect();
        outcomes.push(match terminal {
            Terminal::Eof => Ok(0),
            Terminal::BrokenPipe => {
                Err(PumpError::from(io::Error::from(io::ErrorKind::BrokenPipe)))
            }
            Terminal::Fatal => {
                Err(PumpError::from(io::Error::from(io::ErrorKind::PermissionDenied)))
            }
        });

        let mut remaining = outcomes.into_iter();
        let first = remaining.next().unwrap_or(Ok(0));
        let mut drained = 0_u32;

        let result = accumulate_splices(
            first,
            || remaining.next().unwrap_or(Ok(0)),
            || {
                drained = drained.saturating_add(1);
                Ok(())
            },
        );

        let expected_total: u64 = chunks.iter().map(|&n| u64::from(n)).sum();
        match terminal {
            Terminal::Eof => {
                prop_assert_eq!(drained, 0);
                match result {
                    Ok(total) => prop_assert_eq!(total, expected_total),
                    Err(err) => prop_assert!(false, "unexpected error: {err:?}"),
                }
            }
            Terminal::BrokenPipe => {
                prop_assert_eq!(drained, 1);
                match result {
                    Ok(total) => prop_assert_eq!(total, expected_total),
                    Err(err) => prop_assert!(false, "unexpected error: {err:?}"),
                }
            }
            Terminal::Fatal => {
                prop_assert_eq!(drained, 0);
                prop_assert!(result.is_err());
            }
        }
    }
}
