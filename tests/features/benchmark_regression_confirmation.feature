Feature: Confirming a reported regression before failing the job
  A pull request is measured once, on whichever runner CI gave it. When that
  measurement reports a regression the job measures again and fails only on
  a scenario that regressed both times, so an unlucky runner does not fail a
  change that a re-run would have passed.

  Scenario: A regression that reproduces fails the job
    Given the first measurement flagged medium-single-nocb
    When a second measurement flags medium-single-nocb
    Then the ratchet fails on medium-single-nocb

  Scenario: A regression that does not reproduce is treated as noise
    Given the first measurement flagged medium-single-nocb
    When a second measurement flags nothing
    Then the ratchet passes
    And medium-single-nocb is reported as unconfirmed

  Scenario: A flake that moves to another scenario does not confirm
    Given the first measurement flagged medium-single-nocb
    When a second measurement flags small-single-nocb
    Then the ratchet passes
    And medium-single-nocb is reported as unconfirmed

  Scenario: The second measurement cannot fail a scenario the first passed
    Given the first measurement flagged nothing
    When a second measurement flags medium-single-nocb
    Then the ratchet passes

  Scenario: An unusable second measurement leaves the first verdict standing
    Given the first measurement flagged medium-single-nocb
    When a second measurement cannot be compared
    Then the ratchet fails on medium-single-nocb
    And confirmation is unavailable
