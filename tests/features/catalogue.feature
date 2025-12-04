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
