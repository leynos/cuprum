//! Unit and property tests for the typed stream boundary error.
//!
//! `RustStreamError` unifies the two argument-validation failures — an
//! out-of-range buffer size and an unrepresentable descriptor — with the
//! stream engine's own `PumpError`. These tests pin that classification and
//! the `thiserror` display messages the Python boundary reproduces. They run
//! without an interpreter: this crate is compiled with `pyo3/extension-module`,
//! so no `cargo test` binary can link Python, and assertions about `PyErr`
//! belong in the extension-required Python suite instead.

use cuprum_streams::PumpError;
use rstest::rstest;

use crate::{RustStreamError, convert_fd, validate_buffer_size};

/// Return an argument error's message, rejecting any other variant.
///
/// # Panics
/// Panics when the error is `Stream`, naming it, so a misclassification is
/// reported as the mismatch it is rather than as a message difference.
fn message_of(error: RustStreamError) -> String {
    match error {
        RustStreamError::InvalidBufferSize(message)
        | RustStreamError::InvalidDescriptor(message) => message.to_owned(),
        other @ RustStreamError::Stream(_) => {
            panic!("expected an argument variant, found {other:?}")
        }
    }
}

#[rstest]
#[case::zero(0, "buffer_size must be greater than zero")]
#[case::negative_one(-1, "buffer_size must be greater than zero")]
#[case::negative_large(i64::MIN + 1, "buffer_size must be greater than zero")]
#[case::i64_min(i64::MIN, "buffer_size must be greater than zero")]
#[case::cap_plus_one(
    (1_i64 << 30) + 1,
    "buffer_size exceeds the maximum permitted size"
)]
fn invalid_buffer_sizes_are_classified_with_their_message(
    #[case] size: i64,
    #[case] expected: &str,
) {
    // A non-positive size is refused before any conversion, so every negative
    // value shares one message however far below zero it sits.
    let error = validate_buffer_size(size).expect_err("an invalid size must be rejected");
    assert_eq!(
        message_of(error),
        expected,
        "buffer size {size} must report its stable message",
    );
}

/// An oversized value reports the overflow message only where one can occur.
///
/// `i64::MAX` has two distinct fates and the platform decides which: a 32-bit
/// `usize` cannot hold it, so the conversion fails; a 64-bit `usize` holds it
/// and the cap rejects it. Both are refusals, but the messages differ and the
/// Python boundary matches on the text, so the arm the target actually takes
/// is the one asserted here rather than a single assumed message.
#[cfg(target_pointer_width = "32")]
#[test]
fn i64_max_reports_the_conversion_overflow_on_a_narrow_target() {
    let error = validate_buffer_size(i64::MAX).expect_err("an invalid size must be rejected");
    assert_eq!(message_of(error), "buffer_size is too large");
}

#[cfg(not(target_pointer_width = "32"))]
#[test]
fn i64_max_reports_the_cap_on_a_wide_target() {
    let error = validate_buffer_size(i64::MAX).expect_err("an invalid size must be rejected");
    assert_eq!(
        message_of(error),
        "buffer_size exceeds the maximum permitted size",
    );
}

#[rstest]
#[case::one(1)]
#[case::default_buffer(65_536)]
#[case::cap(1_i64 << 30)]
fn accepted_buffer_sizes_convert_without_allocating(#[case] size: i64) {
    // Validation is pure: it decides a cap without reserving a gigabyte, so
    // the boundary itself can be asserted rather than approximated.
    assert!(
        validate_buffer_size(size).is_ok(),
        "buffer size {size} must be accepted",
    );
}

#[cfg(unix)]
#[rstest]
#[case::negative(-1, "file descriptor must be non-negative")]
#[case::i64_min(i64::MIN, "file descriptor out of range")]
#[case::above_i32(i64::from(i32::MAX) + 1, "file descriptor out of range")]
#[case::i64_max(i64::MAX, "file descriptor out of range")]
fn invalid_descriptors_are_classified_with_their_message(
    #[case] value: i64,
    #[case] expected: &str,
) {
    let error = convert_fd(value).expect_err("an invalid descriptor must be rejected");
    assert_eq!(
        message_of(error),
        expected,
        "descriptor {value} must report its stable message",
    );
}

#[cfg(unix)]
#[rstest]
#[case::stdin(0)]
#[case::stderr(2)]
#[case::i32_max(i64::from(i32::MAX))]
fn accepted_descriptors_convert(#[case] value: i64) {
    assert!(convert_fd(value).is_ok(), "descriptor {value} must convert");
}

#[cfg(windows)]
#[rstest]
#[case::negative(-1, "file handle must be non-negative")]
#[case::i64_min(i64::MIN, "file handle must be non-negative")]
fn invalid_handles_are_classified_with_their_message(#[case] value: i64, #[case] expected: &str) {
    let error = convert_fd(value).expect_err("an invalid handle must be rejected");
    assert_eq!(
        message_of(error),
        expected,
        "handle {value} must report its stable message",
    );
}

/// A buffer size is rejected as a buffer size, never as anything else.
///
/// This is the classification the Python boundary branches on: the two
/// argument variants become `ValueError` whilst `Stream` becomes `OSError`, so
/// a size that landed in the wrong variant would surface to callers as the
/// wrong exception class.
#[test]
fn a_rejected_size_never_reports_as_a_descriptor_or_stream_failure() {
    for size in [0, -1, i64::MIN, (1_i64 << 30) + 1, i64::MAX] {
        let error = validate_buffer_size(size).expect_err("an invalid size must be rejected");
        assert!(
            matches!(error, RustStreamError::InvalidBufferSize(_)),
            "size {size} must classify as a buffer-size failure, found {error:?}",
        );
    }
}

