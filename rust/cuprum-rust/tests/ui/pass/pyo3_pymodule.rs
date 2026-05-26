use pyo3::prelude::*;

#[pyfunction]
fn is_available(_py: Python<'_>) -> PyResult<bool> {
    Ok(true)
}

#[pymodule]
fn compile_pass_module(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(is_available, module)?)?;
    module.add("__doc__", "Cuprum optional Rust backend.")?;
    module.add("__package__", "cuprum")?;
    module.add("__loader__", py.None())?;
    Ok(())
}

fn main() {}
