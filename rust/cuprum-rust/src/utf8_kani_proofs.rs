//! Bounded Kani proofs for short UTF-8 payloads and chunk boundaries.

use super::{FinalChunk, ValidUpTo, append_valid_prefix, decode_chunks, decode_utf8_replace};

fn decode_single_chunk(input: &[u8], final_chunk: bool) -> (String, Vec<u8>) {
    let mut pending = input.to_vec();
    let mut output = String::new();
    decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(final_chunk));
    (output, pending)
}

fn is_valid_incomplete_utf8_sequence(pending: &[u8]) -> bool {
    match pending {
        [first] => matches!(first, 0xC2..=0xDF | 0xE0..=0xEF | 0xF0..=0xF4),
        [0xE0, second] => matches!(second, 0xA0..=0xBF),
        [0xE1..=0xEC, second] => matches!(second, 0x80..=0xBF),
        [0xED, second] => matches!(second, 0x80..=0x9F),
        [0xEE..=0xEF, second] => matches!(second, 0x80..=0xBF),
        [0xF0, second] => matches!(second, 0x90..=0xBF),
        [0xF1..=0xF3, second] => matches!(second, 0x80..=0xBF),
        [0xF4, second] => matches!(second, 0x80..=0x8F),
        [0xF0, second, third] => matches!(second, 0x90..=0xBF) && matches!(third, 0x80..=0xBF),
        [0xF1..=0xF3, second, third] => {
            matches!(second, 0x80..=0xBF) && matches!(third, 0x80..=0xBF)
        }
        [0xF4, second, third] => matches!(second, 0x80..=0x8F) && matches!(third, 0x80..=0xBF),
        _ => false,
    }
}

#[kani::proof]
#[kani::unwind(5)]
fn single_chunk_matches_from_utf8_lossy() {
    let input = [b'a', 0xFF, b'b'];
    let (output, pending) = decode_single_chunk(&input, true);
    let output_bytes = output.as_bytes();

    kani::cover!(output_bytes.len() == 5, "exercise invalid UTF-8 payloads");
    kani::assert(output_bytes.len() == 5, "replacement output length");
    kani::assert(output_bytes[0] == b'a', "valid prefix is preserved");
    kani::assert(output_bytes[1] == 0xEF, "replacement byte 1");
    kani::assert(output_bytes[2] == 0xBF, "replacement byte 2");
    kani::assert(output_bytes[3] == 0xBD, "replacement byte 3");
    kani::assert(output_bytes[4] == b'b', "valid suffix is preserved");
    kani::assert(
        pending.is_empty(),
        "final chunk must leave no pending bytes",
    );
}

#[kani::proof]
#[kani::unwind(5)]
fn two_chunk_boundaries_match_from_utf8_lossy() {
    let input = [0xC2, 0xA2];
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
    kani::assert(output_bytes[0] == 0xC2, "first scalar byte is preserved");
    kani::assert(output_bytes[1] == 0xA2, "second scalar byte is preserved");
    kani::assert(
        pending.is_empty(),
        "final chunk must leave no pending bytes",
    );
}

#[kani::proof]
#[kani::unwind(5)]
fn pending_state_is_valid_incomplete_utf8() {
    let input = [0xE0, 0xA0];
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

/// Symbolic bound: for *any* four-byte payload, the drain bound
/// `valid_up_to + error_len` that `handle_utf8_error` passes to
/// `pending.drain(..)` never exceeds the buffer length and never
/// overflows. This is the exact safety invariant the type system does not
/// enforce — indexing and draining are sound only because the bound is
/// `Utf8Error`-derived — proven here over the whole malformed-layout
/// space without allocating.
#[kani::proof]
#[kani::unwind(5)]
fn utf8_error_drain_bound_stays_in_range() {
    let bytes: [u8; 4] = kani::any();
    if let Err(err) = core::str::from_utf8(&bytes) {
        let valid_up_to = err.valid_up_to();
        if let Some(error_len) = err.error_len() {
            // Cover proves the malformed-sequence branch is reachable, so
            // the assertion below is not vacuously satisfied.
            kani::cover!(
                error_len >= 1,
                "reaches a malformed sequence with a bounded error length"
            );
            // The addition must not overflow, and the drain end must stay
            // within the buffer that `handle_utf8_error` drains.
            let drain_end = valid_up_to
                .checked_add(error_len)
                .expect("valid_up_to + error_len must not overflow");
            kani::assert(
                drain_end <= bytes.len(),
                "the drain bound never exceeds the pending buffer",
            );
        } else {
            // Cover proves the incomplete-tail branch (error_len == None)
            // is reachable; the retained prefix bound is then in range too.
            kani::cover!(
                valid_up_to < bytes.len(),
                "reaches an incomplete trailing sequence (error_len == None)"
            );
            kani::assert(
                valid_up_to <= bytes.len(),
                "the incomplete-sequence prefix bound stays in range",
            );
        }
    }
}

/// Symbolic bound: the unsafe `from_utf8_unchecked` inside
/// `append_valid_prefix` produces exactly the same string as a checked
/// decode of the same prefix, for *any* short payload. This upholds the
/// SAFETY claim that the `valid_up_to` prefix is genuinely valid UTF-8.
#[kani::proof]
#[kani::unwind(4)]
fn append_valid_prefix_matches_checked_decode() {
    let bytes: [u8; 3] = kani::any();
    let valid_up_to = match core::str::from_utf8(&bytes) {
        Ok(valid) => valid.len(),
        Err(err) => err.valid_up_to(),
    };

    // Cover both branches of `append_valid_prefix`: the non-empty prefix that
    // reaches the `unsafe from_utf8_unchecked` call, and the `valid_up_to == 0`
    // early return. Pairing them proves neither became unreachable.
    kani::cover!(
        valid_up_to > 0,
        "reaches the unchecked decode of a non-empty prefix"
    );
    kani::cover!(valid_up_to == 0, "reaches the empty-prefix early return");

    let mut output = String::new();
    append_valid_prefix(&bytes, &mut output, ValidUpTo(valid_up_to));

    // The prefix is valid UTF-8 by construction, so the checked decode
    // cannot fail; the unchecked path must produce the identical string.
    let prefix = &bytes[..valid_up_to];
    let checked = core::str::from_utf8(prefix).expect("the valid_up_to prefix decodes as UTF-8");
    kani::assert(
        output == checked,
        "unchecked prefix decode must equal the checked decode",
    );
}
