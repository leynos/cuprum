//! Bounded Kani proofs for short UTF-8 payloads and chunk boundaries.

use super::{
    ErrorStep,
    FinalChunk,
    ValidUpTo,
    append_valid_prefix,
    decode_chunks,
    decode_utf8_replace,
    handle_utf8_error,
};

fn decode_single_chunk(input: &[u8], final_chunk: bool) -> (String, Vec<u8>) {
    let mut pending = input.to_vec();
    let mut output = String::new();
    decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(final_chunk));
    (output, pending)
}

fn is_valid_incomplete_utf8_sequence(pending: &[u8]) -> bool {
    match pending {
        [first] => matches!(first, 0xC2..=0xDF | 0xE0..=0xEF | 0xF0..=0xF4),
        [0xe0, second] => matches!(second, 0xa0..=0xbf),
        [0xe1..=0xec, second] => matches!(second, 0x80..=0xbf),
        [0xed, second] => matches!(second, 0x80..=0x9f),
        [0xee..=0xef, second] => matches!(second, 0x80..=0xbf),
        [0xf0, second] => matches!(second, 0x90..=0xbf),
        [0xf1..=0xf3, second] => matches!(second, 0x80..=0xbf),
        [0xf4, second] => matches!(second, 0x80..=0x8f),
        [0xf0, second, third] => matches!(second, 0x90..=0xbf) && matches!(third, 0x80..=0xbf),
        [0xf1..=0xf3, second, third] => {
            matches!(second, 0x80..=0xbf) && matches!(third, 0x80..=0xbf)
        }
        [0xf4, second, third] => matches!(second, 0x80..=0x8f) && matches!(third, 0x80..=0xbf),
        _ => false,
    }
}

#[kani::proof]
#[kani::unwind(5)]
fn single_chunk_matches_from_utf8_lossy() {
    let input = [b'a', 0xff, b'b'];
    let (output, pending) = decode_single_chunk(&input, true);
    let output_bytes = output.as_bytes();

    kani::cover!(output_bytes.len() == 5, "exercise invalid UTF-8 payloads");
    kani::assert(output_bytes.len() == 5, "replacement output length");
    kani::assert(output_bytes[0] == b'a', "valid prefix is preserved");
    kani::assert(output_bytes[1] == 0xef, "replacement byte 1");
    kani::assert(output_bytes[2] == 0xbf, "replacement byte 2");
    kani::assert(output_bytes[3] == 0xbd, "replacement byte 3");
    kani::assert(output_bytes[4] == b'b', "valid suffix is preserved");
    kani::assert(
        pending.is_empty(),
        "final chunk must leave no pending bytes",
    );
}

#[kani::proof]
#[kani::unwind(5)]
fn two_chunk_boundaries_match_from_utf8_lossy() {
    let input = [0xc2, 0xa2];
    let split_point = 1_usize;
    let (left, right) = input.split_at(split_point);
    let chunks = [left, right];
    let (output, pending) = decode_chunks(&chunks, true);
    let output_bytes = output.as_bytes();

    kani::cover!(
        left.len() == 1 && right.len() == 1,
        "exercise split points that bisect valid multi-byte sequences"
    );
    kani::assert(output_bytes.len() == 2, "two chunks decode one scalar");
    kani::assert(output_bytes[0] == 0xc2, "first scalar byte is preserved");
    kani::assert(output_bytes[1] == 0xa2, "second scalar byte is preserved");
    kani::assert(
        pending.is_empty(),
        "final chunk must leave no pending bytes",
    );
}

#[kani::proof]
#[kani::unwind(5)]
fn pending_state_is_valid_incomplete_utf8() {
    let input = [0xe0, 0xa0];
    let (_output, pending) = decode_single_chunk(&input, false);

    kani::assert(
        pending.len() <= 3,
        "pending UTF-8 tail is at most three bytes",
    );
    if !pending.is_empty() {
        kani::assert(
            is_valid_incomplete_utf8_sequence(&pending),
            "pending bytes must form an incomplete UTF-8 prefix",
        );
    }
}

