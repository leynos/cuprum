//! Compile-pass UI test: the native availability marker is const-evaluable.
//!
//! Validates that the native marker retains its compile-time contract while
//! its PyO3 registration continues to provide the runtime Python export.

const RUST_AVAILABLE: bool = _rust_backend_native::is_available();

fn main() {
    assert!(RUST_AVAILABLE, "the loaded native extension must be available");
}
