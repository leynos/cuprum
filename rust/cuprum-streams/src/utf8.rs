//! UTF-8 decoding with replacement semantics.
//!
//! This module provides incremental UTF-8 decoding that matches Python's
//! `errors="replace"` behaviour, replacing invalid sequences with the
//! Unicode replacement character (U+FFFD).
//!
//! Decoding is linear in the size of each buffered chunk. The decoder scans
//! forward with a moving offset and never re-validates bytes it has already
//! consumed, so a chunk of lower-trust invalid bytes cannot drive quadratic
//! work. Only the at-most-three-byte incomplete tail is retained between
//! calls.

/// Marker for how far into the pending buffer is valid UTF-8.
#[derive(Clone, Copy, Debug)]
pub(crate) struct ValidUpTo(usize);

impl ValidUpTo {
    pub(crate) const fn value(self) -> usize { self.0 }
}

/// Marker indicating whether this is the final chunk of input.
#[derive(Clone, Copy, Debug)]
pub(crate) struct FinalChunk(bool);

impl FinalChunk {
    pub(crate) const fn new(is_final: bool) -> Self { Self(is_final) }

    pub(crate) const fn is_final(self) -> bool { self.0 }
}

/// Decode pending bytes as UTF-8, replacing invalid sequences.
///
/// This function processes the pending buffer incrementally:
/// - Valid UTF-8 is appended to output and removed from pending
/// - Invalid sequences are replaced with U+FFFD
/// - Incomplete sequences at the end are preserved (unless `final_chunk`)
///
/// The scan keeps a byte offset into `pending` instead of draining the
/// consumed prefix on every replacement. Each iteration therefore validates
/// only bytes it has not seen, and the single compaction at the end moves at
/// most one incomplete UTF-8 sequence. Both properties keep the work linear in
/// `pending.len()`, which matters because the input may come from a
/// lower-trust stream.
pub(crate) fn decode_utf8_replace(
    pending: &mut Vec<u8>,
    output: &mut String,
    final_chunk: FinalChunk,
) {
    let mut offset = 0_usize;
    loop {
        match scan_once(pending, output, offset) {
            ScanOutcome::Consumed => break,
            ScanOutcome::Continue(next) => offset = next,
            ScanOutcome::Incomplete(tail_start) => {
                handle_incomplete_sequence(pending, output, tail_start, final_chunk);
                return;
            }
        }
    }
    pending.clear();
}

/// Result of one forward scan over the unconsumed part of the buffer.
#[derive(Clone, Copy, Debug)]
enum ScanOutcome {
    /// Every remaining byte was consumed.
    Consumed,
    /// Resume scanning at this absolute offset.
    Continue(usize),
    /// The buffer ends inside an incomplete sequence starting at this offset.
    Incomplete(usize),
}

/// Scan from `offset` until a definite error, an incomplete tail, or the end.
fn scan_once(pending: &[u8], output: &mut String, offset: usize) -> ScanOutcome {
    // Checked slicing keeps the offset total even if a future change let it
    // outrun the buffer.
    let Some(remaining) = pending.get(offset..) else {
        return ScanOutcome::Consumed;
    };
    match std::str::from_utf8(remaining) {
        Ok(valid) => {
            output.push_str(valid);
            ScanOutcome::Consumed
        }
        Err(err) => {
            append_valid_prefix(remaining, output, ValidUpTo(err.valid_up_to()));
            match handle_utf8_error(output, &err) {
                ErrorStep::Advance(advance) => resume_at(pending.len(), offset, advance),
                ErrorStep::Incomplete(valid_up_to) => {
                    ScanOutcome::Incomplete(offset.saturating_add(valid_up_to))
                }
            }
        }
    }
}

/// Continue scanning unless the advance reaches the end of the buffer.
const fn resume_at(pending_len: usize, offset: usize, advance: usize) -> ScanOutcome {
    let next = offset.saturating_add(advance);
    if next >= pending_len {
        ScanOutcome::Consumed
    } else {
        ScanOutcome::Continue(next)
    }
}

/// One step of the decoder's forward scan.
#[derive(Clone, Copy, Debug)]
enum ErrorStep {
    /// Append one replacement and resume scanning this many bytes further on.
    Advance(usize),
    /// The remainder is an incomplete sequence whose valid prefix ends here.
    Incomplete(usize),
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

/// Append the valid UTF-8 prefix from the remaining slice to output.
fn append_valid_prefix(remaining: &[u8], output: &mut String, valid_up_to: ValidUpTo) {
    if valid_up_to.value() == 0 {
        return;
    }
    // The prefix normally comes from Utf8Error; keep validation local so
    // an incorrect bound cannot introduce undefined behaviour.
    let Some(prefix_bytes) = remaining.get(..valid_up_to.value()) else {
        return;
    };
    if let Ok(valid_prefix) = std::str::from_utf8(prefix_bytes) {
        output.push_str(valid_prefix);
    }
}

/// Replace one definite UTF-8 error and report how far to advance.
///
/// The caller has already appended the valid prefix. `Advance` carries the
/// already-appended prefix plus the invalid sequence, so the next scan starts
/// at the first unconsumed byte. No pending bytes are removed here.
fn handle_utf8_error(output: &mut String, err: &std::str::Utf8Error) -> ErrorStep {
    err.error_len().map_or_else(
        || ErrorStep::Incomplete(err.valid_up_to()),
        |error_len| {
            output.push('\u{FFFD}');
            // `Utf8Error` derives both bounds from the same slice, so the sum
            // stays in range; saturating addition keeps the step total without
            // trusting that invariant for panic-freedom.
            ErrorStep::Advance(err.valid_up_to().saturating_add(error_len))
        },
    )
}

/// Finish a scan that ended inside an incomplete UTF-8 sequence.
///
/// `tail_start` is the absolute position where the incomplete sequence starts.
/// A final chunk replaces the sequence and drops the buffer; otherwise the
/// already-consumed prefix is compacted away once, leaving only the at-most
/// three-byte tail for the next call.
fn handle_incomplete_sequence(
    pending: &mut Vec<u8>,
    output: &mut String,
    tail_start: usize,
    final_chunk: FinalChunk,
) {
    if final_chunk.is_final() {
        output.push('\u{FFFD}');
        pending.clear();
        return;
    }
    let tail = tail_start.min(pending.len());
    if tail > 0 {
        // Keep only the incomplete tail; the prefix is already in `output`.
        pending.drain(..tail);
    }
}

#[cfg(test)]
#[path = "utf8_tests.rs"]
mod tests;

#[cfg(kani)]
#[path = "utf8_kani_proofs.rs"]
mod kani_proofs;
