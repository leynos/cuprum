//! Compile-fail UI test: the availability export remains runtime-only.

use pyo3::prelude::*;

#[pyfunction]
fn is_available() -> bool {
    true
}

const RUST_AVAILABLE: bool = is_available();

fn main() {}
