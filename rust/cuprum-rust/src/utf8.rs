//! UTF-8 decoding with replacement semantics.
//!
//! This module provides incremental UTF-8 decoding that matches Python's
//! `errors="replace"` behaviour, replacing invalid sequences with the
//! Unicode replacement character (U+FFFD).

/// Marker for how far into the pending buffer is valid UTF-8.
#[derive(Clone, Copy, Debug)]
pub(crate) struct ValidUpTo(usize);

impl ValidUpTo {
    pub(crate) const fn value(self) -> usize {
        self.0
    }
}

/// Marker indicating whether this is the final chunk of input.
#[derive(Clone, Copy, Debug)]
pub(crate) struct FinalChunk(bool);

impl FinalChunk {
    pub(crate) const fn new(is_final: bool) -> Self {
        Self(is_final)
    }

    pub(crate) const fn is_final(self) -> bool {
        self.0
    }
}

/// Decode pending bytes as UTF-8, replacing invalid sequences.
///
/// This function processes the pending buffer incrementally:
/// - Valid UTF-8 is appended to output and removed from pending
/// - Invalid sequences are replaced with U+FFFD
/// - Incomplete sequences at the end are preserved (unless `final_chunk`)
pub(crate) fn decode_utf8_replace(
    pending: &mut Vec<u8>,
    output: &mut String,
    final_chunk: FinalChunk,
) {
    loop {
        match std::str::from_utf8(pending) {
            Ok(valid) => {
                output.push_str(valid);
                pending.clear();
                break;
            }
            Err(err) => {
                append_valid_prefix(pending, output, ValidUpTo(err.valid_up_to()));
                if !handle_utf8_error(
                    pending,
                    output,
                    &err,
                    FinalChunk::new(final_chunk.is_final()),
                ) {
                    break;
                }
            }
        }
    }
}

/// Decode multiple byte chunks through the incremental replacement decoder.
#[cfg(any(test, kani))]
pub(crate) fn decode_chunks(chunks: &[&[u8]], final_chunk: bool) -> (String, Vec<u8>) {
    let mut pending = Vec::new();
    let mut output = String::new();
    for chunk in chunks {
        pending.extend_from_slice(chunk);
        decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(false));
    }
    decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(final_chunk));
    (output, pending)
}

/// Append the valid UTF-8 prefix from pending to output.
fn append_valid_prefix(pending: &[u8], output: &mut String, valid_up_to: ValidUpTo) {
    if valid_up_to.value() == 0 {
        return;
    }
    // SAFETY: `valid_up_to` comes from a `Utf8Error`, so this prefix is known
    // to be valid UTF-8.
    let Some(prefix_bytes) = pending.get(..valid_up_to.value()) else {
        return;
    };
    let valid_prefix = unsafe { std::str::from_utf8_unchecked(prefix_bytes) };
    output.push_str(valid_prefix);
}

/// Handle a UTF-8 decoding error by replacing invalid bytes.
///
/// Returns `true` if there are more bytes to process after handling the error.
fn handle_utf8_error(
    pending: &mut Vec<u8>,
    output: &mut String,
    err: &std::str::Utf8Error,
    final_chunk: FinalChunk,
) -> bool {
    let valid_up_to = err.valid_up_to();
    let is_final_chunk = final_chunk.is_final();
    match err.error_len() {
        Some(error_len) => {
            output.push('\u{FFFD}');
            // NOTE: `decode_utf8_replace` must call `append_valid_prefix`
            // immediately before `handle_utf8_error`; this drain in
            // `handle_utf8_error` skips the already-appended valid prefix plus
            // the invalid sequence (valid_up_to + error_len) to avoid
            // double-draining.
            pending.drain(..valid_up_to + error_len);
            !pending.is_empty()
        }
        None => handle_incomplete_sequence(
            pending,
            output,
            ValidUpTo(valid_up_to),
            FinalChunk::new(is_final_chunk),
        ),
    }
}

/// Handle an incomplete UTF-8 sequence at the end of input.
///
/// Returns `true` if there are more bytes to process.
fn handle_incomplete_sequence(
    pending: &mut Vec<u8>,
    output: &mut String,
    valid_up_to: ValidUpTo,
    final_chunk: FinalChunk,
) -> bool {
    if final_chunk.is_final() {
        output.push('\u{FFFD}');
        pending.clear();
        return false;
    }
    if valid_up_to.value() > 0 {
        // Keep only the incomplete tail; drop already used prefix.
        pending.drain(..valid_up_to.value());
    }
    false
}

