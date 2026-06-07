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

/// Append the valid UTF-8 prefix from pending to output.
fn append_valid_prefix(pending: &[u8], output: &mut String, valid_up_to: ValidUpTo) {
    if valid_up_to.value() == 0 {
        return;
    }
    // SAFETY: `valid_up_to` comes from a `Utf8Error`, so this prefix is known
    // to be valid UTF-8.
    let Some(prefix) = pending.get(..valid_up_to.value()) else {
        return;
    };
    let valid_prefix = unsafe { std::str::from_utf8_unchecked(prefix) };
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
    use super::*;

    #[test]
    fn valid_ascii_passes_through() {
        let mut pending = b"hello world".to_vec();
        let mut output = String::new();
        decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(true));
        assert_eq!(output, "hello world", "valid ASCII must pass through unchanged");
        assert!(pending.is_empty(), "pending must be empty after final chunk");
    }

    #[test]
    fn invalid_bytes_are_replaced_with_replacement_char() {
        let mut pending = b"\xff\xfe".to_vec();
        let mut output = String::new();
        decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(true));
        assert_eq!(
            output,
            "\u{FFFD}\u{FFFD}",
            "each invalid byte must produce one replacement character",
        );
    }

    #[test]
    fn incomplete_sequence_preserved_across_chunks() {
        let mut pending = b"\xf0\x9f".to_vec(); // first two bytes of U+1F600 (😀)
        let mut output = String::new();
        decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(false));
        assert!(output.is_empty(), "incomplete sequence must not appear in output");
        assert_eq!(pending, b"\xf0\x9f", "incomplete sequence must remain in pending");
    }

    #[test]
    fn incomplete_sequence_replaced_at_final_chunk() {
        let mut pending = b"\xf0\x9f".to_vec(); // truncated emoji
        let mut output = String::new();
        decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(true));
        assert_eq!(
            output,
            "\u{FFFD}",
            "truncated sequence in final chunk must produce one replacement character",
        );
        assert!(pending.is_empty(), "pending must be empty after final chunk");
    }

    /// Tests multi-chunk incremental decoding to snapshot the cumulative output
    /// that `consume_stream_files` would produce for a stream containing an
    /// invalid byte followed by valid ASCII.
    #[test]
    fn incremental_replacement_output_matches_snapshot() {
        let mut pending: Vec<u8> = Vec::new();
        let mut output = String::new();

        pending.extend_from_slice(b"hello\xff ");
        decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(false));

        pending.extend_from_slice(b"world");
        decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(true));

        insta::assert_snapshot!(output, @"hello\u{FFFD} world");
    }
}
