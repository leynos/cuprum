//! Canonical semantic error type for the stream pump and consume paths.
//!
//! `PumpError` is the single error taxonomy for the crate's internal stream
//! operations. The previously scattered `io::Error::other("...")`
//! constructions for "impossible" overflow and slice conditions are replaced
//! by machine-distinguishable variants; conversion to a Python exception
//! happens in exactly one place (`From<PumpError> for PyErr`).

use std::io;

use pyo3::PyErr;
use pyo3::exceptions::PyOSError;
use thiserror::Error;

/// Semantic error for stream pump and consume operations.
#[derive(Debug, Error)]
pub(crate) enum PumpError {
    /// An integer length conversion overflowed its target type.
    ///
    /// This is an "impossible" condition on supported platforms (for
    /// example, a non-negative `ssize_t` always fits a `usize` on Linux);
    /// the variant exists so the condition stays observable rather than
    /// silently truncating.
    #[error("integer length conversion overflowed")]
    LengthOverflow,
    /// A computed range exceeded the backing buffer's bounds.
    #[error("computed range exceeded the buffer bounds")]
    BufferRangeExceeded,
    /// An operating-system I/O failure.
    #[error(transparent)]
    Io(#[from] io::Error),
}

impl PumpError {
    /// Report whether this is a non-fatal write condition (broken pipe).
    ///
    /// These errors indicate the write end closed, which is expected when
    /// downstream processes exit early. The caller should drain the reader
    /// and return successfully rather than propagating the error.
    pub(crate) fn is_nonfatal_write(&self) -> bool {
        matches!(
            self,
            Self::Io(err) if matches!(
                err.kind(),
                io::ErrorKind::BrokenPipe | io::ErrorKind::ConnectionReset
            )
        )
    }

    /// Return the Python `OSError` message for semantic non-I/O variants.
    pub(crate) const fn py_os_error_message(&self) -> Option<&'static str> {
        match self {
            Self::LengthOverflow => Some("integer length conversion overflowed"),
            Self::BufferRangeExceeded => Some("computed range exceeded the buffer bounds"),
            Self::Io(_) => None,
        }
    }
}

impl From<PumpError> for PyErr {
    fn from(err: PumpError) -> Self {
        match err {
            PumpError::Io(io_err) => io_error_to_py_err(io_err),
            other @ (PumpError::LengthOverflow | PumpError::BufferRangeExceeded) => {
                PyOSError::new_err(other.py_os_error_message().unwrap_or("stream pump failed"))
            }
        }
    }
}

/// Strip the `" (os error N)"` suffix Rust appends to a raw OS error.
///
/// `io::Error`'s `Display` renders a raw OS error as `"{strerror} (os error
/// {code})"`. Passing that whole string as `strerror` would render as
/// `"[Errno 9] Bad file descriptor (os error 9)"`, stating the number twice.
/// If the format ever changes the suffix simply will not match and the full
/// message is used, so this degrades to the previous wording rather than
/// mangling it.
fn strip_os_error_suffix(message: &str, code: i32) -> String {
    let suffix = format!(" (os error {code})");
    message.strip_suffix(&suffix).unwrap_or(message).to_owned()
}

/// Build a Python `OSError` from a POSIX `errno` and its message.
///
/// The two-argument form is what makes the number reachable: Python populates
/// `OSError.errno` and `OSError.strerror` only when it receives **two or more**
/// arguments. It also fixes the exception type, because `CPython` maps the
/// errno to the matching subclass itself — `OSError(32, ...)` *is* a
/// `BrokenPipeError`. That is the same subclass selection `PyO3` reaches for
/// through `io::ErrorKind`, taken from the authoritative source rather than a
/// parallel table.
#[cfg(unix)]
fn os_error_to_py_err(code: i32, strerror: String) -> PyErr {
    PyOSError::new_err((code, strerror))
}

/// Build a Python `OSError` from a Win32 error code and its message.
///
/// On Windows `raw_os_error` carries a `GetLastError` code, not an `errno`.
/// Passing it where Python expects an `errno` would assign an unrelated number
/// — `ERROR_INVALID_HANDLE` is 6, which as an `errno` is `ENXIO` — leave
/// `winerror` unset, and select the subclass from that wrong number.
///
/// The five-argument form `OSError(errno, strerror, filename, winerror,
/// filename2)` is the one that carries a native code. Given a `winerror`,
/// `CPython` ignores the `errno` argument, derives `errno` from the Win32 code
/// itself, and then selects the subclass from the derived value, so all three
/// fields agree. The leading zero is therefore a placeholder, and the two
/// `None`s are the absent filenames.
#[cfg(windows)]
fn os_error_to_py_err(code: i32, strerror: String) -> PyErr {
    PyOSError::new_err((0, strerror, None::<String>, code, None::<String>))
}

