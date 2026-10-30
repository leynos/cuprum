//! Named vectors, split matrices, and property tests for the UTF-8
//! replacement decoder.
//!
//! The named rows pin each replacement and boundary category; the split
//! matrices cross every partition of them against
//! `String::from_utf8_lossy`; and the property tests generalize the same
//! oracle over arbitrary bytes and chunk boundaries.

use proptest::prelude::*;

use super::{FinalChunk, decode_chunks, decode_utf8_replace};

fn split_input_at_points<'input>(input: &'input [u8], split_points: &[usize]) -> Vec<&'input [u8]> {
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

/// Long run of invalid bytes: every byte is its own replacement.
const INVALID_RUN: [u8; 512] = [0xff; 512];

/// Named byte sequences that pin each replacement and boundary path.
///
/// The overlong, surrogate, and beyond-maximum scalars are rejected by
/// every conforming UTF-8 decoder. The trailing-truncation rows pin the
/// EOF replacement path, and the valid scalars pin that neighbouring
/// malformed rows did not widen replacement onto well-formed input.
const NAMED_INPUTS: &[(&str, &[u8])] = &[
    ("overlong_two_byte", b"\xC0\x80"),
    ("overlong_three_byte", b"\xE0\x80\x80"),
    ("surrogate", b"\xED\xA0\x80"),
    ("above_max_scalar", b"\xF4\x90\x80\x80"),
    ("lone_continuation_run", b"\x80\x80\x80"),
    ("long_invalid_run", &INVALID_RUN),
    ("truncated_two_byte_eof", b"ok\xE2"),
    ("truncated_three_byte_eof", b"ok\xE2\x82"),
    ("truncated_four_byte_eof", b"ok\xF0\x9F\x98"),
    ("valid_two_byte", b"na\xC3\xAFve"),
    ("valid_three_byte", b"snow \xE2\x98\x83"),
    ("valid_four_byte", b"grin \xF0\x9F\x98\x80"),
];

/// True when `pending` is empty or a valid prefix of an incomplete sequence.
const fn is_incomplete_tail(pending: &[u8]) -> bool {
    pending.is_empty()
        || matches!(
            std::str::from_utf8(pending),
            Err(err) if err.error_len().is_none()
        )
}

#[test]
fn named_byte_sequences_match_lossy_decoding() {
    for (name, input) in NAMED_INPUTS {
        let (output, pending) = decode_single_chunk(input, true);
        assert_eq!(
            output,
            String::from_utf8_lossy(input).into_owned(),
            "{name}"
        );
        assert!(pending.is_empty(), "{name}: final chunk retains no bytes");
    }
}

#[test]
fn every_split_point_matches_lossy_decoding() {
    for (name, input) in NAMED_INPUTS {
        let expected = String::from_utf8_lossy(input).into_owned();
        for split in 0..=input.len() {
            let (left, right) = input.split_at(split);
            let (output, pending) = decode_chunks(&[left, right], true);
            assert_eq!(output, expected, "{name}: two-chunk split at {split}");
            assert!(
                pending.is_empty(),
                "{name}: split at {split} leaves no bytes"
            );
        }
    }
}

#[test]
fn every_two_split_partition_matches_lossy_decoding() {
    for (name, input) in NAMED_INPUTS {
        let expected = String::from_utf8_lossy(input).into_owned();
        for first in 0..=input.len() {
            for second in first..=input.len() {
                let (left, rest) = input.split_at(first);
                let (middle, right) = rest.split_at(second - first);
                let (output, pending) = decode_chunks(&[left, middle, right], true);
                assert_eq!(
                    output, expected,
                    "{name}: three-chunk splits at {first},{second}"
                );
                assert!(
                    pending.is_empty(),
                    "{name}: splits at {first},{second} leave no bytes"
                );
            }
        }
    }
}

#[test]
fn single_byte_chunks_match_lossy_decoding() {
    for (name, input) in NAMED_INPUTS {
        let chunks: Vec<&[u8]> = input.chunks(1).collect();
        let (output, pending) = decode_chunks(&chunks, true);
        assert_eq!(
            output,
            String::from_utf8_lossy(input).into_owned(),
            "{name}"
        );
        assert!(
            pending.is_empty(),
            "{name}: single-byte chunks leave no bytes"
        );
    }
}

#[test]
fn non_final_chunks_retain_only_an_incomplete_tail() {
    for (name, input) in NAMED_INPUTS {
        let expected = String::from_utf8_lossy(input).into_owned();
        for split in 0..=input.len() {
            let (left, right) = input.split_at(split);
            let mut pending = Vec::new();
            let mut output = String::from("seed:");
            for chunk in [left, right] {
                pending.extend_from_slice(chunk);
                let before = output.clone();
                decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(false));
                assert!(
                    output.starts_with(&before),
                    "{name}: decoding must only append to output"
                );
                assert!(
                    pending.len() <= 3,
                    "{name}: non-final pending is at most three bytes"
                );
                assert!(
                    is_incomplete_tail(&pending),
                    "{name}: non-final pending must be an incomplete UTF-8 prefix"
                );
            }
            decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(true));
            assert_eq!(
                output,
                format!("seed:{expected}"),
                "{name}: split at {split}"
            );
            assert!(pending.is_empty(), "{name}: final chunk retains no bytes");
        }
    }
}

fn incomplete_tail_strategy() -> impl Strategy<Value = Vec<u8>> {
    prop_oneof![
        Just(vec![0xc2]),
        Just(vec![0xdf]),
        Just(vec![0xe0, 0xa0]),
        Just(vec![0xe1, 0x80]),
        Just(vec![0xef, 0xbf]),
        Just(vec![0xf0, 0x90, 0x80]),
        Just(vec![0xf1, 0x80, 0x80]),
        Just(vec![0xf4, 0x8f, 0xbf]),
    ]
}

proptest! {
    #![proptest_config(crate::miri_proptest_config())]

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
