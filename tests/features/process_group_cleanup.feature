Feature: Opt-in process-group cleanup
  A run that owns its process group can reclaim the descendants its direct
  child leaves behind when it exits, which direct-child teardown cannot reach.

  The policy is opt-in: the default inherits the caller's group, and only a run
  that asked to own a group may signal one.

  Scenario: An owned run reclaims a grandchild that ignores termination
    Given a command whose child leaves a SIGTERM-immune grandchild
    And the command is run under the ownership process-group policy
    When the run is cancelled
    Then the grandchild is gone after the run settles
    And the run's streams have settled
    And a process outside the run's group is untouched

  Scenario: An inheriting run leaves its grandchild alone
    Given a command whose child leaves a SIGTERM-immune grandchild
    And the command is run under the inherited process-group policy
    When the run is cancelled
    Then the run's direct child is gone
    And the grandchild is still running

  Scenario: A repeated cancellation does not abandon the cleanup
    Given a command whose child leaves a SIGTERM-immune grandchild
    And the command is run under the ownership process-group policy
    When the run is cancelled twice
    Then the grandchild is gone after the run settles