/// Convert an [`io::Error`] into a Python exception that keeps its OS code.
///
/// `PyO3`'s own `From<io::Error> for PyErr` picks the exception *type* from
/// `io::ErrorKind`, then constructs it with a single argument — the error's
/// `Display` string. A single-argument construction leaves `errno` as `None`:
/// the number survives in the message text but not anywhere a caller can
/// branch on. Callers are then forced to parse English to tell `EBADF` from
/// `EPIPE`, and message text is not a stable interface.
///
/// The platform decides how the number must be handed over, so the
/// construction itself lives in the `cfg`-selected [`os_error_to_py_err`].
///
/// An `io::Error` with no `raw_os_error` — one synthesized in Rust rather than
/// returned by a syscall — has no number to preserve, so `PyO3`'s `ErrorKind`
/// mapping remains the best available and is used unchanged.
fn io_error_to_py_err(err: io::Error) -> PyErr {
    match raw_os_error_parts(&err) {
        Some((code, strerror)) => os_error_to_py_err(code, strerror),
        None => err.into(),
    }
}

/// Split a syscall failure into the code and message handed to Python.
///
/// `None` means the error carries no operating-system number and the caller
/// must fall back to `PyO3`'s `ErrorKind` mapping. Every `io::Error` built in
/// Rust rather than returned by a syscall lands here, including the
/// `ErrorKind::WriteZero` the write paths raise when a write makes no progress.
///
/// The decision is split out from [`io_error_to_py_err`] so it can be tested
/// directly. Inspecting the built `PyErr` instead would need a live
/// interpreter, and this crate is compiled with `pyo3/extension-module`, which
/// deliberately leaves the Python symbols to be resolved by the host process —
/// so no `cargo test` binary can link one.
fn raw_os_error_parts(err: &io::Error) -> Option<(i32, String)> {
    let code = err.raw_os_error()?;
    Some((code, strip_os_error_suffix(&err.to_string(), code)))
}

#[cfg(test)]
mod tests {
    //! Unit tests for the canonical `PumpError` taxonomy: variant mapping,
    //! the non-fatal write predicate, and stable display messages.
    use super::PumpError;
    use rstest::rstest;
    use std::io;

    #[test]
    fn io_errors_round_trip_their_kind() {
        let source = io::Error::new(io::ErrorKind::BrokenPipe, "downstream closed");
        let err = PumpError::from(source);
        match &err {
            PumpError::Io(inner) => assert_eq!(inner.kind(), io::ErrorKind::BrokenPipe),
            other => panic!("expected Io variant, got {other:?}"),
        }
    }

    #[test]
    fn only_broken_pipe_and_connection_reset_are_nonfatal() {
        let nonfatal_kinds = [io::ErrorKind::BrokenPipe, io::ErrorKind::ConnectionReset];
        for kind in nonfatal_kinds {
            let err = PumpError::from(io::Error::new(kind, "closed"));
            assert!(err.is_nonfatal_write(), "{kind:?} must be non-fatal");
        }

        let fatal_kinds = [
            io::ErrorKind::NotFound,
            io::ErrorKind::PermissionDenied,
            io::ErrorKind::WriteZero,
            io::ErrorKind::Interrupted,
            io::ErrorKind::Other,
        ];
        for kind in fatal_kinds {
            let err = PumpError::from(io::Error::new(kind, "boom"));
            assert!(!err.is_nonfatal_write(), "{kind:?} must be fatal");
        }
        assert!(!PumpError::LengthOverflow.is_nonfatal_write());
        assert!(!PumpError::BufferRangeExceeded.is_nonfatal_write());
    }

    #[test]
    fn overflow_variants_have_stable_messages() {
        assert_eq!(
            PumpError::LengthOverflow.to_string(),
            "integer length conversion overflowed",
        );
        assert_eq!(
            PumpError::BufferRangeExceeded.to_string(),
            "computed range exceeded the buffer bounds",
        );
    }

    #[test]
    fn semantic_overflow_errors_define_py_os_error_messages() {
        assert_eq!(
            PumpError::LengthOverflow.py_os_error_message(),
            Some("integer length conversion overflowed"),
        );
        assert_eq!(
            PumpError::BufferRangeExceeded.py_os_error_message(),
            Some("computed range exceeded the buffer bounds"),
        );
        assert_eq!(
            PumpError::from(io::Error::other("boom")).py_os_error_message(),
            None,
        );
    }

