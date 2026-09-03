Feature: Recording the benchmark gate decision in the run summary
  A skipped job and a broken gate look identical in the run list, so the
  `changes` job writes what it decided — and the two inputs behind it — to
  the run summary of every CI run. These scenarios run the workflow's own
  summary script rather than reading it, because a script that emitted
  nothing, or the wrong verdict, would still contain all the right words.

  Scenario: A pull request touching performance-relevant paths
    Given the detector succeeded and reported some performance-relevant changes
    When the gate summary script runs for a pull_request event
    Then the summary records the benchmark job as run
    And the summary reports the detector as success
    And the summary reports the changed-path verdict as true

  Scenario: A documentation-only pull request
    Given the detector succeeded and reported no performance-relevant changes
    When the gate summary script runs for a pull_request event
    Then the summary records the benchmark job as skip
    And the summary reports the detector as success
    And the summary reports the changed-path verdict as false

  Scenario: A push to main is never gated
    Given the detector succeeded and reported no performance-relevant changes
    When the gate summary script runs for a push event
    Then the summary records the benchmark job as run
    And the summary reports the changed-path verdict as false

  Scenario: The detector itself failed
    Given the detector failed
    When the gate summary script runs for a pull_request event
    Then the summary records the benchmark job as skip-detector-failed
    And the summary reports the detector as failure
    And the summary reports the changed-path verdict as unknown

  Scenario: The detector fails for a push
    Given the detector failed
    When the gate summary script runs for a push event
    Then the summary records the benchmark job as skip-detector-failed
    And the summary reports the detector as failure
    And the summary reports the changed-path verdict as unknown
