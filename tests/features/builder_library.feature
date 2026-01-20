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
