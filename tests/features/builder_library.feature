Feature: Core builder library

  Scenario: Git checkout builder constructs argv
    Given the core git builders
    When I build a git checkout command for ref "main"
    Then the git command argv is "git" "checkout" "main"

  Scenario: Invalid git ref is rejected
    Given the core git builders
    When I build a git checkout command for ref "bad ref"
    Then a git ref validation error is raised

  Scenario: Rsync builder constructs argv with safe paths
    Given valid rsync paths
    When I build an rsync sync command with archive enabled
    Then the rsync command argv includes "--archive" and the paths

  Scenario: Tar create builder constructs argv
    Given a tar archive and source paths
    When I build a tar create command with gzip enabled
    Then the tar command argv includes "-z" and the paths

  Scenario: Tar create separates option-shaped relative sources
    Given a relative tar source beginning with --checkpoint
    When I build a relative tar create command
    Then the tar sources follow the end-of-options separator

  Scenario: Rsync separates option-shaped relative operands
    Given relative rsync paths beginning with --
    When I build a relative rsync sync command
    Then the rsync operands follow the end-of-options separator

  Scenario Outline: Rsync builder rejects remote operands
    Given valid rsync paths
    When I build an rsync sync command with a remote "<role>" operand "<operand>"
    Then an rsync operand validation error is raised for the "<role>"

    Examples:
      | role        | operand             |
      | source      | host:path           |
      | destination | host:path           |
      | source      | host::module/path   |
      | destination | host::module/path   |
      | destination | user@host:backup    |
      | source      | rsync://host/module |
      | destination | [::1]:path          |
