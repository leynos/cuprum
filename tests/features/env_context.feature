Feature: Scoped env context manager
  The env(...) context manager overlays environment variables on top of
  the live os.environ, resolved at subprocess spawn time so post-import
  mutations remain visible to subprocesses spawned inside the scope.

  Scenario: Overlay is visible to a subprocess spawned in scope
    Given a python builder available to the test catalogue
    When I run a command inside an env scope overlaying CUPRUM_BDD_VAR=scope-value
    Then the subprocess prints scope-value for CUPRUM_BDD_VAR

  Scenario: Variables set in os.environ after entering the scope are visible
    Given a python builder available to the test catalogue
    When I enter an env scope, then set CUPRUM_BDD_LIVE in os.environ, then run a command
    Then the subprocess prints the value that was set after entering the scope
