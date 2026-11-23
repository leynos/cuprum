//! Argument and literal cases for the attribute parser.

use super::{ArgValue, attribute_args};

fn text_args(body: &str) -> Vec<(String, String)> {
    let args = attribute_args(body).expect("the sample attribute must parse");
    args.iter()
        .filter_map(|arg| {
            let key = arg.key.clone();
            match &arg.value {
                ArgValue::Text(text) => Some((key, text.clone())),
                ArgValue::Bare => None,
            }
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
fn keeps_a_bare_index_value_separate_from_text() {
    let body = "#[scenario(path = \"p\", index = 0)]";

    let args = attribute_args(body).expect("the sample attribute must parse");

    assert!(
        args.iter().any(|arg| arg.is_key("index")),
        "an index argument must still be recognised by key",
    );
    assert_eq!(
        args.iter().filter_map(|arg| arg.text("index")).count(),
        0,
        "a bare value must not masquerade as a string",
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
