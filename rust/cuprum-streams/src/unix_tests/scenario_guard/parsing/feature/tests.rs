//! Grammar cases for the feature parser.
//!
//! Every refusal the parser can report has a case here, so the diagnostic
//! text and its line number stay pinned as the grammar evolves.

use rstest::rstest;

use super::declared_scenarios;

fn feature(scenarios: &str) -> String { format!("Feature: Demo\n{scenarios}") }

fn declared_titles(feature_text: &str) -> Vec<String> {
    declared_scenarios(feature_text)
        .expect("the sample feature must parse")
        .into_iter()
        .map(|entry| entry.title)
        .collect()
}

#[test]
fn parses_plain_scenarios_and_steps() {
    let text = feature(
        "  Scenario: First case\n    Given a value\n    When it runs\n    Then it passes\n\n  \
         Scenario: Second case\n    Given a value\n",
    );

    assert_eq!(
        declared_titles(&text),
        vec!["First case".to_owned(), "Second case".to_owned()],
        "every plain scenario must be declared exactly once",
    );
}

#[test]
fn rejects_an_outline_with_its_line_number() {
    let text = feature(
        "  Scenario Outline: Outline case\n    Given a value of <size>\n\n    Examples:\n      | \
         size |\n      | 1    |\n",
    );

    let error = declared_scenarios(&text).expect_err("an outline must fail closed");
    assert!(
        error.contains("line 2") && error.contains("Scenario Outline"),
        "the failure must name the outline and its line, found {error}",
    );
}

#[test]
fn rejects_a_localized_keyword() {
    let text = "Fonctionnalité: Démo\n  Scénario: Cas\n";

    let error = declared_scenarios(text).expect_err("a localized keyword must fail closed");
    assert!(
        error.contains("localized keywords"),
        "the failure must name the unsupported keyword class, found {error}",
    );
}

#[test]
fn rejects_a_scenario_without_steps() {
    // A step-less scenario runs nothing, but its binding still reports a
    // passing test, so the grammar must refuse it rather than let the
    // execution guard certify an empty run.
    let text = feature("  Scenario: No steps here\n\n  Scenario: Another with none\n");

    let error = declared_scenarios(&text).expect_err("a step-less scenario must fail closed");
    assert!(
        error.contains("needs at least one step"),
        "the failure must say a scenario needs a step, found {error}",
    );
}

#[test]
fn rejects_a_trailing_scenario_without_steps() {
    // The last declaration has no following scenario to close it, so the
    // rejection has to happen at end of input too.
    let text = feature("  Scenario: Has steps\n    Given a value\n\n  Scenario: Empty tail\n");

    let error = declared_scenarios(&text).expect_err("a trailing step-less scenario must fail");
    assert!(
        error.contains("Empty tail") || error.contains("line 5"),
        "the failure must name the trailing declaration, found {error}",
    );
}

#[test]
fn rejects_a_second_feature_header() {
    let text = "Feature: One\nFeature: Two\n  Scenario: Case\n    Given a step\n";

    let error = declared_scenarios(text).expect_err("a second header must fail closed");
    assert!(
        error.contains("exactly one Feature header"),
        "the failure must name the duplicate header, found {error}",
    );
}

#[test]
fn rejects_a_scenario_before_the_feature_header() {
    let error = declared_scenarios("  Scenario: Early\n    Given a step\n")
        .expect_err("a scenario before the header must fail closed");
    assert!(
        error.contains("must follow the Feature header"),
        "the failure must name the ordering rule, found {error}",
    );
}

#[test]
fn rejects_a_step_before_any_scenario() {
    let error = declared_scenarios("Feature: Demo\n    Given an orphan step\n")
        .expect_err("a step before a scenario must fail closed");
    assert!(
        error.contains("must belong to a Scenario"),
        "the failure must name the orphan step, found {error}",
    );
}

#[test]
fn rejects_a_feature_header_without_a_title() {
    let error =
        declared_scenarios("Feature:\n").expect_err("a header without a title must fail closed");
    assert!(
        error.contains("needs a title"),
        "the failure must name the missing title, found {error}",
    );
}

#[test]
fn rejects_a_scenario_declaration_without_a_title() {
    let error = declared_scenarios("Feature: Demo\n  Scenario:\n")
        .expect_err("a declaration without a title must fail closed");
    assert!(
        error.contains("needs a title"),
        "the failure must name the missing title, found {error}",
    );
}

#[rstest]
#[case::table("| a | b |", "tables")]
#[case::doc_string("\"\"\"text\"\"\"", "doc strings")]
#[case::localized("Scénario: Cas", "localized keywords")]
#[case::unknown_header("Outline: Case", "plain Scenario declarations")]
#[case::unknown_line("just prose", "unsupported feature line")]
fn rejects_each_unsupported_construct(#[case] body: &str, #[case] expected: &str) {
    let text = format!("Feature: Demo\n  Scenario: Case\n    Given a step\n{body}\n");

    let error = declared_scenarios(&text).expect_err("the construct must fail closed");
    assert!(
        error.contains(expected),
        "the failure must name {expected:?}, found {error}",
    );
}

#[test]
fn rejects_a_bare_examples_block() {
    let error = declared_scenarios("Feature: Demo\n  Examples:\n    | size |\n")
        .expect_err("an Examples block must fail closed");

    assert!(
        error.contains("only plain Scenario declarations"),
        "the failure must name the unsupported header, found {error}",
    );
}

#[test]
fn rejects_a_rule_block() {
    let error = declared_scenarios("Feature: Demo\n  Rule: Grouping\n")
        .expect_err("a Rule must fail closed");

    assert!(
        error.contains("only plain Scenario declarations"),
        "the failure must name the unsupported header, found {error}",
    );
}

#[test]
fn rejects_a_rule_that_precedes_its_scenarios() {
    let text = "Feature: Demo\n  Rule: Grouping\n  Scenario: Case\n    Given a step\n";

    let error = declared_scenarios(text).expect_err("a Rule must fail closed");

    assert!(
        error.contains("only plain Scenario declarations"),
        "the failure must name the unsupported header, found {error}",
    );
}
