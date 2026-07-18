//! Tests for the borrowed file-descriptor ownership contract.

use std::io::Read;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::panic::{AssertUnwindSafe, catch_unwind};

use crate::test_support::{fd_is_open, make_pipe, write_all_to};
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
        with_borrowed_reader(raw_fd, |_reader| {
            panic!("simulated failure inside the borrowed scope");
        });
    }));

    assert!(outcome.is_err(), "the panic must propagate to the caller");
}

fn assert_successful_reader_keeps_fd_usable(raw_fd: i32, write_end: OwnedFd) {
    write_all_to(&write_end, b"ping");
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
