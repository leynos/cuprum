Feature: Tolerating benchmark noise without tolerating regressions
  The ratchet compares each pull request against a rolling window of recent
  main-branch measurements rather than the single latest one, so a runner
  that measured badly on one merge cannot fail every pull request that
  follows it. A change that is genuinely slower must still fail.

  Scenario: One anomalous main run does not fail the pull requests after it
    Given main has measured 1.013, 1.001, 1.069, 0.916 and 1.105
    And a noisy main run then measured 0.760
    When a pull request measures 1.110
    Then the ratchet passes

  Scenario: The same pull request fails against that run alone
    Given main has measured 0.760
    When a pull request measures 1.110
    Then the ratchet fails

  Scenario: A genuine slowdown fails against a settled window
    Given main has measured 1.013, 1.001, 1.069, 0.916 and 1.105
    When a pull request measures 1.600
    Then the ratchet fails

  Scenario: A slowdown fails even when the window is noisy
    Given main has measured 1.013, 1.001, 1.069, 0.916 and 1.105
    And a noisy main run then measured 0.760
    When a pull request measures 2.000
    Then the ratchet fails

  Scenario: Measuring what main measures is never a regression
    Given main has measured 1.013, 1.001, 1.069, 0.916 and 1.105
    When a pull request measures 1.013
    Then the ratchet passes

  Scenario: A missing baseline skips the ratchet with durable evidence
    Given no main baseline is available
    When a pull request measures 1.110
    Then the ratchet is skipped with no-baseline evidence
