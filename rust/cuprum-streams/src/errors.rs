//! Semantic errors for safe stream orchestration.
use std::io;
use thiserror::Error;

/// Semantic error for stream pump and consume operations.
#[derive(Debug, Error)]
pub enum PumpError {
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
    #[must_use]
    pub fn is_nonfatal_write(&self) -> bool {
        matches!(
            self,
            Self::Io(err) if matches!(
                err.kind(),
                io::ErrorKind::BrokenPipe | io::ErrorKind::ConnectionReset
            )
        )
    }

    /// Return the Python `OSError` message for semantic non-I/O variants.
    #[must_use]
    pub const fn py_os_error_message(&self) -> Option<&'static str> {
        match self {
            Self::LengthOverflow => Some("integer length conversion overflowed"),
            Self::BufferRangeExceeded => Some("computed range exceeded the buffer bounds"),
            Self::Io(_) => None,
        }
    }
}
