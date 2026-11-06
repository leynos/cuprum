//! Exercise public Windows stream entry points with known synchronous pipes.

use std::{
    io,
    sync::mpsc::{self, RecvTimeoutError},
    thread,
    time::Duration,
};

use cuprum_native_io::{SynchronousOwnedStream, read_once, synchronous_pipe, write_once};

use crate::{BufferSize, consume_stream, pump_stream};

/// How long a pump may take to finish once its source writer has closed.
///
/// The transfer is local and in-process, so a correct pump completes in
/// microseconds. The margin exists only so a lost EOF reports as a failure
/// rather than waiting out the job's own timeout.
const PUMP_DEADLINE: Duration = Duration::from_secs(10);

/// How long a pump with a live source writer is observed before its completion
/// is accepted as correctly withheld.
///
/// A pump that treated the writer's arrival, rather than its closure, as the
/// end of input would report through the channel almost immediately, so this
/// window only has to outlast the payload's own read.
const RETAINED_WRITER_PROBE: Duration = Duration::from_millis(500);

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

/// A broken Windows writer is non-fatal and the pump still drains the source.
///
/// This pins two Windows-specific assumptions the Unix regression cannot: that
/// `WriteFile` against a pipe whose reader has closed reports an error whose
/// `ErrorKind` is `BrokenPipe` rather than a fatal condition, and that the
/// winning drain arm then reads the source to EOF. A wrong error kind would
/// surface here as a failed pump instead of a zero-byte success.
#[test]
fn public_pump_drains_the_reader_after_the_writer_breaks() {
    let (source_reader, source_writer) =
        synchronous_pipe().expect("source synchronous pipe creation should succeed");
    let (sink_reader, sink_writer) =
        synchronous_pipe().expect("sink synchronous pipe creation should succeed");

    let payload = b"payload-written-after-the-downstream-hangs-up";
    let written = write_once(source_writer.as_synchronous_borrowed(), payload)
        .expect("source write should succeed");
    assert_eq!(
        usize::try_from(written).expect("write count should fit usize"),
        payload.len(),
        "source pipe must accept the complete payload",
    );
    // Close the source writer so the pump's read loop can reach EOF.
    drop(source_writer);
    // Close the sink reader so every pump write fails with a broken pipe.
    drop(sink_reader);

    let transferred = pump_stream(
        source_reader.as_synchronous_borrowed(),
        sink_writer,
        buffer_size(8).expect("buffer size should be valid"),
    )
    .expect("a broken downstream pipe must not fail the pump");

    assert_eq!(
        transferred, 0,
        "no bytes reach a downstream that hung up before the first write",
    );

    // A pump that drained the source leaves nothing behind; one that abandoned
    // it mid-read leaves the payload for this read to find.
    let remaining = read_to_eof(&source_reader).expect("drained source read should succeed");
    assert!(
        remaining.is_empty(),
        "the pump must drain the upstream reader to EOF after the writer breaks, found {} bytes",
        remaining.len(),
    );
}

/// A source writer the pump's caller still owns keeps the pump waiting for EOF.
///
/// `pump_stream` reads until the source reports end of input, and on Windows a
/// synchronous `ReadFile` against a pipe reports that only once every write
/// handle has closed. `SynchronousOwnedStream` is not `Clone`, so the caller
/// can hand the pump nothing but the reader while continuing to hold the
/// original writer — exactly the arrangement this test builds. The pump must
/// therefore stay blocked, not treat the writer outliving its own call as end
/// of input, and must still finish normally when that writer finally closes.
///
/// Both halves matter. Until the writer closes the destination must hold no
/// bytes, so a pump that returned early would be caught with a complete
/// transfer rather than a truncated one. After the close it must exit on its
/// own; the deadline turns a pump that never resumes into a failure instead of
/// a hung suite.
#[test]
fn public_pump_waits_for_a_retained_source_writer_to_close() {
    let (source_reader, source_writer) =
        synchronous_pipe().expect("source synchronous pipe creation should succeed");
    let (destination_reader, destination_writer) =
        synchronous_pipe().expect("destination synchronous pipe creation should succeed");

    let payload = b"payload-a-retained-writer-keeps-open";
    let written = write_once(source_writer.as_synchronous_borrowed(), payload)
        .expect("source write should succeed");
    assert_eq!(
        usize::try_from(written).expect("write count should fit usize"),
        payload.len(),
        "source pipe must accept the complete payload",
    );

    let (sender, receiver) = mpsc::channel();
    let deadline = PUMP_DEADLINE;
    // The pump runs on its own thread so a missed EOF cannot wedge the test.
    // The source reader moves with it: the pump needs a borrow of the owner,
    // and the owner has to outlive the pump rather than the test thread.
    thread::spawn(move || {
        let reader = source_reader.as_synchronous_borrowed();
        let result = pump_stream(
            reader,
            destination_writer,
            buffer_size(5).expect("buffer size should be valid"),
        );
        // A failed send means the test already gave up on this worker.
        drop(sender.send(result));
    });

    assert!(
        matches!(
            receiver.recv_timeout(RETAINED_WRITER_PROBE),
            Err(RecvTimeoutError::Timeout),
        ),
        "the pump must not finish while the caller still owns a source writer",
    );

    drop(source_writer);

    let transferred = match receiver.recv_timeout(deadline) {
        Ok(Ok(bytes)) => bytes,
        Ok(Err(error)) => panic!("pump failed after the retained writer closed: {error:?}"),
        Err(RecvTimeoutError::Timeout) => {
            panic!("pump did not finish within {deadline:?} after the retained writer closed")
        }
        Err(RecvTimeoutError::Disconnected) => {
            panic!("pump worker dropped its sender without a result")
        }
    };
    let drained = read_to_eof(&destination_reader).expect("destination pipe read should succeed");

    assert_eq!(
        transferred,
        u64::try_from(payload.len()).expect("payload length should fit u64"),
        "the pump must report the bytes it transferred once the writer closes",
    );
    assert_eq!(
        drained.as_slice(),
        payload,
        "the destination must receive the whole payload once the writer closes",
    );
}