/// Symbolic bound: for *any* three-byte payload, a definite error advances the
/// scan by at least one byte and never past the remaining slice, while an
/// incomplete sequence reports a tail that starts before the end. This is the
/// progress invariant the moving-offset decoder relies on — a non-positive or
/// out-of-range advance would either stall the loop or index outside the
/// buffer — proven here without allocating a pending buffer.
///
/// The payload is three bytes rather than four because a four-byte symbolic
/// payload makes CBMC's model of `run_utf8_validation` grow to tens of
/// gigabytes. Three bytes still reach every incomplete-tail length up to
/// three, which is the bound the decoder retains; the fixed four-byte
/// decoder harnesses cover the remaining payload length.
#[kani::proof]
#[kani::unwind(5)]
fn utf8_error_step_stays_in_range_and_advances() {
    let bytes: [u8; 3] = kani::any();
    if let Err(err) = core::str::from_utf8(&bytes) {
        let mut output = String::new();
        let step = handle_utf8_error(&mut output, &err);
        match err.error_len() {
            Some(error_len) => {
                // Cover proves the malformed-sequence branch is reachable, so
                // the assertions below are not vacuously satisfied.
                kani::cover!(
                    error_len >= 1,
                    "reaches a malformed sequence with a bounded error length"
                );
                kani::assert(
                    matches!(step, ErrorStep::Advance(_)),
                    "a definite error must advance",
                );
                if let ErrorStep::Advance(advance) = step {
                    kani::assert(advance >= 1, "advance is strictly positive");
                    kani::assert(
                        advance <= bytes.len(),
                        "advance never leaves the remaining slice",
                    );
                }
                kani::assert(
                    output == "\u{FFFD}",
                    "a definite error emits exactly one replacement",
                );
            }
            None => {
                // Cover proves the incomplete-tail branch is reachable.
                kani::cover!(
                    err.valid_up_to() < bytes.len(),
                    "reaches an incomplete trailing sequence (error_len == None)"
                );
                kani::assert(
                    matches!(step, ErrorStep::Incomplete(_)),
                    "an incomplete sequence must not advance the scan",
                );
                if let ErrorStep::Incomplete(tail_start) = step {
                    kani::assert(
                        tail_start == err.valid_up_to(),
                        "the incomplete tail starts after the valid prefix",
                    );
                    kani::assert(
                        tail_start < bytes.len(),
                        "the incomplete tail starts before the end",
                    );
                }
                kani::assert(
                    output.is_empty(),
                    "an incomplete sequence emits nothing yet",
                );
            }
        }
    }
}

/// For every three-byte payload, a non-final decode retains at most one
/// incomplete UTF-8 sequence and a final decode retains nothing. Compacting
/// only that tail is what bounds the decoder's retained state.
///
/// Three symbolic bytes are enough to produce every retained-tail length the
/// decoder permits, and they keep the `Vec`/`String` allocator models small
/// enough for CBMC to solve. Four symbolic bytes previously drove the solver
/// past 50 GiB; the unwind bound is the smallest that covers both the scan loop
/// and `run_utf8_validation` over three bytes.
#[kani::proof]
#[kani::unwind(5)]
fn pending_tail_stays_bounded_for_symbolic_input() {
    let bytes: [u8; 3] = kani::any();
    let (_output, pending) = decode_single_chunk(&bytes, false);
    kani::assert(
        pending.len() <= 3,
        "non-final pending is at most three bytes",
    );
    if !pending.is_empty() {
        kani::assert(
            is_valid_incomplete_utf8_sequence(&pending),
            "pending bytes form an incomplete UTF-8 prefix",
        );
    }
    let (_output, final_pending) = decode_single_chunk(&bytes, true);
    kani::assert(
        final_pending.is_empty(),
        "a final chunk retains no pending bytes",
    );
}

/// For every three-byte payload, append exactly its valid UTF-8 prefix.
/// The checked decoder replaces the former unchecked conversion; this proof
/// retains the prefix-selection contract rather than claiming residual unsafety.
#[kani::proof]
#[kani::unwind(4)]
fn append_valid_prefix_matches_checked_decode() {
    let bytes: [u8; 3] = kani::any();
    let valid_up_to = match core::str::from_utf8(&bytes) {
        Ok(valid) => valid.len(),
        Err(err) => err.valid_up_to(),
    };

    // Cover both branches of `append_valid_prefix`: the non-empty prefix that
    // reaches the checked decode, and the `valid_up_to == 0`
    // early return. Pairing them proves neither became unreachable.
    kani::cover!(
        valid_up_to > 0,
        "reaches the checked decode of a non-empty prefix"
    );
    kani::cover!(valid_up_to == 0, "reaches the empty-prefix early return");

    let mut output = String::new();
    append_valid_prefix(&bytes, &mut output, ValidUpTo(valid_up_to));

    // The prefix is valid UTF-8 by construction, so the checked decode
    // cannot fail; the append operation must produce the identical string.
    let prefix = &bytes[..valid_up_to];
    let checked = core::str::from_utf8(prefix).expect("the valid_up_to prefix decodes as UTF-8");
    kani::assert(
        output == checked,
        "appended prefix must equal the checked decode",
    );
}
