//! Isolated execution of every expected scenario.
//!
//! A scenario that is declared, bound, and listed can still execute nothing if
//! its steps are never matched, and libtest reports that as a pass. The only
//! evidence that distinguishes the two is the child's own result line, so each
//! scenario is re-run with `--exact` and its output is classified here. The
//! child also receives the isolation marker, which enables the raw descriptor
//! liveness probe that this shared binary cannot make meaningful.

use std::{
    collections::{BTreeMap, BTreeSet},
    env,
    io::Read,
    path::Path,
    process::{Child, Command, ExitStatus, Stdio},
    thread,
    time::{Duration, Instant},
};

use super::super::{
    ScenarioExpectation,
    support::{ISOLATED_CHILD_ENV, WRITER_PROBE_WITNESS},
};

/// Filter that selects the generated scenario tests in the child's listing.
const TEST_FILTER: &str = "unix_tests::";

/// Prefix of the guard's own tests, which are not scenarios.
const GUARD_MODULE: &str = "unix_tests::scenario_guard::";

/// Marker that identifies a unit-test module rather than a scenario.
///
/// The guard polices scenario-shaped tests under [`TEST_FILTER`], but ordinary
/// unit tests for this suite's own helpers live under the same prefix. Those
/// are always in a nested `tests` module, which a generated scenario name never
/// is, so that segment is what separates the two.
const UNIT_TEST_MODULE: &str = "::tests::";

/// How long the child's test listing may take.
const LIST_DEADLINE: Duration = Duration::from_secs(60);

/// How long one isolated scenario may take before it is killed and reaped.
///
/// A scenario is a handful of in-process pipe operations, so a healthy child
/// finishes in well under a second; the bound exists so a regression fails
/// this guard instead of stalling the binary until a job timeout notices.
const CHILD_DEADLINE: Duration = Duration::from_secs(60);

/// How much of either child stream is retained for diagnostics.
const RETAINED_BYTES: usize = 64 * 1024;

/// How long the poller waits between exit checks.
const POLL_INTERVAL: Duration = Duration::from_millis(25);

/// Resolve one generated test name for every expected binding function.
fn resolve(
    listed: &[String],
    expected: &[ScenarioExpectation],
) -> Result<BTreeMap<&'static str, String>, String> {
    let mut resolved = BTreeMap::new();
    let mut problems = Vec::new();

    for scenario in expected {
        let suffix = format!("::{}", scenario.binding);
        let matches: Vec<&String> = listed
            .iter()
            .filter(|name| name.ends_with(&suffix))
            .collect();
        match matches.as_slice() {
            [only] => {
                resolved.insert(scenario.binding, (*only).clone());
            }
            [] => problems.push(format!(
                "no generated test ends with {suffix:?} for scenario {:?}",
                scenario.title,
            )),
            _ => problems.push(format!(
                "{} generated tests end with {suffix:?}; one binding must resolve to one test",
                matches.len(),
            )),
        }
    }

    if problems.is_empty() {
        Ok(resolved)
    } else {
        Err(problems.join("; "))
    }
}

/// One child run's report, as the classifier reads it.
struct ChildReport<'a> {
    /// The generated test name the child was told to run.
    test_name: &'a str,
    /// Whether the child exited successfully.
    succeeded: bool,
    /// The child's retained standard output.
    stdout: &'a str,
    /// The child's retained standard error, which carries the witness.
    stderr: &'a str,
    /// Whether this scenario is required to print the writer-probe witness.
    requires_witness: bool,
}

/// Classify a child's own report as evidence that its scenario executed.
///
/// The witness is looked for on `stderr` because libtest brackets the test
/// body with the two halves of its result line: with `--nocapture`, a body that
/// printed to `stdout` would split `test <name> ... ok` and defeat the check.
fn classify(report: &ChildReport<'_>) -> Result<(), String> {
    let ChildReport {
        test_name,
        succeeded,
        stdout,
        stderr,
        requires_witness,
    } = *report;
    if !succeeded {
        return Err("the isolated child exited unsuccessfully".to_owned());
    }
    if !stdout.contains("running 1 test") {
        return Err("the isolated child did not report exactly one running test".to_owned());
    }
    let completed = format!("test {test_name} ... ok");
    if !stdout.contains(&completed) {
        return Err(format!("the isolated child did not report {completed:?}"));
    }
    if !stdout.contains("1 passed") {
        return Err("the isolated child did not report `1 passed`".to_owned());
    }
    if requires_witness && !stderr.contains(WRITER_PROBE_WITNESS) {
        return Err("the isolated child did not print the writer-probe witness".to_owned());
    }
    Ok(())
}

/// List the generated names the test binary reports under [`TEST_FILTER`].
fn list_tests(executable: &Path) -> Result<Vec<String>, String> {
    let mut command = Command::new(executable);
    command.args(["--list", TEST_FILTER]);
    let captured = run_bounded(command, LIST_DEADLINE)?;
    if captured.timed_out {
        return Err(format!(
            "listing the test names did not finish within {LIST_DEADLINE:?}"
        ));
    }
    if !captured.status.success() {
        return Err(format!(
            "listing the test names failed with {}; stderr={}",
            render_status(captured.status),
            captured.stderr,
        ));
    }
    Ok(captured
        .stdout
        .lines()
        .filter_map(|line| line.strip_suffix(": test"))
        .map(str::to_owned)
        .collect())
}

