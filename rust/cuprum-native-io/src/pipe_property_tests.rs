//! Property tests for the standard-library-backed pipe seam.
//!
//! Two rules are stated here over generated inputs rather than over a handful
//! of chosen examples: a payload of any bounded size survives the transfer
//! unchanged, and a reader observes EOF only after the last writer owner
//! closes. Both run against real OS endpoints, so the module is compiled out
//! under Miri for the same reason as `pipe_tests`, and it takes that module's
//! shared descriptor lock so the two suites cannot interleave their descriptor
//! tables within one test binary.
//!
//! Neither direction runs on the test thread. A generated payload can exceed
//! the pipe buffer, which blocks `write` until the reader drains it; bounding
//! only the read would let a stalled transfer wait out the job's own timeout
//! instead of failing here.

use std::{sync::mpsc::RecvTimeoutError, time::Duration};

use proptest::prelude::*;

use super::{
    OwnedStream,
    ownership_tests::descriptor_guard,
    pipe,
    pipe_tests::{await_payload, await_written, read_to_eof_on_worker, write_to_pipe_on_worker},
};

/// A size whose transfer cannot complete in one pipe buffer.
///
/// 64 KiB is the default Linux pipe capacity, so a payload of this size forces
/// the writer to block part-way through and resume once the reader drains. The
/// explicit cases either side of it separate a stalled transfer from an
/// off-by-one in the loop bounds.
const CAPACITY_EDGE: usize = 64 * 1024;

/// How long a still-open writer is observed before EOF is accepted as absent.
///
/// The window must be long enough for a reader woken by a premature close to
/// report through the channel, and short enough that the many cases in the
/// sequence property stay inside the suite's per-test timeout.
const EOF_PROBE: Duration = Duration::from_millis(10);

/// Bound the case count and keep generated failures out of the source tree.
///
/// 128 cases keep the blocking EOF probes inside the per-test budget. Failure
/// persistence is off because a shrinking seed for a payload would be written
/// as a multi-kilobyte file inside the crate, and the interesting inputs here
/// are bounded sizes the strategies already name explicitly.
fn pipe_proptest_config() -> ProptestConfig {
    ProptestConfig {
        cases: 128,
        failure_persistence: None,
        ..ProptestConfig::default()
    }
}

/// Payload sizes that between them cover the transfer's interesting edges.
///
/// The uniform arm supplies ordinary binary data; the fixed arms pin the empty
/// payload, a single byte, and the three sizes around one buffer's worth.
fn payload_size() -> impl Strategy<Value = usize> {
    prop_oneof![
        Just(0_usize),
        Just(1_usize),
        Just(CAPACITY_EDGE - 1),
        Just(CAPACITY_EDGE),
        Just(CAPACITY_EDGE + 1),
        0_usize..=CAPACITY_EDGE,
    ]
}

proptest! {
    #![proptest_config(pipe_proptest_config())]

    /// Every bounded payload arrives byte-for-byte and is followed by EOF.
    #[test]
    fn generated_payloads_round_trip_and_reach_eof(
        payload in payload_size().prop_flat_map(|size| {
            prop::collection::vec(any::<u8>(), size..=size)
        }),
        chunk_len in 1_usize..=64,
    ) {
        let guard = descriptor_guard()
            .unwrap_or_else(|error| panic!("descriptor test lock: {error:?}"));
        let (reader, writer) = pipe().unwrap_or_else(|error| panic!("create pipe: {error:?}"));
        let receiver = read_to_eof_on_worker(reader, chunk_len);
        let written = write_to_pipe_on_worker(writer, payload.clone());

        // Both directions are awaited under the same deadline. A payload
        // larger than the pipe buffer blocks the write until the worker
        // drains it, so a reader that stopped early surfaces here as a
        // bounded write failure instead of a hung case. `await_written`
        // returns only when every byte was accepted.
        await_written(&written);

        let received = await_payload(&receiver);
        prop_assert_eq!(
            received.len(),
            payload.len(),
            "the reader must see exactly the bytes that were written",
        );
        prop_assert!(
            received == payload,
            "the transferred payload differed from the source at the same length",
        );
        drop(guard);
    }

    /// EOF waits for the last writer owner whatever order the owners close in.
    #[test]
    fn eof_follows_the_final_writer_owner(
        ordering in prop::collection::vec(any::<u32>(), 1..=4),
    ) {
        let guard = descriptor_guard()
            .unwrap_or_else(|error| panic!("descriptor test lock: {error:?}"));
        let (reader, writer) = pipe().unwrap_or_else(|error| panic!("create pipe: {error:?}"));
        let receiver = read_to_eof_on_worker(reader, 7);

        // One owner per key: the original writer plus a clone for every key
        // beyond the first. Cloning here rather than in the drop loop keeps
        // the sequence's length independent of the order it is dropped in.
        let mut owners: Vec<Option<OwnedStream>> = Vec::with_capacity(ordering.len());
        for _ in 1..ordering.len() {
            let duplicate = writer
                .try_clone()
                .unwrap_or_else(|error| panic!("clone writer: {error:?}"));
            owners.push(Some(duplicate));
        }
        owners.push(Some(writer));

        // The keys are distinct integers only by accident; sorting the indices
        // by them turns the generated values into an arbitrary permutation,
        // which is what makes each case a different close order.
        let mut close_order: Vec<usize> = (0..ordering.len()).collect();
        close_order.sort_by_key(|index| ordering.get(*index).copied().unwrap_or_default());

        let total = owners.len();
        for (position, index) in close_order.iter().enumerate() {
            let owner = owners.get_mut(*index).and_then(Option::take);
            prop_assert!(owner.is_some(), "each owner must be closed exactly once");
            drop(owner);

            if position + 1 < total {
                // A live owner keeps the read blocked: neither a payload nor
                // the worker's completion may arrive yet.
                let still_blocked = matches!(
                    receiver.recv_timeout(EOF_PROBE),
                    Err(RecvTimeoutError::Timeout),
                );
                prop_assert!(
                    still_blocked,
                    "EOF or a reader failure arrived with {} owner(s) still open",
                    total - position - 1,
                );
            }
        }

        // Nothing was written, so the reader's only observation is EOF itself.
        let received = await_payload(&receiver);
        prop_assert!(
            received.is_empty(),
            "the reader must observe EOF, not {} stray bytes",
            received.len(),
        );
        drop(guard);
    }
}
