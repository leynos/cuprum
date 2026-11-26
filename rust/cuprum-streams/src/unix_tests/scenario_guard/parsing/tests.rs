//! Parser and parity cases, including every failure shape the guard names.

use super::{Bound, bound_scenarios};

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
