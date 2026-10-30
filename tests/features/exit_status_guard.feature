Feature: Preserve the exit status through a closed output pipe
  A command-line tool that relays a child's output exits with the child's
  status. When the reader at the other end of the tool's standard output goes
  away, CPython's final flush fails and replaces the requested status with
  120. An opt-in, process-level guard armed at the entry point keeps the
  status the application asked for.

  Scenario: The guard preserves a successful child's status
    Given a probe that relays a child which exits with status 0
    And the probe installs the exit-status guard
    When the reader closes after the child is ready
    Then the probe exits with status 0

  Scenario: The guard preserves a failing child's status
    Given a probe that relays a child which exits with status 3
    And the probe installs the exit-status guard
    When the reader closes after the child is ready
    Then the probe exits with status 3

  Scenario: The guard preserves the status when stderr is merged into stdout
    Given a probe that relays a child which exits with status 3
    And the probe installs the exit-status guard
    And the probe's standard error is merged into its standard output
    When the reader closes after the child is ready
    Then the probe exits with status 3

  Scenario: Without the guard CPython reports 120
    Given a probe that relays a child which exits with status 3
    And the probe does not install the exit-status guard
    When the reader closes after the child is ready
    Then the probe exits with status 120