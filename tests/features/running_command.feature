Feature: Running command handle
  SafeCmd.start() hands back an owned handle with explicit control over the
  child's lifetime: wait for its outcome, signal it, stop it gracefully, stop
  it forcefully, and settle it deterministically on context-manager exit.

  Scenario: Start returns an owned handle before the child exits
    Given a child that blocks until stopped
    When I start it and hold the handle
    Then the handle reports the running child's identifier
    And the child is still running when the handle is returned

  Scenario: Wait returns the child's outcome and captures its output
    Given a safe command whose child exits immediately with status 7
    When I start it and await its outcome
    Then the handle reports the command's exit code
    And the handle reports the command's captured output

  Scenario: Startup failure is owned by start
    Given a registered but absent start executable
    When I start it
    Then start reports the spawn failure
    And the failed start leaves no orphaned child

  Scenario: A child that exits before any signal is refused
    Given a safe command whose child exits immediately with status 3
    When I start it, await its outcome, and signal it
    Then the signal is refused because the child has already exited
    And the exited child's process has gone

  Scenario: Readiness timeout settles the handle
    Given a safe command whose child blocks until stopped
    When I start it and wait with an elapsed deadline
    Then the handle reports a timeout error
    And the stopped child has been reaped

  Scenario: Concurrent waits settle the command once
    Given a safe command whose child writes after a delay
    When I issue several waits on one handle at once
    Then every concurrent wait reports the same outcome
    And the concurrently awaited child has been reaped

  Scenario: Terminate stops a cooperative child gracefully
    Given a child that blocks until stopped
    When I start it and terminate it
    Then the worker's result reports the graceful signal
    And the worker's captured output is preserved
    And the terminated worker has been reaped

  Scenario: Terminate escalates a non-cooperative child
    Given a child that ignores SIGTERM and blocks
    When I start it and terminate it with a short grace period
    Then the worker's result reports the forced signal
    And the escalated worker has been reaped

  Scenario: Kill stops a child without asking
    Given a child that blocks until stopped
    When I start it and kill it
    Then the worker's result reports the forced signal
    And the killed worker has been reaped

  Scenario: Repeated termination is idempotent
    Given a child that blocks until stopped
    When I start it and terminate it twice
    Then both terminations report one outcome
    And the repeatedly terminated worker has been reaped

  Scenario: Context-manager exit settles a running child
    Given a child that blocks until stopped
    When I leave the handle's context while the child runs
    Then the context exit settled the handle
    And the context-managed worker has been reaped

  Scenario: Context-manager exit does not suppress the block's error
    Given a child that blocks until stopped
    When I leave the handle's context with an error
    Then the block's error reaches the caller
    And the suppressed-check worker has been reaped

  Scenario: Cancellation during context-manager teardown still settles
    Given a child that ignores SIGTERM and blocks
    When I leave the handle's context while teardown is cancelled
    Then the cancelled teardown still settled the handle
    And the cancellation-settled worker has been reaped

  Scenario: A platform that cannot deliver the signal refuses it
    Given a child that blocks until stopped
    When I signal a live child on an unsupported platform
    Then the unsupported platform refuses the signal
    And the refused signal names what was asked for
