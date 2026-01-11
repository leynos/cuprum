Feature: Concurrent command execution
  Run multiple SafeCmd instances concurrently with optional limits.

  Scenario: Execute commands concurrently and collect results
    Given three quick echo commands
    When I run them concurrently
    Then all results are returned in submission order
    And all commands succeeded

  Scenario: Concurrency limit restricts parallel execution
    Given four commands that sleep briefly
    When I run them with concurrency limit 2
    Then all commands complete successfully
    And execution respects the concurrency limit

  Scenario: Failed commands are collected in results
    Given two succeeding and one failing command
    When I run them concurrently in collect-all mode
    Then all three results are returned
    And the failure index is reported

  Scenario: Fail-fast cancels pending commands
    Given a failing command and a slow command
    When I run them with fail-fast enabled
    Then partial results are returned quickly
    And the first failure is accessible