/// A stream failure retains exactly the `PumpError` it wrapped.
///
/// `Stream` is the variant the Python boundary resolves into a real `OSError`,
/// so the payload is not cosmetic: the conversion reads the variants' own
/// messages and, for `Io`, the raw code. A conversion that substituted a
/// different payload would change what callers receive.
#[rstest]
#[case::length_overflow(PumpError::LengthOverflow)]
#[case::buffer_range_exceeded(PumpError::BufferRangeExceeded)]
#[case::buffer_allocation_failed(PumpError::BufferAllocationFailed)]
fn a_stream_failure_retains_its_source(#[case] source: PumpError) {
    let expected = std::mem::discriminant(&source);
    let wrapped = RustStreamError::from(source);
    let RustStreamError::Stream(retained) = &wrapped else {
        panic!("a PumpError must wrap as the Stream variant, found {wrapped:?}");
    };
    assert_eq!(
        std::mem::discriminant(retained),
        expected,
        "the retained source must be the payload just wrapped",
    );
}

/// A converted stream failure must not report as an argument failure.
///
/// The `From` conversion is what the boundary's single `map_err` uses, so if
/// it routed a stream error into an argument variant the Python side would
/// raise `ValueError` for a genuine I/O failure.
#[test]
fn a_converted_stream_failure_stays_a_stream_failure() {
    let wrapped = RustStreamError::from(PumpError::BufferRangeExceeded);
    assert!(
        matches!(wrapped, RustStreamError::Stream(_)),
        "a stream failure must not convert into an argument variant, found {wrapped:?}",
    );
}

/// A raw OS failure wrapped as a stream error exposes its original code.
///
/// The code is the machine-readable part of the contract: `errno` on POSIX,
/// `winerror` on Windows. Asserting that the wrapped source still reports the
/// same number is what makes a conversion that swallowed the code fail here
/// rather than only in the Python suite.
#[test]
fn a_wrapped_os_failure_exposes_its_original_code() {
    let code = 9;
    let wrapped = RustStreamError::from(PumpError::from(std::io::Error::from_raw_os_error(code)));
    let RustStreamError::Stream(PumpError::Io(inner)) = &wrapped else {
        panic!("a raw OS failure must wrap as a stream I/O error, found {wrapped:?}");
    };
    assert_eq!(
        inner.raw_os_error(),
        Some(code),
        "the wrapped error must still report the code the syscall returned",
    );
}

/// A synthesized I/O failure keeps its kind and invents no OS code.
///
/// These are the errors the write paths raise in Rust rather than receive from
/// a syscall, so they carry no number. Routing them through the raw-code path
/// would fabricate one and hand Python a bogus `errno` to branch on.
#[test]
fn a_wrapped_synthesized_failure_reports_no_code() {
    let source = std::io::Error::new(
        std::io::ErrorKind::WriteZero,
        "failed to write whole buffer",
    );
    let wrapped = RustStreamError::from(PumpError::from(source));
    let RustStreamError::Stream(PumpError::Io(inner)) = &wrapped else {
        panic!("a synthesized failure must wrap as a stream I/O error, found {wrapped:?}");
    };
    assert_eq!(inner.kind(), std::io::ErrorKind::WriteZero);
    assert_eq!(
        inner.raw_os_error(),
        None,
        "a synthesized failure has no code to preserve",
    );
}

mod properties {
    //! Generated coverage for the argument classification.
    //!
    //! The `rstest` cases above fix hand-picked values. Over the whole `i64`
    //! domain the guarantee is twofold: every input falls on the correct side
    //! of the accepted window, and every rejection carries the buffer-size
    //! variant rather than the descriptor one — the distinction the Python
    //! boundary turns into `ValueError` versus `OSError`.

    use proptest::prelude::*;

    use super::{message_of, validate_buffer_size};
    use crate::RustStreamError;

    proptest! {
        /// Acceptance and rejection agree with the documented size window.
        #[test]
        fn validation_matches_the_size_window(size in any::<i64>()) {
            // `u64::try_from` rejects negative sizes rather than reinterpreting
            // them, so the window is stated once: strictly positive, and at
            // most the inclusive cap. Zero is inside `i64` but outside the
            // window, so it must be rejected — a `magnitude <= cap` test alone
            // would call it accepted and fail against correct code. The lower
            // bound is not exercised by `any::<i64>()` in practice, but the
            // oracle is stated for the whole domain rather than for the values
            // this generator happens to draw.
            let accepted = u64::try_from(size)
                .is_ok_and(|magnitude| magnitude > 0 && magnitude <= 1 << 30);
            prop_assert_eq!(
                validate_buffer_size(size).is_ok(),
                accepted,
                "size {} must {}be accepted",
                size,
                if accepted { "" } else { "not " },
            );
        }

        /// Every rejected size reports as a buffer-size failure with a message.
        #[test]
        fn every_rejection_is_a_buffer_size_failure(size in any::<i64>()) {
            if let Err(error) = validate_buffer_size(size) {
                prop_assert!(
                    matches!(error, RustStreamError::InvalidBufferSize(_)),
                    "size {} must classify as a buffer-size failure, found {:?}",
                    size,
                    error,
                );
                prop_assert!(
                    !message_of(error).is_empty(),
                    "size {} must carry a stable message",
                    size,
                );
            }
        }
    }
}
