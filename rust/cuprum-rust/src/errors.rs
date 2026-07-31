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

/// Convert an [`io::Error`] into a Python exception that keeps its `errno`.
///
/// `PyO3`'s own `From<io::Error> for PyErr` picks the exception *type* from
/// `io::ErrorKind`, then constructs it with a single argument — the error's
/// `Display` string. Python only populates `OSError.errno` and
/// `OSError.strerror` when it receives **two or more** arguments, so a
/// single-argument construction leaves `errno` as `None`: the number survives
/// in the message text but not anywhere a caller can branch on. Callers are
/// then forced to parse English to tell `EBADF` from `EPIPE`, and message text
/// is not a stable interface.
///
/// Constructing `OSError(code, strerror)` instead fixes both halves at once,
/// because `CPython` maps the errno to the matching subclass itself:
/// `OSError(32, ...)` *is* a `BrokenPipeError`. That is the same subclass
/// selection `PyO3` was reaching for through `ErrorKind`, obtained from the
/// authoritative source rather than a parallel table.
///
/// An `io::Error` with no `raw_os_error` — one synthesized in Rust rather than
/// returned by a syscall — has no number to preserve, so `PyO3`'s `ErrorKind`
/// mapping remains the best available and is used unchanged.
fn io_error_to_py_err(err: io::Error) -> PyErr {
    match err.raw_os_error() {
        Some(code) => {
            let strerror = strip_os_error_suffix(&err.to_string(), code);
            PyOSError::new_err((code, strerror))
        }
        None => err.into(),
    }
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
}
