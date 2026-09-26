//! Python integration boundary for Cuprum's optional native stream backend.
//!
//! Raw-resource obligations are confined here and in `cuprum-native-io`.
//! Stream policy lives in `cuprum-streams`, which forbids unsafe code.
#![deny(unsafe_op_in_unsafe_fn)]

use cuprum_native_io::PlatformFd;
use cuprum_streams::{BufferSize, PumpError, consume_stream, pump_stream};
use pyo3::prelude::*;
use thiserror::Error;
mod errors;
#[cfg(test)]
mod fd_tests;
#[cfg(loom)]
#[doc(hidden)]
pub mod loom_model;
#[cfg(test)]
mod stream_error_behaviour;
#[cfg(test)]
mod stream_error_tests;

#[derive(Clone, Copy, Debug)]
struct ReaderFd(PlatformFd);

/// Classify a failure at the native stream boundary.
///
/// The two argument variants exist so a caller can distinguish a malformed
/// request from a failing stream: they become `ValueError` in Python, whilst
/// [`Self::Stream`] becomes `OSError` carrying the engine's own code. Wrapping
/// [`PumpError`] rather than copying its variants keeps `cuprum-streams` the
/// single owner of stream policy and avoids a parallel taxonomy that could
/// drift from it.
#[derive(Debug, Error)]
enum RustStreamError {
    /// The requested buffer size is not a permitted allocation size.
    #[error("{0}")]
    InvalidBufferSize(&'static str),
    /// A Python descriptor value does not denote a usable native handle.
    #[error("{0}")]
    InvalidDescriptor(&'static str),
    /// The stream engine failed.
    ///
    /// Transparent, so the message and, more importantly, the raw OS code
    /// reach the existing converter unchanged.
    #[error(transparent)]
    Stream(#[from] PumpError),
}

fn validate_buffer_size(size: i64) -> Result<BufferSize, RustStreamError> {
    BufferSize::new(size).map_err(RustStreamError::InvalidBufferSize)
}

/// Report whether the Rust extension is available.
///
/// This entry point is only reached once the native module has loaded, so it
/// always reports `true`. The Python wrapper treats a failed import as
/// "unavailable", so no runtime probing is needed here.
///
/// # Returns
/// `true` whenever the extension is loaded and callable.
#[must_use]
#[doc(hidden)]
#[pyfunction]
pub const fn is_available() -> bool { true }

#[expect(
    clippy::allow_attributes,
    reason = "PyO3 emits the argument-count lint only in some build configurations"
)]
#[allow(
    clippy::too_many_arguments,
    reason = "PyO3 generates five-parameter wrappers for these stable Python FFI functions"
)]
mod stream_pyfunctions;

use stream_pyfunctions::{rust_consume_stream, rust_pump_stream};

#[cfg(any(unix, windows))]
fn convert_fd(value: i64) -> Result<PlatformFd, RustStreamError> {
    convert_platform_fd(value).map_err(RustStreamError::InvalidDescriptor)
}

#[cfg(unix)]
fn convert_platform_fd(value: i64) -> Result<PlatformFd, &'static str> {
    let fd = i32::try_from(value).map_err(|_| "file descriptor out of range")?;
    if fd < 0 {
        return Err("file descriptor must be non-negative");
    }
    Ok(fd)
}

#[cfg(windows)]
fn convert_platform_fd(value: i64) -> Result<PlatformFd, &'static str> {
    // Reject negative handles for symmetry with the Unix arm: Python hands
    // over non-negative handle values, and reinterpreting a negative i64 as
    // a pointer-sized handle would silently address nonsense.
    if value < 0 {
        return Err("file handle must be non-negative");
    }
    usize::try_from(value).map_err(|_| "file handle out of range")
}

/// Python module definition for the optional Rust backend.
///
/// # Errors
/// Returns a Python error if the module cannot be initialized.
#[pymodule]
fn _rust_backend_native(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(is_available, module)?)?;
    module.add_function(wrap_pyfunction!(rust_pump_stream, module)?)?;
    module.add_function(wrap_pyfunction!(rust_consume_stream, module)?)?;
    module.add("__doc__", "Cuprum optional Rust backend.")?;
    module.add("__package__", "cuprum")?;
    module.add("__loader__", py.None())?;
    Ok(())
}
