//! Compile-fail UI test: the availability export remains runtime-only.

const RUST_AVAILABLE: bool = _rust_backend_native::is_available();

fn main() {}
