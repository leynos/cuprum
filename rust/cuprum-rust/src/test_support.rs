//! Shared Unix file-descriptor fixtures and assertions for Rust unit tests.

use std::fmt::Debug;
use std::fs::File;
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};

pub(crate) fn make_pipe() -> (OwnedFd, OwnedFd) {
    let mut fds = [0_i32; 2];
    // SAFETY: `fds` is a valid two-element array for `pipe(2)` to fill.
    let rc = unsafe { libc::pipe(fds.as_mut_ptr()) };
    assert_eq!(rc, 0, "pipe(2) failed: {}", io::Error::last_os_error());
    // SAFETY: on success `pipe(2)` returned two freshly opened descriptors
    // that this process exclusively owns.
    unsafe { (OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1])) }
}

pub(crate) fn dup_as_file(fd: &OwnedFd) -> File {
    // SAFETY: duplicating an owned descriptor for a scoped `File` wrapper.
    let duplicated_fd = unsafe { libc::dup(fd.as_raw_fd()) };
    assert_ne!(
        duplicated_fd,
        -1,
        "dup(2) failed: {}",
        io::Error::last_os_error(),
    );
    // SAFETY: `duplicated_fd` was checked for `dup(2)` failure above and is
    // now owned by this scoped `File`.
    unsafe { File::from_raw_fd(duplicated_fd) }
}

pub(crate) fn write_all_to(fd: &OwnedFd, payload: &[u8]) {
    unwrap_ok(dup_as_file(fd).write_all(payload));
}

pub(crate) fn read_all_from(fd: &OwnedFd) -> Vec<u8> {
    let mut collected = Vec::new();
    unwrap_ok(dup_as_file(fd).read_to_end(&mut collected));
    collected
}

pub(crate) fn unwrap_ok<T, E: Debug>(result: Result<T, E>) -> T {
    match result {
        Ok(value) => value,
        Err(err) => panic!("expected Ok(..), got Err({err:?})"),
    }
}

pub(crate) fn unwrap_err<T: Debug, E>(result: Result<T, E>) -> E {
    match result {
        Ok(value) => panic!("expected Err(..), got Ok({value:?})"),
        Err(err) => err,
    }
}

pub(crate) fn fd_is_open(fd: i32) -> bool {
    loop {
        // SAFETY: F_GETFD on an arbitrary integer reports EBADF for a closed
        // descriptor without dereferencing memory.
        let result = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        if result != -1 {
            return true;
        }

        if io::Error::last_os_error().raw_os_error() != Some(libc::EINTR) {
            return false;
        }
    }
}
