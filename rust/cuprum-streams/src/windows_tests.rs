//! Exercise public Windows stream entry points with known synchronous pipes.

use cuprum_native_io::{SynchronousOwnedStream, read_once, synchronous_pipe, write_once};

use crate::{BufferSize, consume_stream, pump_stream};

fn buffer_size(bytes: i64) -> BufferSize {
    BufferSize::new(bytes).expect("test buffer size must be valid")
}

fn read_to_eof(reader: &SynchronousOwnedStream) -> Vec<u8> {
    let mut output = Vec::new();
    let mut buffer = [0; 7];

    loop {
        let read_count = read_once(reader.as_synchronous_borrowed(), &mut buffer)
            .expect("synchronous pipe read must succeed");
        let read_count = usize::try_from(read_count).expect("read count must be non-negative");
        if read_count == 0 {
            break;
        }
        let chunk = buffer
            .get(..read_count)
            .expect("pipe read count must fit its buffer");
        output.extend_from_slice(chunk);
    }

    output
}

#[test]
fn public_pump_stream_transfers_synchronous_pipe_bytes() {
    let (source_reader, source_writer) = synchronous_pipe().expect("create source pipe");
    let (destination_reader, destination_writer) =
        synchronous_pipe().expect("create destination pipe");
    let payload = b"synchronous transfer";

    let written =
        write_once(source_writer.as_synchronous_borrowed(), payload).expect("write source payload");
    assert_eq!(
        usize::try_from(written).expect("write count must be non-negative"),
        payload.len(),
        "source pipe must accept the complete payload",
    );
    drop(source_writer);

    let transferred = pump_stream(
        source_reader.as_synchronous_borrowed(),
        destination_writer,
        buffer_size(5),
    )
    .expect("public Windows pump must complete");
    let received = read_to_eof(&destination_reader);

    assert_eq!(
        transferred,
        u64::try_from(payload.len()).expect("payload length fits u64"),
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
    let (reader, writer) = synchronous_pipe().expect("create consume pipe");
    let payload = "synchronous naïve".as_bytes();

    let written =
        write_once(writer.as_synchronous_borrowed(), payload).expect("write UTF-8 payload");
    assert_eq!(
        usize::try_from(written).expect("write count must be non-negative"),
        payload.len(),
        "consume pipe must accept the complete payload",
    );
    drop(writer);

    let output = consume_stream(reader.as_synchronous_borrowed(), buffer_size(3))
        .expect("public Windows consume must reach EOF and decode");

    assert_eq!(
        output, "synchronous naïve",
        "consume must decode the full UTF-8 stream",
    );
}
