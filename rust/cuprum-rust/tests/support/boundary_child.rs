//! Launch the independent Python consumer and verify what it reported.
//!
//! The parity harness in `consume_boundary_parity.rs` owns the properties and
//! the cases; this module owns everything between "we have a payload" and "the
//! child ran on the interpreter Make selected". Keeping the two apart means the
//! harness file holds the contract under test, while the process plumbing —
//! command construction, the bounded wait, and the provenance checks a child
//! must pass before its verdict is trusted — lives where a reader debugging a
//! child failure will look.
//!
//! The provenance checks matter as much as the exit status. A child that exits
//! zero on a different interpreter, or against a native module staged
//! somewhere unexpected, has not compared the artefact this run built, and its
//! `PASS` line would be a false negative rather than evidence.

use std::{
    env,
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

use crate::bounded_child;

/// Environment variable Make sets to the exact project interpreter.
pub(crate) const PYTHON_ENV: &str = "CUPRUM_BOUNDARY_PYTHON";

/// How long any one parity child may run before it is killed.
pub(crate) const CHILD_TIMEOUT: Duration = Duration::from_secs(30);

/// One payload paired with the buffer size it must be consumed under.
///
/// The two travel together everywhere: a payload is only meaningful against
/// the read boundary it straddles, and every diagnostic has to name both.
/// Keeping them as one value stops the pair drifting apart at a call site and
/// keeps the failure text consistent across the child, the interpreter check,
/// and the property runner.
pub(crate) struct BoundaryCase<'payload> {
    payload: &'payload [u8],
    buffer_size: u32,
}

impl<'payload> BoundaryCase<'payload> {
    pub(crate) const fn new(payload: &'payload [u8], buffer_size: u32) -> Self {
        Self {
            payload,
            buffer_size,
        }
    }

    /// Describe the case for a failure message.
    ///
    /// Length rather than content: a generated payload can be a mebibyte of
    /// arbitrary bytes, and echoing it would bury the diagnostic it belongs to.
    fn describe(&self) -> String {
        format!(
            "payload_bytes={} buffer_size={}",
            self.payload.len(),
            self.buffer_size
        )
    }
}

fn repository_root() -> Result<PathBuf, String> {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(2)
        .map(Path::to_path_buf)
        .ok_or_else(|| "cuprum-rust manifest has no repository-root ancestor".to_owned())
}

#[expect(
    clippy::disallowed_methods,
    reason = "Make injects the exact project interpreter for this extension boundary test"
)]
fn configured_project_python() -> Option<PathBuf> { env::var_os(PYTHON_ENV).map(PathBuf::from) }

/// The interpreter the Make target injected, verified to be an absolute path.
fn project_python() -> Result<PathBuf, String> {
    let python = configured_project_python()
        .ok_or_else(|| format!("{PYTHON_ENV} must name the project interpreter"))?;
    if !python.is_absolute() {
        return Err(format!(
            "project interpreter path is not absolute: {}",
            python.display()
        ));
    }
    Ok(python)
}

/// Build the child invocation that runs the independent Python consumer.
fn boundary_child_command(python: &Path, buffer_size: u32) -> Result<Command, String> {
    let helper = repository_root()?.join("tests/helpers/consume_boundary_oracle.py");
    let mut command = Command::new(python);
    command
        .arg(helper)
        .arg("--buffer-size")
        .arg(buffer_size.to_string())
        // The oracle reports the interpreter and native-module paths on
        // stdout, and both are decoded as UTF-8 here. Python picks an
        // encoding for a redirected stream from the locale, which on Windows
        // is the ANSI code page; a path containing a character outside it
        // would arrive as replacement bytes and fail the provenance check for
        // a reason that has nothing to do with the boundary under test.
        // Only the metadata stream is pinned: the payload travels on stdin as
        // raw bytes and is never decoded.
        .env("PYTHONIOENCODING", "utf-8");
    Ok(command)
}

/// Reject a child that failed, timed out, or could not receive its payload.
fn require_successful_run(
    output: &bounded_child::ChildOutput,
    case: &BoundaryCase<'_>,
) -> Result<String, String> {
    if let Some(error) = &output.stdin_error {
        return Err(format!(
            "boundary child input transfer failed for {}: {error}",
            case.describe()
        ));
    }
    let stdout = String::from_utf8_lossy(&output.stdout).into_owned();
    let stderr = String::from_utf8_lossy(&output.stderr);
    if !output.status.success() {
        return Err(format!(
            "boundary child exited with {}; {}; stdout={stdout:?}; stderr={stderr:?}",
            output.status,
            case.describe()
        ));
    }
    if !stdout.lines().any(|line| line.starts_with("PASS ")) {
        return Err(format!(
            "boundary child reported no success marker for {}: {stdout:?}",
            case.describe()
        ));
    }
    Ok(stdout)
}

/// Read one `KEY=value` diagnostic the child printed on stdout.
fn reported_line<'a>(stdout: &'a str, key: &str) -> Result<&'a str, String> {
    stdout
        .lines()
        .find_map(|line| line.strip_prefix(key))
        .ok_or_else(|| format!("boundary child omitted its {}", key.trim_end_matches('=')))
}

/// Prove the child ran on the interpreter Make selected, not another install.
fn require_expected_interpreter(stdout: &str, python: &Path) -> Result<(), String> {
    let reported_python = reported_line(stdout, "PYTHON_EXECUTABLE=")?;
    let expected_python = python
        .canonicalize()
        .map_err(|error| format!("could not resolve {PYTHON_ENV}: {error}"))?;
    let actual_python = Path::new(reported_python)
        .canonicalize()
        .map_err(|error| format!("could not resolve reported interpreter: {error}"))?;
    if actual_python != expected_python {
        return Err(format!(
            "boundary child used interpreter {}, expected {}",
            actual_python.display(),
            expected_python.display()
        ));
    }
    Ok(())
}

/// Reject a child that imported a native module from somewhere unexpected.
fn require_native_module(stdout: &str) -> Result<(), String> {
    let native_path = Path::new(reported_line(stdout, "NATIVE_MODULE=")?);
    if !native_path.is_absolute() || !native_path.is_file() {
        return Err(format!(
            "boundary child reported an invalid native module path: {}",
            native_path.display()
        ));
    }
    Ok(())
}

/// Run one case through the Python oracle and verify the child it reported.
pub(crate) fn run_boundary_case(case: &BoundaryCase<'_>) -> Result<(), String> {
    let python = project_python()?;
    let mut command = boundary_child_command(&python, case.buffer_size)?;
    let output = bounded_child::run_child(&mut command, case.payload, CHILD_TIMEOUT)
        .map_err(|failure| format!("boundary child failed: {failure}"))?;
    let stdout = require_successful_run(&output, case)?;
    require_expected_interpreter(&stdout, &python)?;
    require_native_module(&stdout)
}
