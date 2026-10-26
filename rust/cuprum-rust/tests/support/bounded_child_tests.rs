//! Exercise bounded child I/O, diagnostics, deadlines, and cleanup.

use std::{
    env,
    io::{self, Read, Write},
    process::Command,
    thread,
    time::Duration,
};

use rstest::{fixture, rstest};

use super::{CHILD_MODE_ENV, CHILD_TEST_NAME, ChildFailure, DIAGNOSTIC_STREAM_LIMIT, run_child};

#[fixture]
fn child_timeout(#[default(5_000)] milliseconds: u64) -> Duration {
    let timeout = Duration::from_millis(milliseconds);
    assert!(!timeout.is_zero(), "child timeout budget must be positive");
    timeout
}

#[derive(Clone, Copy)]
enum ChildFixture {
    Copy,
    Mismatch,
    EarlyExit,
    Count,
    NeverRead,
    Flood,
    HangAfterInput,
}

impl ChildFixture {
    fn mode(self) -> &'static str {
        match self {
            Self::Copy => "copy",
            Self::Mismatch => "mismatch",
            Self::EarlyExit => "early-exit",
            Self::Count => "count",
            Self::NeverRead => "never-read",
            Self::Flood => "flood",
            Self::HangAfterInput => "hang-after-input",
        }
    }
}

fn fixture_command(fixture: ChildFixture) -> Command {
    let executable = env::current_exe().expect("test executable path is available");
    let mut command = Command::new(executable);
    command
        .args(["--exact", CHILD_TEST_NAME, "--nocapture"])
        .env(CHILD_MODE_ENV, fixture.mode());
    command
}

#[expect(
    clippy::disallowed_methods,
    reason = "The child fixture reads the mode explicitly supplied by its parent test"
)]
fn child_fixture_mode() -> Option<String> {
    env::var_os(CHILD_MODE_ENV).and_then(|mode| mode.into_string().ok())
}

fn read_child_input() -> io::Result<Vec<u8>> {
    let mut input = Vec::new();
    io::stdin().read_to_end(&mut input)?;
    Ok(input)
}

#[rstest]
fn child_fixture() -> io::Result<()> {
    child_fixture_mode().map_or(Ok(()), |mode| run_child_fixture(&mode))
}

fn run_child_fixture(mode: &str) -> io::Result<()> {
    match mode {
        "copy" => copy_child_input(),
        "mismatch" => report_child_mismatch(),
        "early-exit" => std::process::exit(17),
        "count" => report_child_input_count(),
        "never-read" => {
            wait_without_reading();
            Ok(())
        }
        "flood" => flood_child_diagnostics(),
        "hang-after-input" => wait_after_reading(),
        _ => Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("unknown child fixture mode: {mode}"),
        )),
    }
}

fn copy_child_input() -> io::Result<()> {
    let mut input = io::stdin().lock();
    let mut output = io::stdout().lock();
    io::copy(&mut input, &mut output)?;
    output.flush()
}

fn report_child_mismatch() -> io::Result<()> {
    let input = read_child_input()?;
    let mut stderr = io::stderr().lock();
    writeln!(stderr, "injected mismatch for {} bytes", input.len())?;
    stderr.flush()?;
    std::process::exit(23)
}

fn report_child_input_count() -> io::Result<()> {
    let input = read_child_input()?;
    let mut stdout = io::stdout().lock();
    writeln!(stdout, "received={}", input.len())?;
    stdout.flush()
}

fn wait_without_reading() { thread::sleep(Duration::from_secs(60)); }

fn flood_child_diagnostics() -> io::Result<()> {
    let mut stdout = io::stdout().lock();
    let mut stderr = io::stderr().lock();
    let block = [b'x'; 4096];
    for _ in 0..32 {
        stdout.write_all(&block)?;
        stderr.write_all(&block)?;
    }
    Ok(())
}

fn wait_after_reading() -> io::Result<()> {
    let input = read_child_input()?;
    let mut stdout = io::stdout().lock();
    writeln!(stdout, "received={}", input.len())?;
    stdout.flush()?;
    thread::sleep(Duration::from_secs(60));
    Ok(())
}

