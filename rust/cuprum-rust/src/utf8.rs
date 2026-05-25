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