    #[rstest]
    #[case::raw_os_error("Bad file descriptor (os error 9)", 9, "Bad file descriptor")]
    #[case::other_code("Broken pipe (os error 32)", 32, "Broken pipe")]
    #[case::no_suffix("something went wrong", 9, "something went wrong")]
    #[case::mismatched_code(
        "Bad file descriptor (os error 9)",
        22,
        "Bad file descriptor (os error 9)"
    )]
    fn strip_os_error_suffix_removes_only_its_own_code(
        #[case] message: &str,
        #[case] code: i32,
        #[case] expected: &str,
    ) {
        // A mismatched code must leave the message intact rather than trimming
        // a suffix that belongs to a different error: the `strerror` a caller
        // reads should never be silently truncated.
        assert_eq!(
            super::strip_os_error_suffix(message, code),
            expected,
            "stripping must be anchored to this error's own code",
        );
    }

    /// A syscall failure surrenders its code and its message without the
    /// suffix `io::Error` appends.
    ///
    /// The expected `strerror` is not spelled out, so the case holds whatever
    /// the platform calls this code: it asserts that putting the suffix back
    /// reproduces `Display` exactly, which pins that the helper removes the
    /// suffix and nothing else and returns the code unaltered.
    #[test]
    fn a_raw_os_error_yields_its_code_and_bare_strerror() {
        let code = 9;
        let err = io::Error::from_raw_os_error(code);
        let rendered = err.to_string();

        let Some((reported, strerror)) = super::raw_os_error_parts(&err) else {
            panic!("a raw OS error must report its code, found none for {err:?}")
        };

        assert_eq!(reported, code, "the code must survive unaltered");
        assert_eq!(
            rendered,
            format!("{strerror} (os error {code})"),
            "the message must lose exactly the suffix Rust appended",
        );
    }

    /// An error built in Rust has no number, so the conversion falls back.
    ///
    /// This is the arm that hands the error to `PyO3` unchanged. It is not
    /// hypothetical: the write paths in `io_utils` raise
    /// `ErrorKind::WriteZero` in exactly this way when a write makes no
    /// progress, so a regression that fabricated a code for an error that has
    /// none would reach production. Both `io::Error` representations are
    /// covered — one carrying a custom payload, one built from a bare kind.
    #[rstest]
    #[case::write_zero_from_the_write_paths(io::Error::new(
        io::ErrorKind::WriteZero,
        "failed to write whole buffer",
    ))]
    #[case::other_with_a_message(io::Error::other("boom"))]
    #[case::bare_kind(io::Error::from(io::ErrorKind::BrokenPipe))]
    fn a_synthesized_error_has_no_code_to_preserve(#[case] err: io::Error) {
        assert!(
            super::raw_os_error_parts(&err).is_none(),
            "an error with no raw_os_error must select PyO3's ErrorKind mapping \
             rather than a fabricated code: {err:?}",
        );
    }

    mod properties {
        //! Generated coverage for [`super::super::strip_os_error_suffix`].
        //!
        //! The `rstest` cases above fix four hand-picked messages. They cannot
        //! show that stripping is anchored to *this* error's code for every
        //! message and every `i32`, which is the whole point of the helper: it
        //! must never truncate a `strerror` a caller reads, and must never
        //! leave the number stated twice.

        use proptest::prelude::*;

        /// Messages shaped like the ones this helper meets, plus near-misses.
        ///
        /// An unconstrained `".*"` almost never produces a string ending in
        /// `)`, so it cannot tell a correct implementation from one that trims
        /// trailing punctuation off messages it does not recognize. The arms
        /// are: a plain `strerror`, one already carrying a rendered suffix, and
        /// one that merely ends the way a suffix does.
        fn os_error_message() -> impl Strategy<Value = String> {
            prop_oneof![
                "[^()]*",
                ("[^()]*", any::<i32>())
                    .prop_map(|(text, code)| format!("{text} (os error {code})")),
                "[^()]*\\)",
            ]
        }

        proptest! {
            /// Stripping undoes appending, for any message and any code.
            ///
            /// This is the case that actually occurs: `io::Error` renders a raw
            /// OS error as exactly `"{strerror} (os error {code})"`, so the
            /// helper must recover `strerror` whatever the two contain.
            #[test]
            fn stripping_undoes_appending_the_matching_suffix(
                message in os_error_message(),
                code in any::<i32>(),
            ) {
                let rendered = format!("{message} (os error {code})");
                prop_assert_eq!(
                    super::super::strip_os_error_suffix(&rendered, code),
                    message,
                    "the rendered form must strip back to the bare strerror",
                );
            }

            /// A suffix bearing a different code is left in place.
            ///
            /// Trimming it would hand the caller a truncated `strerror`, and
            /// would hide that the message came from another error entirely.
            #[test]
            fn a_mismatched_code_leaves_the_message_untouched(
                message in os_error_message(),
                code in any::<i32>(),
                other in any::<i32>(),
            ) {
                prop_assume!(code != other);
                let rendered = format!("{message} (os error {other})");
                prop_assert_eq!(
                    super::super::strip_os_error_suffix(&rendered, code),
                    rendered,
                    "only this error's own suffix may be removed",
                );
            }

            /// The result is never longer than the input, and only ever shrinks
            /// by the exact suffix.
            ///
            /// Nothing above rules out the helper *rewriting* a message it does
            /// not recognize; this pins that it either returns the input
            /// verbatim or removes precisely one known suffix.
            #[test]
            fn the_message_is_returned_verbatim_or_minus_its_own_suffix(
                message in os_error_message(),
                code in any::<i32>(),
            ) {
                let stripped = super::super::strip_os_error_suffix(&message, code);
                let removed = format!(" (os error {code})");
                prop_assert!(
                    stripped == message
                        || format!("{stripped}{removed}") == message,
                    "unrecognized messages must pass through unchanged",
                );
            }
        }
    }
}
