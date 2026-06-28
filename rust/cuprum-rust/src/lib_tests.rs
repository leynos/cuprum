//! Tests for the borrowed file-descriptor ownership contract.

use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::panic::{AssertUnwindSafe, catch_unwind};

use crate::with_borrowed_reader;
use rstest::{fixture, rstest};

struct BorrowedReaderPipe {
    read_end: OwnedFd,
    write_end: OwnedFd,
    raw_fd: i32,
}

enum BorrowedReaderScenario {
    Panic,
    Success,
}

fn make_pipe() -> (OwnedFd, OwnedFd) {
    let mut fds = [0_i32; 2];
    // SAFETY: `fds` is a valid two-element array for `pipe(2)` to fill.
    let rc = unsafe { libc::pipe(fds.as_mut_ptr()) };
    assert_eq!(rc, 0, "pipe(2) failed: {}", io::Error::last_os_error());
    // SAFETY: on success `pipe(2)` returned two freshly opened FDs that this
    // process exclusively owns.
    unsafe { (OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1])) }
}

fn fd_is_open(fd: i32) -> bool {
    loop {
        // SAFETY: F_GETFD on an arbitrary integer is safe; it reports EBADF
        // for descriptors that are not open.
        let result = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        if result != -1 {
            return true;
        }

        if io::Error::last_os_error().raw_os_error() != Some(libc::EINTR) {
            return false;
        }
    }
}

#[fixture]
fn borrowed_reader_pipe() -> BorrowedReaderPipe {
    let (read_end, write_end) = make_pipe();
    let raw_fd = read_end.as_raw_fd();

    BorrowedReaderPipe {
        read_end,
        write_end,
        raw_fd,
    }
}

#[rstest]
#[case::panicking_operation(BorrowedReaderScenario::Panic)]
#[case::successful_operation(BorrowedReaderScenario::Success)]
fn borrowed_reader_stays_open_after_operation(
    borrowed_reader_pipe: BorrowedReaderPipe,
    #[case] scenario: BorrowedReaderScenario,
) {
    let BorrowedReaderPipe {
        read_end,
        write_end,
        raw_fd,
    } = borrowed_reader_pipe;

    match scenario {
        BorrowedReaderScenario::Panic => assert_panicking_reader_keeps_fd_open(raw_fd),
        BorrowedReaderScenario::Success => {
            assert_successful_reader_keeps_fd_usable(raw_fd, write_end);
        }
    }

    assert!(fd_is_open(raw_fd), "the borrowed FD must remain open");
    drop(read_end);
}

fn assert_panicking_reader_keeps_fd_open(raw_fd: i32) {
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        with_borrowed_reader(raw_fd, |_reader| -> () {
            panic!("simulated failure inside the borrowed scope");
        });
    }));

    assert!(outcome.is_err(), "the panic must propagate to the caller");
}

fn assert_successful_reader_keeps_fd_usable(raw_fd: i32, write_end: OwnedFd) {
    {
        // SAFETY: duplicating an owned descriptor for a scoped writer.
        let duplicated_fd = unsafe { libc::dup(write_end.as_raw_fd()) };
        assert_ne!(
            duplicated_fd,
            -1,
            "dup(2) failed: {}",
            io::Error::last_os_error(),
        );
        // SAFETY: `duplicated_fd` was checked for `dup(2)` failure above and
        // is now owned by this scoped `File`.
        let mut writer = unsafe { std::fs::File::from_raw_fd(duplicated_fd) };
        match writer.write_all(b"ping") {
            Ok(()) => {}
            Err(err) => panic!("pipe write failed: {err}"),
        }
    }
    drop(write_end);

    let collected = with_borrowed_reader(raw_fd, |reader| {
        // SAFETY: reading through the borrowed handle's raw descriptor.
        let mut file =
            unsafe { std::mem::ManuallyDrop::new(std::fs::File::from_raw_fd(reader.as_raw_fd())) };
        let mut data = Vec::new();
        match file.read_to_end(&mut data) {
            Ok(_) => {}
            Err(err) => panic!("pipe read failed: {err}"),
        }
        data
    });

    assert_eq!(collected, b"ping");
}
