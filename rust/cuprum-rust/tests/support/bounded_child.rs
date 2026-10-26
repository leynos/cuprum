//! Run test children with bounded time, diagnostics, and owned process cleanup.

use std::{
    fmt,
    io::{self, Read, Write},
    process::{Child, Command, ExitStatus, Stdio},
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

const DIAGNOSTIC_STREAM_LIMIT: usize = 8 * 1024;
const CHILD_MODE_ENV: &str = "CUPRUM_INTERNAL_BOUNDARY_CHILD_MODE";
const CHILD_TEST_NAME: &str = "bounded_child::tests::child_fixture";

/// Output and status collected from one child process.
#[derive(Debug)]
pub(crate) struct ChildOutput {
    pub(crate) status: ExitStatus,
    pub(crate) stdout: Vec<u8>,
    pub(crate) stderr: Vec<u8>,
    pub(crate) stdin_error: Option<String>,
}

/// Failures that prevent a bounded child run from producing a result.
#[derive(Debug)]
pub(crate) enum ChildFailure {
    Spawn(io::Error),
    MissingPipe(&'static str),
    Kill(io::Error),
    Wait(io::Error),
    Worker {
        name: &'static str,
        reason: String,
    },
    TimedOut {
        status: Option<i32>,
        stdout: String,
        stderr: String,
        stdin_error: Option<String>,
    },
}

impl fmt::Display for ChildFailure {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Spawn(error) => write!(formatter, "could not spawn child: {error}"),
            Self::MissingPipe(name) => write!(formatter, "child {name} pipe was unavailable"),
            Self::Kill(error) => write!(formatter, "could not kill child: {error}"),
            Self::Wait(error) => write!(formatter, "could not wait for child: {error}"),
            Self::Worker { name, reason } => {
                write!(formatter, "child {name} worker failed: {reason}")
            }
            Self::TimedOut {
                status,
                stdout,
                stderr,
                stdin_error,
            } => write!(
                formatter,
                "child timed out (status={status:?}, stdin_error={stdin_error:?}); \
                 stdout={stdout:?}; stderr={stderr:?}"
            ),
        }
    }
}

struct ChildPipes {
    stdin: std::process::ChildStdin,
    stdout: std::process::ChildStdout,
    stderr: std::process::ChildStderr,
}

struct ChildWorkers {
    stdin: JoinHandle<io::Result<()>>,
    stdout: JoinHandle<io::Result<Vec<u8>>>,
    stderr: JoinHandle<io::Result<Vec<u8>>>,
}

struct ChildStreams {
    stdin_error: Option<String>,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
}

enum ChildCompletion {
    Exited(ExitStatus),
    TimedOut(ExitStatus),
}

/// Run a child while input transfer and bounded output drains proceed together.
pub(crate) fn run_child(
    command: &mut Command,
    input: &[u8],
    timeout: Duration,
) -> Result<ChildOutput, ChildFailure> {
    let deadline = Instant::now() + timeout;
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(ChildFailure::Spawn)?;

    let pipes = take_child_pipes(&mut child)?;
    let workers = start_workers(pipes, input);
    let completion = wait_until_deadline(&mut child, deadline)?;
    let streams = join_workers(workers)?;

    match completion {
        ChildCompletion::Exited(status) => Ok(ChildOutput {
            status,
            stdout: streams.stdout,
            stderr: streams.stderr,
            stdin_error: streams.stdin_error,
        }),
        ChildCompletion::TimedOut(status) => Err(ChildFailure::TimedOut {
            status: status.code(),
            stdout: String::from_utf8_lossy(&streams.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&streams.stderr).into_owned(),
            stdin_error: streams.stdin_error,
        }),
    }
}

fn take_child_pipes(child: &mut Child) -> Result<ChildPipes, ChildFailure> {
    let Some(stdin) = child.stdin.take() else {
        kill_and_reap(child)?;
        return Err(ChildFailure::MissingPipe("stdin"));
    };
    let Some(stdout) = child.stdout.take() else {
        kill_and_reap(child)?;
        return Err(ChildFailure::MissingPipe("stdout"));
    };
    let Some(stderr) = child.stderr.take() else {
        kill_and_reap(child)?;
        return Err(ChildFailure::MissingPipe("stderr"));
    };
    Ok(ChildPipes {
        stdin,
        stdout,
        stderr,
    })
}

fn start_workers(pipes: ChildPipes, input: &[u8]) -> ChildWorkers {
    let input_bytes = input.to_vec();
    let stdin = thread::spawn(move || write_input(pipes.stdin, &input_bytes));
    let stdout = thread::spawn(move || drain_capped(pipes.stdout));
    let stderr = thread::spawn(move || drain_capped(pipes.stderr));
    ChildWorkers {
        stdin,
        stdout,
        stderr,
    }
}

fn join_workers(workers: ChildWorkers) -> Result<ChildStreams, ChildFailure> {
    let stdin_result = join_worker(workers.stdin, "stdin")?;
    let stdout_bytes =
        join_worker(workers.stdout, "stdout")?.map_err(|error| worker_error("stdout", &error))?;
    let stderr_bytes =
        join_worker(workers.stderr, "stderr")?.map_err(|error| worker_error("stderr", &error))?;
    Ok(ChildStreams {
        stdin_error: stdin_result.err().map(|error| error.to_string()),
        stdout: stdout_bytes,
        stderr: stderr_bytes,
    })
}

fn write_input(mut stdin: std::process::ChildStdin, input: &[u8]) -> io::Result<()> {
    stdin.write_all(input)
}

fn drain_capped(mut reader: impl Read) -> io::Result<Vec<u8>> {
    let mut retained = Vec::with_capacity(DIAGNOSTIC_STREAM_LIMIT);
    let mut chunk = [0_u8; 4096];
    loop {
        let read = reader.read(&mut chunk)?;
        if read == 0 {
            return Ok(retained);
        }
        let available = DIAGNOSTIC_STREAM_LIMIT.saturating_sub(retained.len());
        retained.extend(chunk.iter().take(read.min(available)).copied());
    }
}

fn wait_until_deadline(
    child: &mut Child,
    deadline: Instant,
) -> Result<ChildCompletion, ChildFailure> {
    loop {
        if Instant::now() >= deadline {
            return kill_and_reap(child).map(ChildCompletion::TimedOut);
        }
        match child.try_wait() {
            Ok(Some(status)) => return Ok(ChildCompletion::Exited(status)),
            Ok(None) => thread::sleep(
                deadline
                    .saturating_duration_since(Instant::now())
                    .min(Duration::from_millis(5)),
            ),
            Err(error) => {
                kill_and_reap(child)?;
                return Err(ChildFailure::Wait(error));
            }
        }
    }
}

fn kill_and_reap(child: &mut Child) -> Result<ExitStatus, ChildFailure> {
    if let Err(kill_error) = child.kill()
        && child.try_wait().map_err(ChildFailure::Wait)?.is_none()
    {
        return Err(ChildFailure::Kill(kill_error));
    }
    child.wait().map_err(ChildFailure::Wait)
}

fn join_worker<T>(worker: JoinHandle<T>, name: &'static str) -> Result<T, ChildFailure> {
    worker.join().map_err(|_| ChildFailure::Worker {
        name,
        reason: "thread panicked".to_owned(),
    })
}

fn worker_error(name: &'static str, error: &io::Error) -> ChildFailure {
    ChildFailure::Worker {
        name,
        reason: error.to_string(),
    }
}

#[cfg(test)]
#[path = "bounded_child_tests.rs"]
mod tests;
