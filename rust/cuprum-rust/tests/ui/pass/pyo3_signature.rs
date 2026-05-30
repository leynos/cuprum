//! Compile-pass UI test: `#[pyo3(signature = (...))]` with a default argument.
//!
//! Validates that a `#[pyfunction]` annotated with an explicit PyO3 signature
//! that includes a defaulted parameter compiles without error.

use pyo3::prelude::*;

#[pyfunction]
#[pyo3(signature = (reader_fd, writer_fd, buffer_size = 65536))]
fn example_pump(_reader_fd: i64, _writer_fd: i64, _buffer_size: i64) -> PyResult<i64> {
    Ok(0)
}

fn main() {}