#[rstest]
fn transfers_input_and_drains_output_concurrently(#[with(5_000)] child_timeout: Duration) {
    let payload = b"\0independent child input\xff";
    let output = run_child(
        &mut fixture_command(ChildFixture::Copy),
        payload,
        child_timeout,
    )
    .expect("the echo child should finish");

    assert!(output.status.success(), "child should exit successfully");
    assert!(
        output
            .stdout
            .windows(payload.len())
            .any(|window| window == payload),
        "child stdout should contain the exact binary input"
    );
    assert!(output.stdin_error.is_none(), "input should reach child EOF");
}

#[rstest]
fn reports_a_child_mismatch_without_losing_its_diagnostic(#[with(5_000)] child_timeout: Duration) {
    let output = run_child(
        &mut fixture_command(ChildFixture::Mismatch),
        b"payload",
        child_timeout,
    )
    .expect("a nonzero child exit is still a captured result");

    assert_eq!(
        output.status.code(),
        Some(23),
        "child status should survive"
    );
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("injected mismatch"),
        "bounded stderr should retain the mismatch reason"
    );
}

#[rstest]
fn reports_an_early_child_exit_and_reaps_it(#[with(5_000)] child_timeout: Duration) {
    let output = run_child(
        &mut fixture_command(ChildFixture::EarlyExit),
        b"payload",
        child_timeout,
    )
    .expect("an early exit should return a status");

    assert_eq!(
        output.status.code(),
        Some(17),
        "early status should survive"
    );
}

#[rstest]
fn sends_large_input_while_the_child_drains_it(#[with(5_000)] child_timeout: Duration) {
    let payload = vec![b'z'; 2 * 1024 * 1024];
    let output = run_child(
        &mut fixture_command(ChildFixture::Count),
        &payload,
        child_timeout,
    )
    .expect("the child should drain input larger than a pipe buffer");

    assert!(output.status.success(), "large-input child should succeed");
    let child_stdout = String::from_utf8_lossy(&output.stdout);
    let reported_input_count = child_stdout
        .lines()
        .find(|line| line.starts_with("received="))
        .expect("the child should report its received byte count");
    let expected_input_count = format!("received={}", payload.len());
    assert_eq!(
        reported_input_count,
        expected_input_count.as_str(),
        "child should receive every input byte"
    );
}

#[rstest]
fn times_out_while_the_child_is_not_reading_stdin(#[with(250)] child_timeout: Duration) {
    let payload = vec![b'x'; 8 * 1024 * 1024];
    let failure = run_child(
        &mut fixture_command(ChildFixture::NeverRead),
        &payload,
        child_timeout,
    )
    .expect_err("a child blocked before reading input must time out");

    match &failure {
        ChildFailure::TimedOut { stdin_error, .. } => assert!(
            stdin_error.is_some(),
            "killing the non-reading child should unblock its input writer"
        ),
        _ => panic!("timeout should kill and reap the owned child, got {failure:?}"),
    }
}

#[rstest]
fn continues_draining_after_diagnostic_storage_is_full(#[with(5_000)] child_timeout: Duration) {
    let output = run_child(
        &mut fixture_command(ChildFixture::Flood),
        b"",
        child_timeout,
    )
    .expect("the child should finish while output is drained");

    assert!(output.status.success(), "flooding child should succeed");
    assert!(output.stdout.len() <= DIAGNOSTIC_STREAM_LIMIT);
    assert!(output.stderr.len() <= DIAGNOSTIC_STREAM_LIMIT);
}

#[rstest]
fn times_out_after_input_when_the_child_stops_responding(#[with(250)] child_timeout: Duration) {
    let failure = run_child(
        &mut fixture_command(ChildFixture::HangAfterInput),
        b"complete input",
        child_timeout,
    )
    .expect_err("a child that hangs after EOF must time out");

    assert!(
        matches!(&failure, ChildFailure::TimedOut { .. }),
        "timeout should also cover post-input work, got {failure:?}"
    );
}
