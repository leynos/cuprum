//! Regression coverage for the decoder's linear-time contract.
//!
//! `rust_consume_stream` exposes the decoder to lower-trust bytes, so a
//! malformed chunk is adversarial input rather than a programming error. The
//! decoder must therefore do a bounded amount of work per byte: it scans with
//! a moving offset and compacts only the at-most-three-byte incomplete tail.
//! An implementation that re-validated and drained the whole remainder per
//! replacement would grow quadratically, turning one buffered read of invalid
//! UTF-8 into a CPU-exhaustion vector.
//!
//! Timing is never exact, so these tests compare the *minimum* of several
//! repeats and allow a generous ratio bound. Near-linear decoding of eight
//! times the input stays far below that bound, while the quadratic regression
//! this guards against would exceed it by a wide margin. These tests compile
//! out under Miri and Kani, where wall-clock measurement is meaningless.

use std::time::{Duration, Instant};

use rstest::rstest;

use crate::{
    BufferSize,
    consume_tests::{ScriptedReader, Step},
    consume_with_reader,
    utf8::{FinalChunk, decode_utf8_replace},
};

/// Small input size, in bytes.
const SMALL_INPUT: usize = 32 * 1024;

/// Large input size: eight times [`SMALL_INPUT`].
const LARGE_INPUT: usize = SMALL_INPUT * 8;

/// Repeats per measurement; the minimum is the least noisy estimate.
const REPEATS: usize = 5;

/// Upper bound on the large/small time ratio. Linear growth is `8`; the
/// quadratic regression this test guards against is about `64`.
const MAX_RATIO: u32 = 24;

/// Floor for the small measurement so a fast machine cannot turn the ratio
/// into a division by a near-zero duration.
const FLOOR: Duration = Duration::from_micros(250);

/// Malformed payload shapes that stress distinct decoder paths.
#[derive(Clone, Copy, Debug)]
enum Workload {
    /// Every byte is invalid on its own.
    AllInvalid,
    /// A valid ASCII byte between invalid bytes.
    AlternatingAscii,
    /// Repeated truncated three-byte leads: each `E2 82` pair ends at a new
    /// lead byte, so every second byte forces another replacement.
    TruncatedThreeByte,
    /// Lone continuation bytes, invalid without a preceding lead.
    LoneContinuation,
}

impl Workload {
    /// Build `len` bytes of this workload.
    fn build(self, len: usize) -> Vec<u8> {
        match self {
            Self::AllInvalid => vec![0xff; len],
            Self::AlternatingAscii => (0..len)
                .map(|index| if index & 1 == 0 { b'a' } else { 0xff })
                .collect(),
            Self::TruncatedThreeByte => (0..len)
                .map(|index| if index & 1 == 0 { 0xe2 } else { 0x82 })
                .collect(),
            Self::LoneContinuation => vec![0x80; len],
        }
    }
}

/// Decode `input` once as a final chunk and return the measured duration.
///
/// The lossy-decoding and pending assertions run after the clock stops so
/// they cannot flatter or penalize the measurement.
fn decode_once(input: &[u8]) -> Duration {
    let mut pending = input.to_vec();
    let mut output = String::new();
    let start = Instant::now();
    decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(true));
    let elapsed = start.elapsed();

    assert_eq!(
        output,
        String::from_utf8_lossy(input).into_owned(),
        "decoded output must match lossy decoding"
    );
    assert!(
        pending.is_empty(),
        "final chunk must retain no pending bytes"
    );
    elapsed
}

/// Minimum of [`REPEATS`] decode timings for `input`.
fn minimum_elapsed(input: &[u8]) -> Duration {
    (1..REPEATS).fold(decode_once(input), |minimum, _| {
        minimum.min(decode_once(input))
    })
}

#[rstest]
#[case::all_invalid(Workload::AllInvalid)]
#[case::alternating_ascii(Workload::AlternatingAscii)]
#[case::truncated_three_byte(Workload::TruncatedThreeByte)]
#[case::lone_continuation(Workload::LoneContinuation)]
fn malformed_input_decodes_in_near_linear_time(#[case] workload: Workload) {
    let small = workload.build(SMALL_INPUT);
    let large = workload.build(LARGE_INPUT);

    let small_elapsed = minimum_elapsed(&small).max(FLOOR);
    let large_elapsed = minimum_elapsed(&large);
    let bound = small_elapsed.saturating_mul(MAX_RATIO);

    assert!(
        large_elapsed <= bound,
        "{workload:?}: decoding {LARGE_INPUT} bytes took {large_elapsed:?} against a \
         {SMALL_INPUT}-byte baseline of {small_elapsed:?} (bound {MAX_RATIO}x); the decoder may \
         have regressed to quadratic work"
    );
}

/// One buffered read of a large all-invalid payload decodes to completion.
///
/// This drives the same `consume_with_reader` loop the exported entry point
/// uses, so the threat model is exercised end to end: a single read supplies
/// the whole malformed chunk, and the decoder must finish without per-byte
/// rescans of the buffer.
#[test]
fn consume_with_reader_decodes_one_large_malformed_read() {
    let payload = Workload::AllInvalid.build(LARGE_INPUT);
    let expected = String::from_utf8_lossy(&payload).into_owned();
    let mut reader = ScriptedReader::new([Step::Bytes(payload.clone())]);

    let output = consume_with_reader(
        |buffer| reader.read(buffer),
        BufferSize(payload.len()),
        "scaling-test",
    )
    .expect("one buffered read of invalid UTF-8 must decode");

    assert_eq!(output, expected);
}
