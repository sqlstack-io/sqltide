//! Access-policy compilation checked against outcomes recorded from the Python compiler it
//! replaced, running on PyYAML 6.0.3: realistic policies, every distinct failure a differential
//! fuzzer reached, and hostile YAML.

use std::collections::HashSet;

use serde_json::{Value as Json, json};

use crate::access::{AccessPolicy, AccessPolicyError, compile_access_policy};
use crate::tests::test_types::AccessPolicyErrorTestCase;

/// Cases with the Python outcome: roles and fingerprint, an error at a line and column, or the
/// message of a Python exception that was not a YAML error (`crash`).
const CORPUS: &str = include_str!("fixtures/access_corpus.json");
const PIPELINES: [&str; 4] = ["orders", "customers", "inventory", "fulfillment"];

fn pipelines() -> HashSet<String> {
    PIPELINES.iter().map(|name| (*name).to_owned()).collect()
}

fn outcome(result: Result<AccessPolicy, AccessPolicyError>, expected: &Json) -> Json {
    match result {
        Ok(policy) => json!({
            "roles": policy.roles.iter().map(|role| json!([
                role.name,
                role.description,
                role.grants.iter().map(|grant| json!([
                    grant.permissions,
                    grant.pipelines,
                    grant.scope,
                ])).collect::<Vec<_>>(),
            ])).collect::<Vec<_>>(),
            "fingerprint": policy.fingerprint,
        }),
        Err(error) if expected.get("crash").is_some() => json!({ "crash": error.message }),
        Err(error) => json!({
            "error": error.message,
            "line": error.line,
            "column": error.column,
        }),
    }
}

#[test]
fn given_recorded_python_outcomes_when_compiling_then_outcomes_match() {
    let cases: Vec<Json> = serde_json::from_str(CORPUS).expect("the corpus is JSON");
    let pipelines: HashSet<String> = pipelines();

    let mismatches: Vec<String> = cases
        .iter()
        .filter_map(|case| {
            let contents: &str = case["yaml"].as_str().expect("each case has YAML");
            let actual: Json = outcome(
                compile_access_policy(contents, &pipelines),
                &case["expected"],
            );
            (actual != case["expected"]).then(|| {
                format!(
                    "{contents:?}\n  expected {}\n  actual   {actual}",
                    case["expected"]
                )
            })
        })
        .collect();

    assert!(cases.len() > 500, "the corpus has {} cases", cases.len());
    assert!(
        mismatches.is_empty(),
        "{} of {} cases differ:\n{}",
        mismatches.len(),
        cases.len(),
        mismatches.join("\n")
    );
}

#[test]
fn given_input_python_failed_on_outside_yaml_when_compiling_then_error_is_located() {
    let test_cases = [
        AccessPolicyErrorTestCase {
            description: "impossible date in an explicit timestamp",
            contents: "roles:\n  analyst:\n    description: !!timestamp 2001-02-29\n",
            expected_message: "day is out of range for month",
            expected_line: 3,
            expected_column: 18,
        },
        AccessPolicyErrorTestCase {
            description: "unknown boolean word",
            contents: "roles: {analyst: {description: !!bool Maybe}}",
            expected_message: "'maybe'",
            expected_line: 1,
            expected_column: 32,
        },
        AccessPolicyErrorTestCase {
            description: "control character PyYAML's reader rejects",
            contents: "roles:\n  ana\u{1}lyst: {}\n",
            expected_message: "unacceptable character #x0001: special characters are not allowed",
            expected_line: 2,
            expected_column: 6,
        },
        AccessPolicyErrorTestCase {
            description: "escape of a lone surrogate, which Rust text cannot hold",
            contents: "roles:\n  \"ana\\ud800lyst\": {}\n",
            expected_message: "found an escape for the lone surrogate U+D800, which SQLTide does not accept",
            expected_line: 2,
            expected_column: 9,
        },
    ];

    for test_case in test_cases {
        let error: AccessPolicyError = compile_access_policy(test_case.contents, &pipelines())
            .expect_err(test_case.description);

        assert_eq!(
            error,
            AccessPolicyError {
                message: test_case.expected_message.to_owned(),
                line: test_case.expected_line,
                column: test_case.expected_column,
            },
            "{}",
            test_case.description
        );
    }
}
