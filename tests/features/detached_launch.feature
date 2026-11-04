Feature: Detached command launch
  A detached launch starts the same command a managed run would execute and
  then gives it away: the worker gets its own session and no connected
  streams, cuprum keeps nothing but the PID, and the caller owns the worker's
  lifetime from the moment the receipt is returned.

  Every refusal here happens before a worker exists, because a refusal that
  created one would leave a process nobody could name.

  Scenario: An allowed program is launched and its lifetime transferred
    Given an allowlisted command for the current interpreter
    When the command is launched detached
    Then the receipt names the worker that was started

  Scenario: A forbidden program is refused before a worker exists
    Given an allowlisted command for the current interpreter
    And a scope that allows a different program
    When the command is launched detached
    Then the launch fails with a permission error
    And no worker was started

  Scenario: A platform without POSIX sessions is refused before a worker exists
    Given an allowlisted command for the current interpreter
    And the host reports a platform without POSIX sessions
    When the command is launched detached
    Then the launch fails with a value error
    And no worker was started

  Scenario: A missing executable fails the launch instead of returning a receipt
    Given an allowlisted command for the current interpreter
    And the program is bound to a file that does not exist
    When the command is launched detached
    Then the launch fails with a file-not-found error
    And no receipt is returned

  Scenario: Cancelling at the transfer boundary stops the worker
    Given an allowlisted command for the current interpreter
    And a launch that is cancelled as it spawns
    When the command is launched detached
    Then the launch is cancelled
    And the orphaned worker is stopped
