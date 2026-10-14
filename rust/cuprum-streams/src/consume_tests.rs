//! Platform-independent tests for the shared [`consume_with_reader`] loop.
//!
//! The descriptor-backed snapshots in `consume_snapshot_tests` only run on
//! Unix. Driving the shared loop through a scripted read closure lets these
//! cases run on every platform and pin read boundaries exactly, including a
//! mid-stream read error that a real pipe cannot produce on demand.

use std::{collections::VecDeque, io};

use rstest::rstest;

use crate::{BufferSize, consume_with_reader, errors::PumpError};

/// One scripted outcome of the read closure.
enum Step {
    Bytes(Vec<u8>),
    Fail(io::ErrorKind),
}

/// Scripted reader that records how often the loop called it.
struct ScriptedReader {
    steps: VecDeque<Step>,
    calls: usize,
}

impl ScriptedReader {
    fn new(steps: impl IntoIterator<Item = Step>) -> Self {
        Self {
            steps: steps.into_iter().collect(),
            calls: 0,
        }
    }

    /// Serve the next scripted step, reporting EOF once the script runs out.
    fn read(&mut self, buffer: &mut [u8]) -> Result<usize, PumpError> {
        self.calls += 1;
        match self.steps.pop_front() {
            None => Ok(0),
            Some(Step::Fail(kind)) => Err(PumpError::from(io::Error::from(kind))),
            Some(Step::Bytes(bytes)) => {
                let target = buffer
                    .get_mut(..bytes.len())
                    .ok_or(PumpError::BufferRangeExceeded)?;
                target.copy_from_slice(&bytes);
                Ok(bytes.len())
            }
        }
    }
}

fn consume(reader: &mut ScriptedReader) -> Result<String, PumpError> {
    consume_with_reader(|buffer| reader.read(buffer), BufferSize(8), "test")
}

#[rstest]
#[case::empty_stream(&[], "")]
#[case::two_byte_sequence_split(&[b"na\xC3".as_slice(), b"\xAFve"], "naïve")]
#[case::four_byte_sequence_split_three_ways(
    &[b"\xF0".as_slice(), b"\x9F\x98", b"\x80"],
    "\u{1F600}"
)]
#[case::invalid_byte_between_reads(&[b"a\xFF".as_slice(), b"b"], "a\u{FFFD}b")]
#[case::incomplete_sequence_at_eof(&[b"ok".as_slice(), b"\xE2\x82"], "ok\u{FFFD}")]
fn decodes_across_read_boundaries(#[case] chunks: &[&[u8]], #[case] expected: &str) {
    let mut reader = ScriptedReader::new(chunks.iter().map(|chunk| Step::Bytes(chunk.to_vec())));

    let output = consume(&mut reader).expect("scripted reads must decode");

    assert_eq!(output, expected);
}

#[test]
fn stops_reading_at_the_first_eof() {
    // Bytes scripted after the zero-length read must never be requested.
    let mut reader = ScriptedReader::new([
        Step::Bytes(b"done".to_vec()),
        Step::Bytes(b"".to_vec()),
        Step::Bytes(b"unreachable".to_vec()),
    ]);

    let output = consume(&mut reader).expect("scripted reads must decode");

    assert_eq!(output, "done");
    assert_eq!(
        reader.calls, 2,
        "the loop must stop at the zero-length read"
    );
    assert_eq!(reader.steps.len(), 1, "no read may follow EOF");
}

#[test]
fn propagates_a_read_error_after_partial_output() {
    let mut reader = ScriptedReader::new([
        Step::Bytes(b"partial".to_vec()),
        Step::Fail(io::ErrorKind::PermissionDenied),
        Step::Bytes(b"unreachable".to_vec()),
    ]);

    let result = consume(&mut reader);

    assert!(
        matches!(
            &result,
            Err(PumpError::Io(err)) if err.kind() == io::ErrorKind::PermissionDenied
        ),
        "expected the read error to propagate, got {result:?}"
    );
    assert_eq!(reader.calls, 2, "the loop must stop at the failing read");
}
