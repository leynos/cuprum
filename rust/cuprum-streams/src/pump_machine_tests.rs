//! Property and example tests for the pure pump state machine.
//!
//! Everything here drives [`advance`](super::advance), the production entry
//! point, so the read-length translation and the write precondition under test
//! are the ones the pump loop actually uses rather than a copy living only in
//! test code.

use proptest::prelude::*;
use rstest::rstest;

use super::{Flow, PumpState, WriteEvent, advance, drive};

/// A read length standing in for "a chunk was read".
const CHUNK_LEN: usize = 4;

#[test]
fn advance_skips_the_write_once_the_writer_is_closed() {
    let mut state = PumpState::start();
    // Latch the writer closed with a non-fatal short write.
    drive(&mut state, CHUNK_LEN, WriteEvent::Closed { bytes: 2 });
    assert!(
        !state.writer_open(),
        "the broken pipe must latch the writer"
    );

    let (flow, invoked) = drive(&mut state, CHUNK_LEN, WriteEvent::Complete { bytes: 9 });

    assert_eq!(flow, Flow::Continue, "a drained chunk keeps the loop going");
    assert!(
        !invoked,
        "a closed writer must not be written to again; the chunk only drains the reader",
    );
    assert_eq!(
        state.total_written(),
        2,
        "draining must not accrue further bytes",
    );
}

/// From the starting state the read length alone decides whether the write
/// runs: a non-empty read drives it, a zero-length read stops the loop.
#[rstest]
#[case::chunk_while_open(CHUNK_LEN, Flow::Continue, true, 4)]
#[case::eof(0, Flow::Stop, false, 0)]
fn advance_writes_only_for_a_chunk_while_open(
    #[case] read_len: usize,
    #[case] expected_flow: Flow,
    #[case] expected_invoked: bool,
    #[case] expected_total: u64,
) {
    let mut state = PumpState::start();

    let (flow, invoked) = drive(&mut state, read_len, WriteEvent::Complete { bytes: 4 });

    assert_eq!(
        flow, expected_flow,
        "unexpected flow for read_len {read_len}"
    );
    assert_eq!(
        invoked, expected_invoked,
        "the write must run only for a chunk read while the writer is open",
    );
    assert_eq!(
        state.total_written(),
        expected_total,
        "only a performed write may accrue bytes",
    );
}

/// Only a zero length is EOF; every other length is a chunk, including the
/// one-byte read a small buffer produces.
#[rstest]
#[case::one_byte(1)]
#[case::small(7)]
#[case::whole_buffer(65_536)]
fn advance_treats_every_non_zero_length_as_a_chunk(#[case] read_len: usize) {
    let mut state = PumpState::start();

    let (flow, invoked) = drive(&mut state, read_len, WriteEvent::Complete { bytes: 1 });

    assert_eq!(flow, Flow::Continue, "a non-empty read must keep pumping");
    assert!(
        invoked,
        "a non-empty read while open must perform the write"
    );
}

/// A fatal write aborts the iteration without advancing the state, so the
/// caller's error is what propagates rather than a half-applied transition.
#[test]
fn advance_propagates_a_failed_write_without_advancing() {
    let mut state = PumpState::start();
    let before = state;

    let outcome: Result<Flow, &str> = advance(&mut state, CHUNK_LEN, || Err("fatal write"));

    assert_eq!(
        outcome,
        Err("fatal write"),
        "the write error must propagate"
    );
    assert_eq!(
        state, before,
        "a failed write must leave the state untouched",
    );
}

fn read_len() -> impl Strategy<Value = usize> {
    // Zero is EOF; every other length is a chunk, so both are generated.
    prop_oneof![Just(0_usize), 1_usize..=1 << 16]
}

fn write_event() -> impl Strategy<Value = WriteEvent> {
    prop_oneof![
        (0_u64..=1 << 20).prop_map(|bytes| WriteEvent::Complete { bytes }),
        (0_u64..=1 << 20).prop_map(|bytes| WriteEvent::Closed { bytes }),
    ]
}

proptest! {
    /// Across any script of iterations the running total never decreases, the
    /// writer never reopens once closed, a closed writer accrues no further
    /// bytes, and the loop stops exactly on a zero-length read.
    #[test]
    fn invariants_hold_across_iterations(
        script in prop::collection::vec((read_len(), write_event()), 0..64),
    ) {
        let mut state = PumpState::start();
        for (read_len, write) in script {
            let before = state;
            let (flow, invoked) = drive(&mut state, read_len, write);

            // `advance` must invoke the write exactly when the transition
            // permits one.
            prop_assert_eq!(
                invoked,
                read_len != 0 && before.writer_open(),
                "the write runs only for a chunk read while the writer is open",
            );

            prop_assert!(
                state.total_written() >= before.total_written(),
                "total bytes must be monotonic",
            );
            prop_assert!(
                before.writer_open() || !state.writer_open(),
                "a closed writer must never reopen",
            );
            if !before.writer_open() {
                prop_assert_eq!(
                    state.total_written(),
                    before.total_written(),
                    "a closed writer must accrue no further bytes",
                );
            }
            prop_assert_eq!(
                flow == Flow::Stop,
                read_len == 0,
                "the loop stops exactly on a zero-length read",
            );
            // EOF halts the loop before any further state change.
            if read_len == 0 {
                prop_assert_eq!(state, before, "EOF leaves the state unchanged");
            }
        }
    }

    /// Once a broken-pipe write latches the writer closed, no later write
    /// outcome — however large — can increase the total again, and no further
    /// write is even attempted.
    #[test]
    fn writes_stop_after_broken_pipe(
        accepted in 0_u64..=1 << 20,
        tail in prop::collection::vec(write_event(), 0..32),
    ) {
        let mut state = PumpState::start();
        // First chunk latches the writer closed via a non-fatal short write.
        drive(&mut state, CHUNK_LEN, WriteEvent::Closed { bytes: accepted });
        prop_assert!(!state.writer_open());
        let latched_total = state.total_written();

        for write in tail {
            let (_, invoked) = drive(&mut state, CHUNK_LEN, write);
            prop_assert!(!invoked, "a closed writer is never written to again");
            prop_assert!(!state.writer_open(), "writer stays closed");
            prop_assert_eq!(
                state.total_written(),
                latched_total,
                "no bytes are written after the pipe breaks",
            );
        }
    }
}
