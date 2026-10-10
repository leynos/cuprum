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

  Scenario: Cancellation settles while observe-hook cleanup drains
    Given a long running command for terminal outcome observation
    When I cancel while terminal cleanup is observed
    Then the terminal-outcome subprocess stops cleanly
    And exactly one cancelled terminal outcome is observed

  Scenario: Repeated cancellation settles while observe-hook cleanup drains
    Given a long running command for terminal outcome observation
    When I cancel repeatedly during terminal cleanup
    Then the terminal-outcome subprocess stops cleanly
    And exactly one cancelled terminal outcome is observed

  Scenario: Spawn failure has a terminal execution outcome
    Given a registered but absent terminal-outcome executable
    When I run it with an observe hook
    Then spawn failure emits one error terminal outcome

  Scenario: Timeout terminates running subprocess
    Given a long running safe command
    When I run the command with a timeout
    Then a timeout error is raised
    And the subprocess stops cleanly

  Scenario: Non-positive timeout terminates running subprocess immediately
    Given a long running safe command
    When I run the command with an already-elapsed timeout
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

  Scenario: Run passes direct stdin input to the command
    Given a safe command that reads stdin
    When I run the command with direct stdin text
    Then the command result contains the stdin text
