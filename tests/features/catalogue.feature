Feature: Catalogue defaults

  Scenario: Unknown program is blocked by default
    Given the default catalogue
    When I request the program "unknown-tool"
    Then the catalogue rejects it with an unknown program error

  Scenario: Projects expose metadata for downstream services
    Given the default catalogue
    When downstream services request visible settings
    Then project "core-ops" advertises noise rules and docs

  Scenario: Curated program is accepted via the public API
    Given the cuprum public API surface
    When I look up the curated program "echo"
    Then the lookup succeeds for project "core-ops" with a typed program
    And the allowlist accepts the string name "ls"

  Scenario: Safe command builder constructs typed argv
    Given the curated program "echo" is present in the catalogue
    When I build a safe command with "-n" and "hello world"
    Then the safe command argv includes the program name and arguments
    And the safe command exposes project metadata for downstream services

  Scenario: A catalogue scope selects the builder catalogue
    Given a catalogue owning the program "gh"
    When I build a safe command for "gh" inside that catalogue scope
    Then the safe command resolves through the scoped catalogue
    And the default catalogue still rejects "gh"

  Scenario: An executable binding names the executable but not the identity
    When I bind the program "echo" to the running interpreter
    Then the bound executable runs and reports itself
    And the logical program remains "echo"

  Scenario: A binding cannot authorize an unlisted logical program
    Given the curated program "echo" is present in the catalogue
    When I bind the unlisted program "sccache" to the executable "/opt/tools/sccache"
    Then execution is refused as a forbidden program
    And the binding's executable was never resolved
