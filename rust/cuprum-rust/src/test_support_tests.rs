//! Error-path coverage for shared Unix file-descriptor test helpers.

use std::io;

use super::{dup_as_file, make_pipe, read_all_from, unwrap_err, unwrap_ok, write_all_to};

/// Assert that a descriptor operation preserves its native `EBADF` failure.
///
/// This remains local to the test-support error contract rather than becoming
/// a general assertion framework.
#[track_caller]
fn assert_bad_file_descriptor(error: &io::Error, operation: &str) {
    assert_eq!(
        error.raw_os_error(),
        Some(libc::EBADF),
        "{operation} must preserve the invalid-descriptor error",
    );
}

#[test]
fn dup_as_file_returns_an_error_for_an_invalid_raw_descriptor() {
    let error = unwrap_err(dup_as_file(&-1));

    assert_bad_file_descriptor(&error, "dup(2)");
}

#[test]
fn write_all_to_returns_an_error_for_a_pipe_read_end() {
    let (read_end, _write_end) = unwrap_ok(make_pipe());
    let error = unwrap_err(write_all_to(&read_end, b"cannot write here"));

    assert_bad_file_descriptor(&error, "write_all");
}

#[test]
fn read_all_from_returns_an_error_for_a_pipe_write_end() {
    let (_read_end, write_end) = unwrap_ok(make_pipe());
    let error = unwrap_err(read_all_from(&write_end));

    assert_bad_file_descriptor(&error, "read_to_end");
}
