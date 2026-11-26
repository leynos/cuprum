//! Argument and literal cases for the attribute parser.

use rstest::rstest;

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

#[test]
fn skips_an_empty_argument() {
    let body = "#[scenario(path = \"p\",)]";

    assert_eq!(
        text_args(body),
        vec![("path".to_owned(), "p".to_owned())],
        "a trailing comma must not produce an argument",
    );
}

#[test]
fn keeps_a_bracket_nested_value_whole() {
    // The comma inside the brackets must not split the argument list.
    let body = "#[scenario(path = concat!(\"a\", \"b\"))]";

    let args = attribute_args(body).expect("a nested value must stay one argument");

    assert_eq!(args.len(), 1, "the nested comma must not split the list");
}

#[test]
fn keeps_a_bare_value_as_a_non_string() {
    let body = "#[scenario(path = 0)]";

    let args = attribute_args(body).expect("a bare value must parse");

    assert_eq!(
        args.iter()
            .filter_map(|arg| arg.text(super::ArgKey::Path))
            .count(),
        0,
        "a bare value must not masquerade as a string",
    );
}

#[test]
fn refuses_a_literal_that_never_closes() {
    let body = "#[scenario(path = \"open)]";

    let error = attribute_args(body).expect_err("an unterminated literal must fail closed");
    assert!(
        error.contains("unterminated"),
        "the failure must say the literal never closes, found {error}",
    );
}

#[rstest]
#[case::quote(r#""a\"b""#, "a\"b")]
#[case::apostrophe(r#""a\'b""#, "a'b")]
#[case::newline(r#""a\nb""#, "a\nb")]
#[case::carriage_return(r#""a\rb""#, "a\rb")]
#[case::tab(r#""a\tb""#, "a\tb")]
#[case::nul(r#""a\0b""#, "a\0b")]
fn decodes_each_supported_escape(#[case] literal: &str, #[case] expected: &str) {
    let body = format!("#[scenario(name = {literal})]");

    assert_eq!(
        text_args(&body),
        vec![("name".to_owned(), expected.to_owned())],
        "the escape must decode to the character it names",
    );
}

#[rstest]
#[case::bad_hex(r#""a\u{zz}""#, "malformed")]
#[case::not_a_char(r#""a\u{d800}""#, "not a character")]
fn refuses_a_malformed_unicode_escape(#[case] literal: &str, #[case] expected: &str) {
    let body = format!("#[scenario(name = {literal})]");

    let error = attribute_args(&body).expect_err("a malformed unicode escape must fail closed");
    assert!(
        error.contains(expected),
        "the failure must name {expected:?}, found {error}",
    );
}
