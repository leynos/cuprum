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
