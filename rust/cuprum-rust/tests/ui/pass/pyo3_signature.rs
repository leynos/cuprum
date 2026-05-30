//! Compile-pass UI test: `#[pyo3(signature = (...))]` with a default argument.
//!
//! Validates that a `#[pyfunction]` annotated with an explicit PyO3 signature
//! that includes a defaulted parameter compiles without error.

use pyo3::prelude::*;

#[pyfunction]
#[pyo3(signature = (reader_fd, writer_fd, buffer_size = 65536))]
fn example_pump(reader_fd: i64, writer_fd: i64, buffer_size: i64) -> PyResult<i64> {
    let _ = (reader_fd, writer_fd, buffer_size);
    Ok(0)
}

fn main() {}
