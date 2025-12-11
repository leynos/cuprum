Feature: Execution context and hooks
  CuprumContext scopes allowlists and hooks for command execution.

  Scenario: Scoped context narrows allowlist
    Given a context that allows echo and ls
    When I enter a scoped context allowing only echo
    Then echo is allowed in the inner scope
    And ls is not allowed in the inner scope

  Scenario: Context restores after scope exits
    Given a context that allows echo and ls
    When I enter and exit a scoped context allowing only echo
    Then ls is allowed in the outer scope

  Scenario: Before hooks execute in registration order
    Given a context with multiple before hooks
    When I invoke the before hooks
    Then they execute in registration order

  Scenario: After hooks execute in reverse order
    Given a context with multiple after hooks
    When I invoke the after hooks
    Then they execute in reverse registration order

  Scenario: Hook registration can be detached
    Given a context with a registered before hook
    When I detach the hook registration
    Then the hook is no longer in the context

  Scenario: Context is isolated across threads
    Given two threads with different allowlists
    When each thread checks its allowlist
    Then each thread sees its own allowlist

  Scenario: Context is isolated across async tasks
    Given two async tasks with different allowlists
    When each task checks its allowlist
    Then each task sees its own allowlist
