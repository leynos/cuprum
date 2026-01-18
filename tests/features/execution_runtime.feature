Feature: Execution runtime
  The SafeCmd runtime executes curated commands with predictable behaviour.

  Scenario: Run captures output by default
    Given a simple safe echo command
    When I run the command asynchronously
    Then the command result contains captured output

  Scenario: Cancellation terminates running subprocess
    Given a long running safe command
    When I cancel the command after it starts
    Then the subprocess stops cleanly

  Scenario: Timeout terminates running subprocess
    Given a long running safe command
    When I run the command with a timeout
    Then a timeout error is raised
    And the subprocess stops cleanly

  Scenario: Cancellation escalates a non-cooperative subprocess
    Given a non-cooperative safe command
    When I cancel the command with a short grace period
    Then the subprocess is killed after escalation

  Scenario: Sync run captures output by default
    Given a simple safe echo command
    When I run the command synchronously
    Then the command result contains captured output
