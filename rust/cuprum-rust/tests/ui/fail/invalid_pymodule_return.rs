//! Compile-fail UI test: `#[pymodule]` function must return `PyResult<()>`.
//!
//! Validates that returning a plain `i32` from a `#[pymodule]` function is
//! rejected by the PyO3 macro with a type-mismatch diagnostic.

use pyo3::prelude::*;

#[pymodule]
fn bad_module(_py: Python<'_>, module: &Bound<'_, PyModule>) -> i32 {
    let _ = (_py, module);
    0
}

fn main() {}
