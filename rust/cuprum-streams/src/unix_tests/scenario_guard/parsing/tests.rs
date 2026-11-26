//! Parser and parity cases, including every failure shape the guard names.

use rstest::rstest;

use super::{Bound, Declared, ParityTable, bound_scenarios, check_parity};

const PATH: &str = "tests/features/demo.feature";

fn sample_binding(name: &str, function: &str) -> String {
    format!(
        "#[scenario(\n    path = \"{PATH}\",\n    name = \"{name}\"\n)]\nfn {function}(context: \
         C) {{ let _ = context; }}\n",
    )
}

#[test]
fn rejects_an_unnamed_binding() {
    let source = "#[scenario(\n    path = \"tests/features/demo.feature\"\n)]\nfn unnamed() {}\n";

    let error = bound_scenarios(source, PATH).expect_err("a binding must name its scenario");
    assert!(
        error.contains("must name `name`"),
        "the failure must name the missing key, found {error}",
    );
}

#[test]
fn rejects_an_index_binding() {
    let source = "#[scenario(\n    path = \"tests/features/demo.feature\",\n    index = 0\n)]\nfn \
                  indexed() {}\n";

    // `index` is not in the key vocabulary, so an index binding is refused
    // before any comparison can treat it as a name.
    let error = bound_scenarios(source, PATH).expect_err("an index binding must be rejected");
    assert!(
        error.contains("unknown argument") && error.contains("index"),
        "the failure must name the refused `index` key, found {error}",
    );
}

#[test]
fn rejects_a_binding_for_another_feature() {
    let source = "#[scenario(\n    path = \"tests/features/other.feature\",\n    name = \
                  \"Case\"\n)]\nfn case() {}\n";

    let error = bound_scenarios(source, PATH).expect_err("a wrong feature path must be rejected");
    assert!(
        error.contains("tests/features/demo.feature"),
        "the failure must name the expected path, found {error}",
    );
}

#[test]
fn extracts_names_and_functions() {
    let source = sample_binding("First case", "first_case");

    let bound = bound_scenarios(&source, PATH).expect("the sample binding must parse");

    assert_eq!(
        bound,
        vec![Bound {
            name: "First case".to_owned(),
            function: "first_case".to_owned(),
            line: 1,
        }],
        "the binding must retain its name, function and line",
    );
}

#[test]
fn parity_accepts_matching_multisets() {
    let declared = vec![Declared {
        title: "First case".to_owned(),
        line: 2,
    }];
    let bound = vec![Bound {
        name: "First case".to_owned(),
        function: "first_case".to_owned(),
        line: 1,
    }];

    let table = ParityTable::from_rows(&[("First case", "first_case")]);
    let outcome = check_parity(&declared, &bound, &table);

    assert_eq!(outcome, Ok(()), "matching declarations must pass parity");
}

#[test]
fn parity_reports_a_duplicate_declared_title() {
    let declared = vec![
        Declared {
            title: "Same case".to_owned(),
            line: 2,
        },
        Declared {
            title: "Same case".to_owned(),
            line: 5,
        },
    ];
    let bound = vec![Bound {
        name: "Same case".to_owned(),
        function: "same_case".to_owned(),
        line: 1,
    }];

    let table = ParityTable::from_rows(&[("Same case", "same_case")]);
    let error = check_parity(&declared, &bound, &table)
        .expect_err("a duplicate declaration must fail parity");
    assert!(
        error.contains("appears 2 times"),
        "the failure must count the duplicate, found {error}",
    );
}

#[test]
fn parity_reports_a_declaration_without_a_binding() {
    let declared = vec![
        Declared {
            title: "Bound case".to_owned(),
            line: 2,
        },
        Declared {
            title: "Unbound case".to_owned(),
            line: 5,
        },
    ];
    let bound = [
        Bound {
            name: "Bound case".to_owned(),
            function: "bound_case".to_owned(),
            line: 1,
        },
        Bound {
            name: "Unbound case".to_owned(),
            function: "unbound_case".to_owned(),
            line: 9,
        },
    ];
    let table = ParityTable::from_rows(&[
        ("Bound case", "bound_case"),
        ("Unbound case", "unbound_case"),
    ]);
    let Some(first_bound) = bound.first().cloned() else {
        panic!("the fixture must declare a binding to drop");
    };

    let error = check_parity(&declared, &[first_bound], &table)
        .expect_err("a declaration without a binding must fail parity");
    assert!(
        error.contains("has no matching scenario binding name"),
        "the failure must name the missing binding, found {error}",
    );
}

#[test]
fn parity_reports_an_unexpected_binding() {
    let declared = vec![Declared {
        title: "Known case".to_owned(),
        line: 2,
    }];
    let bound = vec![
        Bound {
            name: "Known case".to_owned(),
            function: "known_case".to_owned(),
            line: 1,
        },
        Bound {
            name: "Extra case".to_owned(),
            function: "extra_case".to_owned(),
            line: 8,
        },
    ];

    let table = ParityTable::from_rows(&[("Known case", "known_case")]);
    let error = check_parity(&declared, &bound, &table)
        .expect_err("an unexpected binding must fail parity");
    assert!(
        error.contains("Extra case") && error.contains("has no matching"),
        "the failure must name the unexpected binding, found {error}",
    );
}

