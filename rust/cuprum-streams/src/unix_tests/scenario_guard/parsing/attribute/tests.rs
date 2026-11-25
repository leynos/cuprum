//! Argument and literal cases for the attribute parser.

use super::{ArgValue, attribute_args};

fn text_args(body: &str) -> Vec<(String, String)> {
    let args = attribute_args(body).expect("the sample attribute must parse");
    args.iter()
        .filter_map(|arg| match &arg.value {
            ArgValue::Text(text) => Some((arg.key.as_str().to_owned(), text.clone())),
            ArgValue::Bare => None,
        })
        .collect()
}

#[test]
fn parses_plain_arguments() {
    let body = "#[scenario(path = \"tests/features/demo.feature\", name = \"A case\")]";

    assert_eq!(
        text_args(body),
        vec![
            ("path".to_owned(), "tests/features/demo.feature".to_owned()),
            ("name".to_owned(), "A case".to_owned()),
        ],
        "both arguments must keep their key and value",
    );
}

#[test]
fn decodes_escaped_quotes_in_a_name() {
    let body = r#"#[scenario(name = "Consume preserves \"quoted\" text")]"#;

    assert_eq!(
        text_args(body),
        vec![(
            "name".to_owned(),
            "Consume preserves \"quoted\" text".to_owned()
        )],
        "an escaped quote must decode rather than end the literal",
    );
}

#[test]
fn decodes_escaped_backslash_and_unicode() {
    let body = r#"#[scenario(name = "a\\b\u{1F600}")]"#;

    assert_eq!(
        text_args(body),
        vec![("name".to_owned(), "a\\b\u{1F600}".to_owned())],
        "backslash and unicode escapes must decode",
    );
}

#[test]
fn refuses_an_unknown_argument_key() {
    // An index binding is refused at the key vocabulary, before any comparison.
    let body = "#[scenario(path = \"p\", index = 0)]";

    let error = attribute_args(body).expect_err("an unknown key must fail closed");
    assert!(
        error.contains("unknown argument") && error.contains("index"),
        "the failure must name the unknown key, found {error}",
    );
}

#[test]
fn refuses_an_unsupported_escape() {
    // `\q` is not a Rust escape, so the parser must refuse rather than guess.
    let body = r#"#[scenario(name = "bell\qhere")]"#;

    let error = attribute_args(body).expect_err("an unsupported escape must fail closed");
    assert!(
        error.contains("unsupported escape"),
        "the failure must name the escape class, found {error}",
    );
}

#[test]
fn refuses_an_attribute_without_a_closing_paren() {
    let body = "#[scenario(name = \"open";

    let error = attribute_args(body).expect_err("a truncated attribute must fail closed");
    assert!(
        error.contains("never closed"),
        "the failure must say the argument list never closes, found {error}",
    );
}
