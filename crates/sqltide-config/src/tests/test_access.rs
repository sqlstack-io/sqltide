//! Access-policy compilation checked against outcomes recorded from the Python compiler it
//! replaced, running on PyYAML 6.0.3: realistic policies, every distinct failure a differential
//! fuzzer reached, and hostile YAML.

use std::collections::HashSet;

use serde_json::{Value as Json, json};

use crate::access::{AccessPolicy, AccessPolicyError, compile_access_policy};

/// Cases with the Python outcome (roles and fingerprint, an error at a line and column, or the
/// message of a Python exception that was not a YAML error) and how the native outcome must
/// relate to it: `exact`, `rejects` (both reject, wording may differ), or a reviewed
/// `difference` where `saphyr-parser` accepts or rejects what PyYAML did not.
const CORPUS: &str = include_str!("fixtures/access_corpus.json");
const PIPELINES: [&str; 4] = ["orders", "customers", "inventory", "fulfillment"];
/// Reviewed ways `saphyr-parser` reads YAML differently from PyYAML.
const DIFFERENCES: [&str; 9] = [
    "comment-without-space",
    "flow-key-line-break",
    "content-after-document-end",
    "quoted-continuation-indentation",
    "flow-collection-indentation",
    "flow-adjacent-colon",
    "tab-separation",
    "tag-before-flow-indicator",
    "unicode-line-break",
];

fn outcome(result: Result<AccessPolicy, AccessPolicyError>) -> Json {
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
        Err(error) => json!({
            "error": error.message,
            "line": error.line,
            "column": error.column,
        }),
    }
}

/// Why the native outcome breaks the case's contract, if it does.
fn violation(case: &Json, actual: &Json) -> Option<String> {
    let accepted: bool = actual.get("roles").is_some();
    let difference: Option<&str> = case.get("difference").and_then(Json::as_str);
    if let Some(difference) = difference
        && !DIFFERENCES.contains(&difference)
    {
        return Some(format!("unreviewed difference {difference}"));
    }
    match case["match"].as_str().expect("each case has a match mode") {
        "exact" => (actual != &case["python"]).then(|| "differs from Python".to_owned()),
        "rejects" => accepted.then(|| "accepts what Python rejected".to_owned()),
        "rejects-accepted" => accepted.then(|| "accepts, yet the difference rejects".to_owned()),
        "accepts-rejected" => (!accepted).then(|| "rejects, yet the difference accepts".to_owned()),
        mode => Some(format!("unknown match mode {mode}")),
    }
}

#[test]
fn given_recorded_python_outcomes_when_compiling_then_outcomes_keep_their_contract() {
    let cases: Vec<Json> = serde_json::from_str(CORPUS).expect("the corpus is JSON");
    let pipelines: HashSet<String> = PIPELINES.iter().map(|name| (*name).to_owned()).collect();

    let violations: Vec<String> = cases
        .iter()
        .filter_map(|case| {
            let contents: &str = case["yaml"].as_str().expect("each case has YAML");
            let actual: Json = outcome(compile_access_policy(contents, &pipelines));
            violation(case, &actual).map(|reason| {
                format!(
                    "{contents:?}: {reason}\n  python {}\n  actual {actual}",
                    case["python"]
                )
            })
        })
        .collect();

    assert!(cases.len() > 500, "the corpus has {} cases", cases.len());
    assert!(
        violations.is_empty(),
        "{} of {} cases break their contract:\n{}",
        violations.len(),
        cases.len(),
        violations.join("\n")
    );
}
