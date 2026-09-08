//! Shared Unix file-descriptor fixtures and assertions for Rust unit tests.

use cap_std::fs::File;
use std::fmt::Debug;
use std::io::{self, Read, Write};
use std::os::fd::OwnedFd;

/// Create an anonymous pipe as `(read_end, write_end)`.
///
/// Returns the operating-system error from `pipe(2)` so fixtures can pass it
/// to the test body, where the failure receives assertion context.
pub(crate) fn make_pipe() -> io::Result<(OwnedFd, OwnedFd)> {
    cuprum_native_io::pipe()
}

/// Duplicate a typed descriptor into an independently owned [`File`].
///
/// The caller retains its descriptor; cloning failures propagate to the test.
pub(crate) fn dup_as_file(fd: &OwnedFd) -> io::Result<File> {
    fd.try_clone().map(File::from)
}

/// Write every byte of `payload` through a duplicated descriptor.
///
/// This preserves failures from both `dup(2)` and [`Write::write_all`] for the
/// test body to assert on.
pub(crate) fn write_all_to(fd: &OwnedFd, payload: &[u8]) -> io::Result<()> {
    let mut file = dup_as_file(fd)?;
    file.write_all(payload)
}

/// Read to EOF through a duplicated descriptor.
///
/// This preserves failures from both `dup(2)` and [`Read::read_to_end`] for
/// the test body to assert on.
pub(crate) fn read_all_from(fd: &OwnedFd) -> io::Result<Vec<u8>> {
    let mut collected = Vec::new();
    let mut file = dup_as_file(fd)?;
    file.read_to_end(&mut collected)?;
    Ok(collected)
}

/// Extract a successful fixture result at a recognized test boundary.
pub(crate) fn unwrap_ok<T, E: Debug>(result: Result<T, E>) -> T {
    match result {
        Ok(value) => value,
        Err(err) => panic!("expected Ok(..), got Err({err:?})"),
    }
}

/// Extract an expected failure while retaining the successful value in panic
/// output.
pub(crate) fn unwrap_err<T: Debug, E>(result: Result<T, E>) -> E {
    match result {
        Ok(value) => panic!("expected Err(..), got Ok({value:?})"),
        Err(err) => err,
    }
}

#[cfg(test)]
#[path = "test_support_tests.rs"]
mod tests;