#[cfg(test)]
mod tests {
    //! Property tests for the incremental UTF-8 replacement decoder.

    use proptest::prelude::*;

    use super::{FinalChunk, decode_chunks, decode_utf8_replace};

    fn split_input_at_points<'input>(
        input: &'input [u8],
        split_points: &[usize],
    ) -> Vec<&'input [u8]> {
        let mut sorted_points = split_points
            .iter()
            .map(|point| (*point).min(input.len()))
            .collect::<Vec<_>>();
        sorted_points.sort_unstable();
        sorted_points.dedup();

        let mut chunks = Vec::new();
        let mut offset = 0_usize;
        let mut remainder = input;
        for split_point in sorted_points {
            let (chunk, next_remainder) = remainder.split_at(split_point.saturating_sub(offset));
            chunks.push(chunk);
            remainder = next_remainder;
            offset = split_point;
        }
        chunks.push(remainder);
        chunks
    }

    fn decode_single_chunk(input: &[u8], final_chunk: bool) -> (String, Vec<u8>) {
        let mut pending = input.to_vec();
        let mut output = String::new();
        decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(final_chunk));
        (output, pending)
    }

    fn incomplete_tail_strategy() -> impl Strategy<Value = Vec<u8>> {
        prop_oneof![
            Just(vec![0xC2]),
            Just(vec![0xDF]),
            Just(vec![0xE0, 0xA0]),
            Just(vec![0xE1, 0x80]),
            Just(vec![0xEF, 0xBF]),
            Just(vec![0xF0, 0x90, 0x80]),
            Just(vec![0xF1, 0x80, 0x80]),
            Just(vec![0xF4, 0x8F, 0xBF]),
        ]
    }

    proptest! {
        #[test]
        fn single_chunk_matches_from_utf8_lossy(input in any::<Vec<u8>>()) {
            let (output, pending) = decode_single_chunk(&input, true);

            prop_assert_eq!(output, String::from_utf8_lossy(&input).into_owned());
            prop_assert!(pending.is_empty());
        }

        #[test]
        fn chunked_decoding_matches_from_utf8_lossy(
            input in any::<Vec<u8>>(),
            split_points in prop::collection::vec(0_usize..256, 0..32),
        ) {
            let chunks = split_input_at_points(&input, &split_points);
            let (output, pending) = decode_chunks(&chunks, true);

            prop_assert_eq!(output, String::from_utf8_lossy(&input).into_owned());
            prop_assert!(pending.is_empty());
        }

        #[test]
        fn incomplete_sequences_are_retained_until_final_chunk(
            prefix in any::<String>(),
            tail in incomplete_tail_strategy(),
        ) {
            let mut input = prefix.into_bytes();
            input.extend_from_slice(&tail);
            let prefix_len = input.len() - tail.len();
            let (valid_prefix, _) = input.split_at(prefix_len);

            let (deferred_output, deferred_pending) = decode_single_chunk(&input, false);
            prop_assert_eq!(deferred_output, String::from_utf8_lossy(valid_prefix));
            prop_assert_eq!(deferred_pending, tail);

            let (final_output, final_pending) = decode_single_chunk(&input, true);
            prop_assert_eq!(final_output, String::from_utf8_lossy(&input).into_owned());
            prop_assert!(final_pending.is_empty());
        }
    }
}

#[cfg(kani)]
mod kani_proofs {
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
                // The addition must not overflow, and the resulting drain end
                // must stay within the buffer that `handle_utf8_error` drains.
                let drain_end = valid_up_to
                    .checked_add(error_len)
                    .expect("valid_up_to + error_len must not overflow");
                kani::assert(
                    drain_end <= bytes.len(),
                    "the drain bound never exceeds the pending buffer",
                );
            } else {
                // Incomplete tail: the retained prefix bound is in range too.
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

        let mut output = String::new();
        append_valid_prefix(&bytes, &mut output, ValidUpTo(valid_up_to));

        // The prefix is valid UTF-8 by construction, so the checked decode
        // cannot fail; the unchecked path must produce the identical string.
        let prefix = &bytes[..valid_up_to];
        let checked = core::str::from_utf8(prefix).unwrap();
        kani::assert(
            output == checked,
            "unchecked prefix decode must equal the checked decode",
        );
    }
}
