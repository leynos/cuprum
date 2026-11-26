//! Bounded child-process execution for the isolated scenario guard.
//!
//! The guard re-runs one scenario per child, so every wait here is bounded:
//! a child that outlives its deadline is killed and reaped, and a killed
//! child's grandchildren cannot hold the report open, because the drain wait
//! is bounded too.

use std::{
    io::Read,
    process::{Child, Command, ExitStatus, Stdio},
    sync::{
        Arc,
        Mutex,
        PoisonError,
        mpsc::{self, RecvTimeoutError},
    },
    thread,
    time::{Duration, Instant},
};

/// How much of either child stream is retained for diagnostics.
pub(super) const RETAINED_BYTES: usize = 64 * 1024;

/// How long the poller waits between exit checks.
const POLL_INTERVAL: Duration = Duration::from_millis(25);

/// How long the collector waits for a drained stream after the child exits.
///
/// A killed child's grandchildren can inherit its stdout and stderr, so the
/// pipes may not reach end of file even though the child is gone. Waiting on
/// the reader thread would then block for as long as the grandchild lives, so
/// the wait is bounded and whatever was retained is reported instead.
const DRAIN_DEADLINE: Duration = Duration::from_secs(2);

/// A child's exit status and bounded output.
#[derive(Debug)]
pub(super) struct CapturedChild {
    pub(super) status: ExitStatus,
    pub(super) stdout: String,
    pub(super) stderr: String,
    pub(super) timed_out: bool,
}

/// Spawn `command` with piped output, polling to a deadline that kills it.
pub(super) fn run_bounded(
    mut command: Command,
    deadline: Duration,
) -> Result<CapturedChild, String> {
    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("the child process failed to start: {error}"))?;
    let (Some(stdout_pipe), Some(stderr_pipe)) = (child.stdout.take(), child.stderr.take()) else {
        discard(&mut child);
        return Err("the child output pipes were not captured".to_owned());
    };
    let stdout = drain(stdout_pipe);
    let stderr = drain(stderr_pipe);

    let started = Instant::now();
    let (status, timed_out) = loop {
        match child.try_wait() {
            Ok(Some(status)) => break (status, false),
            Ok(None) if started.elapsed() >= deadline => {
                let status = kill_and_reap(&mut child)?;
                break (status, true);
            }
            Ok(None) => thread::sleep(POLL_INTERVAL),
            Err(error) => {
                discard(&mut child);
                return Err(format!("waiting for the child failed: {error}"));
            }
        }
    };

    Ok(CapturedChild {
        status,
        stdout: stdout.finish(),
        stderr: stderr.finish(),
        timed_out,
    })
}

/// Kill a child and reap it, preserving either failure as an error.
fn kill_and_reap(child: &mut Child) -> Result<ExitStatus, String> {
    child
        .kill()
        .map_err(|error| format!("the timed-out child could not be killed: {error}"))?;
    child
        .wait()
        .map_err(|error| format!("the killed child could not be reaped: {error}"))
}

/// Kill and reap a child whose output could not be observed.
fn discard(child: &mut Child) {
    drop(child.kill());
    drop(child.wait());
}

/// Read a child stream on its own thread, retaining a bounded prefix.
///
/// Draining on a thread is what keeps a child that writes more than the pipe
/// buffer can hold from blocking on its own output while this process polls.
pub(super) fn drain(mut pipe: impl Read + Send + 'static) -> Drain {
    let retained = Arc::new(Mutex::new(Vec::new()));
    let sink = Arc::clone(&retained);
    let (finished, completion) = mpsc::channel();
    thread::spawn(move || {
        let mut chunk = [0_u8; 4096];
        while let Ok(count) = pipe.read(&mut chunk) {
            if count == 0 {
                break;
            }
            let mut buffer = sink.lock().unwrap_or_else(PoisonError::into_inner);
            let room = RETAINED_BYTES.saturating_sub(buffer.len());
            let keep = room.min(count);
            if let Some(prefix) = chunk.get(..keep) {
                buffer.extend_from_slice(prefix);
            }
        }
        // A failed send means the collector already gave up on this stream.
        // Reporting it is pointless: the thread is about to end either way.
        drop_ignoring_must_use(finished.send(()));
    });
    Drain {
        retained,
        completion,
    }
}

/// Discard a value whose success or failure no caller can act on.
fn drop_ignoring_must_use<T>(value: T) { std::mem::drop(value); }

/// A stream being drained, and the completion signal for its reader thread.
pub(super) struct Drain {
    /// The bounded prefix the reader has retained so far.
    retained: Arc<Mutex<Vec<u8>>>,
    /// Signalled once the reader reaches end of file.
    completion: mpsc::Receiver<()>,
}

impl Drain {
    /// Wait briefly for the reader, then return what it retained.
    ///
    /// The wait is bounded because a grandchild of a killed child can keep the
    /// pipe open indefinitely; a late reader then simply contributes nothing
    /// further to the report.
    fn finish(self) -> String {
        match self.completion.recv_timeout(DRAIN_DEADLINE) {
            Ok(()) | Err(RecvTimeoutError::Timeout | RecvTimeoutError::Disconnected) => {}
        }
        let retained = self.retained.lock().unwrap_or_else(PoisonError::into_inner);
        String::from_utf8_lossy(&retained).into_owned()
    }
}
