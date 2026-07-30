//! Snapshot tests pinning the exact UTF-8 replacement output of the
//! [`consume_stream_files`] read loop.
//!
//! The incremental decoder in [`crate::utf8`] is proven equivalent to
//! `String::from_utf8_lossy` by the property tests there. These snapshots pin
//! the observable end-to-end output of the full read-and-decode loop across
//! four categories — pure ASCII, multi-byte sequences split across buffer
//! boundaries, invalid bytes, and an incomplete trailing sequence at EOF — so a
//! regression in the loop, the bounds-checked slicing, or the `final_chunk`
//! handling shows up as a concrete text diff.
//!
//! `TestRustConsumeStream` in `cuprum/unittests/test_rust_streams.py` declares
//! the same four categories against `rust_consume_stream`, checked with the
//! `payload.decode("utf-8", errors="replace")` oracle. Those cases are gated on
//! the compiled extension being importable, and neither `make build` nor the
//! CI test job builds it — only the benchmark job runs `maturin develop` — so
//! they skip in practice. These Rust-side snapshots are therefore the coverage
//! that actually executes for these categories, not a duplicate of it.
//!
//! Each case feeds a fixed payload through a real pipe, so the decode is driven
//! by actual descriptor reads rather than a synthetic byte buffer.

use crate::test_support::{make_pipe, write_all_to};
use crate::{BufferSize, consume_stream_files};

/// Decode `payload` through the real `consume_stream_files` read loop using a
/// pipe, reading `buffer_size` bytes at a time.
///
/// The whole payload is written and the write end closed before decoding, so
/// the loop reads the buffered bytes and then observes EOF. Payloads must stay
/// well within a pipe's capacity so the unbuffered write never blocks.
fn consume(payload: &[u8], buffer_size: usize) -> String {
    let (read_end, write_end) = make_pipe();
    write_all_to(&write_end, payload);
    // Close the write end so the read loop reaches EOF and terminates.
    drop(write_end);

    let mut reader = read_end;
    match consume_stream_files(&mut reader, BufferSize(buffer_size)) {
        Ok(text) => text,
        Err(err) => panic!("consume over a closed pipe failed: {err:?}"),
    }
}

#[test]
fn pure_ascii_decodes_verbatim() {
    let output = consume(b"cuprum reads pipes", 64);
    insta::assert_snapshot!(output, @"cuprum reads pipes");
}

#[test]
fn multibyte_sequences_split_across_buffer_boundaries() {
    // Each non-ASCII scalar is 2-3 bytes, so a one-byte buffer forces every
    // multi-byte sequence to straddle at least one read boundary.
    let payload = "héllo, 世界! ☕".as_bytes();
    let byte_at_a_time = consume(payload, 1);

    // The decode must be independent of where the buffer boundaries fall: a
    // one-byte, three-byte, and whole-payload buffer all yield the same text.
    assert_eq!(
        byte_at_a_time,
        consume(payload, 3),
        "a three-byte buffer must decode identically to a one-byte buffer",
    );
    assert_eq!(
        byte_at_a_time,
        consume(payload, 64),
        "a whole-payload buffer must decode identically to a one-byte buffer",
    );

    insta::assert_snapshot!(byte_at_a_time, @"héllo, 世界! ☕");
}

#[test]
fn invalid_bytes_become_replacement_characters() {
    // 0xFF is never a valid lead byte and 0x80 is a lone continuation byte;
    // each is replaced with a single U+FFFD, independent of the buffer size.
    let payload = b"x\xffy\x80z";
    let byte_at_a_time = consume(payload, 1);

    assert_eq!(
        byte_at_a_time,
        consume(payload, 3),
        "a three-byte buffer must decode identically to a one-byte buffer",
    );
    assert_eq!(
        byte_at_a_time,
        consume(payload, 64),
        "invalid-byte replacement must not depend on the buffer size",
    );

    insta::assert_snapshot!(byte_at_a_time, @"x�y�z");
}

#[test]
fn incomplete_trailing_sequence_is_replaced_at_eof() {
    // The euro sign is E2 82 AC; dropping the final byte leaves an incomplete
    // three-byte sequence that must resolve to a single U+FFFD once EOF marks
    // the final chunk, rather than being silently dropped.
    let payload = b"euro sign: \xe2\x82";
    let byte_at_a_time = consume(payload, 1);

    assert_eq!(
        byte_at_a_time,
        consume(payload, 3),
        "a three-byte buffer must decode identically to a one-byte buffer",
    );
    assert_eq!(
        byte_at_a_time,
        consume(payload, 64),
        "EOF replacement of an incomplete tail must not depend on the buffer size",
    );

    insta::assert_snapshot!(byte_at_a_time, @"euro sign: �");
}

/// Randomised coverage complementing the four fixed cases above.
///
/// The snapshots pin what the loop produces for four hand-picked payload
/// categories; they cannot show that the categories are exhaustive. These
/// properties generate arbitrary bytes and buffer sizes and check the same
/// invariant the snapshots encode by example: the loop's output is exactly
/// `String::from_utf8_lossy` of the input, whatever the buffer size, so no
/// generated payload can straddle a read boundary in a way the fixed cases
/// missed.
///
/// Payloads stay small so the unbuffered write in `consume` cannot fill the
/// pipe and block, which is the precondition the helper documents.
mod properties {
    use super::consume;
    use proptest::prelude::*;

    /// The largest payload a single unbuffered pipe write must accept.
    const MAX_PAYLOAD: usize = 512;

    proptest! {
        /// The loop reproduces `from_utf8_lossy` for any payload and buffer size.
        #[test]
        fn decodes_exactly_like_from_utf8_lossy(
            payload in prop::collection::vec(any::<u8>(), 0..MAX_PAYLOAD),
            buffer_size in 1_usize..=64,
        ) {
            let expected = String::from_utf8_lossy(&payload);
            prop_assert_eq!(
                consume(&payload, buffer_size),
                expected.into_owned(),
                "the read loop must decode identically to from_utf8_lossy",
            );
        }

        /// Where the read boundaries fall never changes the decoded text.
        #[test]
        fn decoding_is_independent_of_the_buffer_size(
            payload in prop::collection::vec(any::<u8>(), 0..MAX_PAYLOAD),
            first in 1_usize..=64,
            second in 1_usize..=64,
        ) {
            prop_assert_eq!(
                consume(&payload, first),
                consume(&payload, second),
                "two buffer sizes must split the same payload to the same text",
            );
        }
    }
}
