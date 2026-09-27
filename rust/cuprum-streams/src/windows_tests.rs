//! Exercise public Windows stream entry points with known synchronous pipes.

use std::io;

use cuprum_native_io::{SynchronousOwnedStream, read_once, synchronous_pipe, write_once};

use crate::{BufferSize, consume_stream, pump_stream};

fn buffer_size(bytes: i64) -> Result<BufferSize, io::Error> {
    BufferSize::new(bytes).map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))
}

fn read_to_eof(reader: &SynchronousOwnedStream) -> Result<Vec<u8>, io::Error> {
    let mut output = Vec::new();
    let mut buffer = [0; 7];

    loop {
        let read_count = read_once(reader.as_synchronous_borrowed(), &mut buffer)?;
        let byte_count = usize::try_from(read_count)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        if byte_count == 0 {
            break;
        }
        let chunk = buffer.get(..byte_count).ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "pipe read exceeded its buffer")
        })?;
        output.extend_from_slice(chunk);
    }

    Ok(output)
}

#[test]
fn public_pump_stream_transfers_synchronous_pipe_bytes() {
    let (source_reader, source_writer) =
        synchronous_pipe().expect("source synchronous pipe creation should succeed");
    let (destination_reader, destination_writer) =
        synchronous_pipe().expect("destination synchronous pipe creation should succeed");
    let payload = b"synchronous transfer";

    let written = write_once(source_writer.as_synchronous_borrowed(), payload)
        .expect("synchronous source write should succeed");
    assert_eq!(
        usize::try_from(written).expect("write count should fit usize"),
        payload.len(),
        "source pipe must accept the complete payload",
    );
    drop(source_writer);

    let transferred = pump_stream(
        source_reader.as_synchronous_borrowed(),
        destination_writer,
        buffer_size(5).expect("buffer size should be valid"),
    )
    .expect("synchronous pump should succeed");
    let received = read_to_eof(&destination_reader).expect("destination pipe read should succeed");

    assert_eq!(
        transferred,
        u64::try_from(payload.len()).expect("payload length should fit u64"),
        "pump result must count all transferred bytes",
    );
    assert_eq!(
        received.as_slice(),
        payload,
        "destination pipe must receive the source bytes",
    );
}

#[test]
fn public_consume_stream_decodes_synchronous_pipe_bytes() {
    let (reader, writer) = synchronous_pipe().expect("synchronous pipe creation should succeed");
    let payload = "synchronous naïve".as_bytes();

    let written = write_once(writer.as_synchronous_borrowed(), payload)
        .expect("synchronous pipe write should succeed");
    assert_eq!(
        usize::try_from(written).expect("write count should fit usize"),
        payload.len(),
        "consume pipe must accept the complete payload",
    );
    drop(writer);

    let output = consume_stream(
        reader.as_synchronous_borrowed(),
        buffer_size(3).expect("buffer size should be valid"),
    )
    .expect("synchronous consume should succeed");

    assert_eq!(
        output, "synchronous naïve",
        "consume must decode the full UTF-8 stream",
    );
}
