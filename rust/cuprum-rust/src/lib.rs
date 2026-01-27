//! Optional Rust extension for Cuprum stream operations.
//!
//! This crate exposes a minimal PyO3 module that allows Python to detect
//! whether the Rust extension is available. The core stream operations are
//! implemented later in the roadmap.

use pyo3::prelude::*;

/// Report whether the Rust extension is available.
///
/// # Returns
/// `true` when the extension is successfully loaded.
#[pyfunction]
#[must_use]
fn is_available(_py: Python<'_>) -> PyResult<bool> {
    Ok(true)
}

/// Python module definition for the optional Rust backend.
///
/// # Errors
/// Returns a Python error if the module cannot be initialized.
#[pymodule]
fn _rust_backend_native(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(is_available, module)?)?;
    module.add("__doc__", "Cuprum optional Rust backend.")?;
    module.add("__package__", "cuprum")?;
    module.add("__loader__", py.None())?;
    Ok(())
}