#[test]
fn accepts_a_title_that_mentions_an_index_argument() {
    let source = "#[scenario(\n    path = \"tests/features/demo.feature\",\n    name = \"Consume \
                  handles index = 0\"\n)]\nfn case() {}\n";

    let bound = bound_scenarios(source, PATH)
        .expect("a title mentioning `index =` is a value, not an argument");

    assert_eq!(
        bound.first().map(|entry| entry.name.as_str()),
        Some("Consume handles index = 0"),
        "the binding must keep its complete title",
    );
}

#[test]
fn decodes_escaped_quotes_in_a_bound_name() {
    let source = "#[scenario(\n    path = \"tests/features/demo.feature\",\n    name = \"Consume \
                  preserves \\\"quoted\\\" text\"\n)]\nfn case() {}\n";
    let declared = vec![Declared {
        title: "Consume preserves \"quoted\" text".to_owned(),
        line: 2,
    }];
    let table = ParityTable::from_rows(&[("Consume preserves \"quoted\" text", "case")]);

    let bound = bound_scenarios(source, PATH).expect("the escaped title must parse");
    let outcome = check_parity(&declared, &bound, &table);

    assert_eq!(
        outcome,
        Ok(()),
        "a decoded title must match the feature declaration exactly",
    );
}

/// Two titles whose declarations and expectation rows are fixed.
///
/// Only the functions the bindings call vary between the cases below, so the
/// fixture keeps each case focused on the pairing decision.
fn two_declared_titles() -> Vec<Declared> {
    vec![
        Declared {
            title: "First case".to_owned(),
            line: 2,
        },
        Declared {
            title: "Second case".to_owned(),
            line: 5,
        },
    ]
}

fn two_row_table() -> ParityTable<'static> {
    ParityTable::from_rows(&[("First case", "first_case"), ("Second case", "second_case")])
}

/// Build the two bindings with the functions the case supplies.
fn bindings_calling(first: &str, second: &str) -> [Bound; 2] {
    [
        Bound {
            name: "First case".to_owned(),
            function: first.to_owned(),
            line: 1,
        },
        Bound {
            name: "Second case".to_owned(),
            function: second.to_owned(),
            line: 8,
        },
    ]
}

#[rstest]
#[case::correctly_paired("first_case", "second_case", true)]
#[case::swapped("second_case", "first_case", false)]
fn parity_pairs_each_title_with_its_own_binding(
    #[case] first: &str,
    #[case] second: &str,
    #[case] should_pass: bool,
) {
    let declared = two_declared_titles();
    let table = two_row_table();
    let bound = bindings_calling(first, second);

    let outcome = check_parity(&declared, &bound, &table);

    if should_pass {
        assert_eq!(
            outcome,
            Ok(()),
            "the row ({first:?}, {second:?}) pairs each title with its own binding",
        );
        return;
    }
    let error = outcome.expect_err("a swap between two rows must fail parity");
    assert!(
        error.contains("expects binding") && error.contains("calls"),
        "the failure must name the row and the function it actually calls, found {error}",
    );
}

#[test]
fn parity_reports_a_duplicate_binding_name() {
    // Two bindings name the same scenario, so each declaration is covered
    // twice while the other declaration has no binding of its own.
    let declared = vec![Declared {
        title: "Only case".to_owned(),
        line: 2,
    }];
    let bound = [
        Bound {
            name: "Only case".to_owned(),
            function: "first_fn".to_owned(),
            line: 1,
        },
        Bound {
            name: "Only case".to_owned(),
            function: "second_fn".to_owned(),
            line: 8,
        },
    ];
    let table = ParityTable::from_rows(&[("Only case", "first_fn")]);

    let error = check_parity(&declared, &bound, &table)
        .expect_err("a duplicate binding name must fail parity");

    assert!(
        error.contains("scenario binding name") && error.contains("appears 2 times"),
        "the failure must count the duplicate binding, found {error}",
    );
}

#[test]
fn parity_reports_a_duplicate_expectation_row() {
    let declared = vec![Declared {
        title: "Only case".to_owned(),
        line: 2,
    }];
    let bound = [Bound {
        name: "Only case".to_owned(),
        function: "only_fn".to_owned(),
        line: 1,
    }];
    let table = ParityTable::from_rows(&[("Only case", "only_fn"), ("Only case", "only_fn")]);

    let error = check_parity(&declared, &bound, &table)
        .expect_err("a duplicate expectation row must fail parity");

    assert!(
        error.contains("expectation table title") && error.contains("appears 2 times"),
        "the failure must count the duplicate row, found {error}",
    );
}
