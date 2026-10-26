//! Check the compiled Python extension against the independent Python consumer.

use std::{
    env,
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

use proptest::{
    prelude::*,
    test_runner::{Config, TestCaseError, TestRunner},
};

#[path = "support/bounded_child.rs"]
mod bounded_child;

const PYTHON_ENV: &str = "CUPRUM_BOUNDARY_PYTHON";
const CHILD_TIMEOUT: Duration = Duration::from_secs(30);

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

fn run_boundary_case(payload: &[u8], buffer_size: u32) -> Result<(), String> {
    let python = configured_project_python()
        .ok_or_else(|| format!("{PYTHON_ENV} must name the project interpreter"))?;
    if !python.is_absolute() {
        return Err(format!(
            "project interpreter path is not absolute: {}",
            python.display()
        ));
    }
    let helper = repository_root()?.join("tests/helpers/consume_boundary_oracle.py");
    let mut command = Command::new(&python);
    command
        .arg(helper)
        .arg("--buffer-size")
        .arg(buffer_size.to_string());

    let output = bounded_child::run_child(&mut command, payload, CHILD_TIMEOUT)
        .map_err(|failure| format!("boundary child failed: {failure}"))?;
    if let Some(error) = output.stdin_error {
        return Err(format!(
            "boundary child input transfer failed for payload_bytes={} buffer_size={buffer_size}: \
             {error}",
            payload.len()
        ));
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    if !output.status.success() {
        return Err(format!(
            "boundary child exited with {}; payload_bytes={} buffer_size={buffer_size}; \
             stdout={stdout:?}; stderr={stderr:?}",
            output.status,
            payload.len()
        ));
    }
    if !stdout.lines().any(|line| line.starts_with("PASS ")) {
        return Err(format!(
            "boundary child reported no success marker for payload_bytes={} \
             buffer_size={buffer_size}: {stdout:?}",
            payload.len()
        ));
    }

    let reported_python = stdout
        .lines()
        .find_map(|line| line.strip_prefix("PYTHON_EXECUTABLE="))
        .ok_or_else(|| "boundary child omitted its interpreter path".to_owned())?;
    let reported_native = stdout
        .lines()
        .find_map(|line| line.strip_prefix("NATIVE_MODULE="))
        .ok_or_else(|| "boundary child omitted its native module path".to_owned())?;
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
    let native_path = Path::new(reported_native);
    if !native_path.is_absolute() || !native_path.is_file() {
        return Err(format!(
            "boundary child reported an invalid native module path: {}",
            native_path.display()
        ));
    }
    Ok(())
}

fn property_runner(cases: u32) -> TestRunner {
    let config = Config {
        cases,
        max_shrink_iters: 1024,
        max_shrink_time: 120_000,
        ..Config::default()
    };
    TestRunner::new(config)
}

#[test]
#[ignore = "requires the compiled extension; run make test-boundary-parity"]
fn native_boundary_named_witness() {
    let payload = b"boundary\x00 split-\xe2\x98\x83 malformed-\xed\xa0\x80";
    run_boundary_case(payload, 2).unwrap_or_else(|error| panic!("{error}"));
}

#[test]
#[ignore = "requires the compiled extension; run make test-boundary-parity"]
fn native_boundary_smoke_property() {
    let strategy = prop::collection::vec(any::<u8>(), 0..=128);
    let result = property_runner(1).run(&strategy, |payload| {
        run_boundary_case(&payload, 4096).map_err(TestCaseError::fail)
    });
    if let Err(error) = result {
        panic!("shrinkable native boundary smoke property failed: {error}");
    }
}
