//! Pin that a live generic handle does not grant synchronous native I/O.

use std::os::windows::io::AsHandle;

use cuprum_native_io::{BorrowedStream, pipe, read_once, write_once};

fn main() {
    let (reader, writer) = pipe().expect("create generic pipe");
    let generic_reader: BorrowedStream<'_> = reader.as_handle();
    let generic_writer: BorrowedStream<'_> = writer.as_handle();
    let mut buffer = [0; 1];

    let _ = read_once(generic_reader, &mut buffer);
    let _ = write_once(generic_writer, b"x");
}
