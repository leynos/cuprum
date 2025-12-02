Feature: Catalogue defaults

  Scenario: Unknown program is blocked by default
    Given the default catalogue
    When I request the program "unknown-tool"
    Then the catalogue rejects it with an unknown program error

  Scenario: Projects expose metadata for downstream services
    Given the default catalogue
    When downstream services request visible settings
    Then project "core-ops" advertises noise rules and docs
