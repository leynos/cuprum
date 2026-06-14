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
            PumpError::Io(io_err) => io_err.into(),
            other @ (PumpError::LengthOverflow | PumpError::BufferRangeExceeded) => {
                PyOSError::new_err(other.py_os_error_message().unwrap_or("stream pump failed"))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    //! Unit tests for the canonical `PumpError` taxonomy: variant mapping,
    //! the non-fatal write predicate, and stable display messages.
    use super::PumpError;
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
}
