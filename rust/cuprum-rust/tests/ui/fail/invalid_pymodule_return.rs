use pyo3::prelude::*;

#[pyfunction]
fn example() -> PyResult<i32> {
    Ok(1)
}

#[pymodule]
fn bad_module(_py: Python<'_>, module: &Bound<'_, PyModule>) -> i32 {
    module.add_function(wrap_pyfunction!(example, module)?)?;
    0
}

fn main() {}
