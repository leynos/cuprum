Feature: CI runner selection by event
  The placement contracts read declarations one job at a time. These scenarios
  exercise the composed workflows: they evaluate each event against the real
  YAML, follow the call from ci.yml into build-wheels.yml, and assert the
  runner each event actually selects.

  The property that matters is not that a lane names two labels. It is that a
  fork's pull request never selects a runner a fork cannot obtain, and that an
  owned event never gives away the paid lane.

  Scenario: an owned pull request keeps every reviewed lane on the paid runner
    Given the continuous integration workflow
    When a pull request is opened from a branch of this repository
    Then every reviewed lane selects the Ubicloud runner
    And the wheel jobs reached through the called workflow select it too

  Scenario: a fork's pull request selects only runners a fork can obtain
    Given the continuous integration workflow
    When a pull request is opened from a fork
    Then no lane selects a paid runner
    And every fork-reachable lane selects the hosted fallback

  Scenario: a push to the default branch takes the owned arm
    Given the continuous integration workflow
    When a commit is pushed to the default branch
    Then every reviewed lane selects the Ubicloud runner

  Scenario: a push does not schedule the pull-request-only lane
    Given the continuous integration workflow
    When a commit is pushed to the default branch
    Then the pull-request-only lane is absent

  Scenario: a pull request does schedule the pull-request-only lane
    Given the continuous integration workflow
    When a pull request is opened from a branch of this repository
    Then the pull-request-only lane is present

  Scenario: a tag push reaches the wheel jobs on the paid runner
    Given the release workflow
    When a tag is pushed
    Then the wheel jobs reached through the called workflow select it too
    And the native wheel matrix keeps its platform runners

  Scenario Outline: every selected runner is a schedulable label
    Given the continuous integration workflow
    When the <event> event is evaluated
    Then every selected label is a concrete runner name

    Examples:
      | event    |
      | fork     |
      | owned    |
      | push     |