/// Run one scenario alone with the isolation marker and require its evidence.
fn run_isolated(executable: &Path, test_name: &str, requires_witness: bool) -> Result<(), String> {
    let mut command = Command::new(executable);
    command
        .args(["--exact", test_name, "--test-threads=1", "--nocapture"])
        .env(ISOLATED_CHILD_ENV, "1");
    let captured = run_bounded(command, CHILD_DEADLINE)?;
    if captured.timed_out {
        return Err(format!(
            "did not finish within {CHILD_DEADLINE:?} and was killed"
        ));
    }
    let report = ChildReport {
        test_name,
        succeeded: captured.status.success(),
        stdout: &captured.stdout,
        stderr: &captured.stderr,
        requires_witness,
    };
    classify(&report).map_err(|reason| {
        format!(
            "{reason}; status={}; stdout={}; stderr={}",
            render_status(captured.status),
            captured.stdout,
            captured.stderr,
        )
    })
}

/// Run every expected scenario in isolation, reporting every failure at once.
pub(super) fn execute_every_scenario(expected: &[ScenarioExpectation]) -> Result<(), String> {
    let executable = env::current_exe()
        .map_err(|error| format!("the test executable path is unavailable: {error}"))?;
    let listed = list_tests(&executable)?;
    let resolved = resolve(&listed, expected)?;

    reject_unclaimed(&listed, &resolved)?;

    let mut tally = Tally::default();
    for scenario in expected {
        let Some(test_name) = resolved.get(scenario.binding) else {
            tally.failures.push(format!(
                "{:?} was not resolved to a generated test",
                scenario.title,
            ));
            continue;
        };
        let attempt = Attempt {
            scenario,
            test_name,
        };
        record_outcome(&executable, &attempt, &mut tally);
    }

    if !tally.failures.is_empty() {
        return Err(tally.failures.join("; "));
    }
    if tally.executed != expected.len() {
        return Err(format!(
            "{} of {} declared scenarios executed successfully",
            tally.executed,
            expected.len(),
        ));
    }
    Ok(())
}

/// Fail when a scenario-shaped test exists that no expectation claims.
///
/// The guard's own tests are excluded by module prefix: they are ordinary unit
/// tests for this guard, not scenarios.
fn reject_unclaimed(
    listed: &[String],
    resolved: &BTreeMap<&'static str, String>,
) -> Result<(), String> {
    let claimed: BTreeSet<&str> = resolved.values().map(String::as_str).collect();
    let unexpected: Vec<&str> = listed
        .iter()
        .map(String::as_str)
        .filter(|name| name.starts_with(TEST_FILTER))
        .filter(|name| !name.starts_with(GUARD_MODULE))
        .filter(|name| !name.contains(UNIT_TEST_MODULE))
        .filter(|name| !claimed.contains(name))
        .collect();
    if unexpected.is_empty() {
        return Ok(());
    }
    Err(format!(
        "unexpected tests under {TEST_FILTER:?} that no scenario claims: {unexpected:?}"
    ))
}

/// Run one scenario and file its pass or failure by title.
fn record_outcome(executable: &Path, attempt: &Attempt<'_>, tally: &mut Tally) {
    let Attempt {
        scenario,
        test_name,
    } = *attempt;
    match run_isolated(executable, test_name, scenario.requires_writer_probe) {
        Ok(()) => tally.executed = tally.executed.saturating_add(1),
        Err(reason) => tally
            .failures
            .push(format!("{:?} ({test_name}): {reason}", scenario.title)),
    }
}

/// One scenario resolved to the generated test that must run it.
struct Attempt<'a> {
    scenario: &'a ScenarioExpectation,
    test_name: &'a str,
}

/// Running totals for the isolated execution guard.
#[derive(Default)]
struct Tally {
    executed: usize,
    failures: Vec<String>,
}

/// A child's exit status and bounded output.
#[derive(Debug)]
struct CapturedChild {
    status: ExitStatus,
    stdout: String,
    stderr: String,
    timed_out: bool,
}

/// Spawn `command` with piped output, polling to a deadline that kills it.
fn run_bounded(mut command: Command, deadline: Duration) -> Result<CapturedChild, String> {
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
        stdout: stdout.join().unwrap_or_default(),
        stderr: stderr.join().unwrap_or_default(),
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
fn drain(mut pipe: impl Read + Send + 'static) -> thread::JoinHandle<String> {
    thread::spawn(move || {
        let mut retained = Vec::new();
        let mut chunk = [0_u8; 4096];
        while let Ok(count) = pipe.read(&mut chunk) {
            if count == 0 {
                break;
            }
            let room = RETAINED_BYTES.saturating_sub(retained.len());
            let keep = room.min(count);
            if let Some(prefix) = chunk.get(..keep) {
                retained.extend_from_slice(prefix);
            }
        }
        String::from_utf8_lossy(&retained).into_owned()
    })
}

/// Render an exit status for a diagnostic without assuming a code exists.
fn render_status(status: ExitStatus) -> String {
    status
        .code()
        .map_or_else(|| format!("{status:?}"), |code| format!("exit code {code}"))
}

#[cfg(test)]
mod tests;
