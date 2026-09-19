//! Python integration boundary for Cuprum's optional native stream backend.
//!
//! Raw-resource obligations are confined here and in `cuprum-native-io`.
//! Stream policy lives in `cuprum-streams`, which forbids unsafe code.
#![deny(unsafe_op_in_unsafe_fn)]

use cuprum_native_io::PlatformFd;
use cuprum_streams::{BufferSize, PumpError, consume_stream, pump_stream};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
mod errors;
#[cfg(test)]
mod fd_tests;

#[derive(Clone, Copy, Debug)]
struct ReaderFd(PlatformFd);

fn validate_buffer_size(size: i64) -> PyResult<BufferSize> {
    BufferSize::new(size).map_err(PyValueError::new_err)
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
pub const fn is_available() -> bool {
    true
}

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
fn convert_fd(value: i64) -> PyResult<PlatformFd> {
    convert_platform_fd(value).map_err(PyValueError::new_err)
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
