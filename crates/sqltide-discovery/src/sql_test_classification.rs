//! Classify an authored SQL test's CTEs by name into helpers, mocks, expectations, assertions,
//! and the macro-mode actual/expected pair.
//!
//! Apache-2.0: adapted from SQLBuild `compile/_helpers/sql_tests/core.py` classification.

pub const EXPECTED_CTE_PREFIX: &str = "__expected__";
pub const REF_CTE_PREFIX: &str = "__ref__";
pub const SOURCE_CTE_PREFIX: &str = "__source__";
pub const ASSERT_CTE_PREFIX: &str = "__assert__";
pub const MACRO_ACTUAL_CTE_NAME: &str = "__macro_actual__";
pub const MACRO_EXPECTED_CTE_NAME: &str = "__macro_expected__";

const RESERVED_CTE_NAMES: [&str; 5] = [
    "__actual",
    "__expected__typed",
    "__actual__projected",
    "__missing__",
    "__unexpected__",
];
const UNSUPPORTED_CTE_NAMES: [&str; 4] = [
    "__udf_actual__",
    "__udf_expected__",
    "__table_fn_actual__",
    "__table_fn_expected__",
];
const UNSUPPORTED_CTE_PREFIXES: [&str; 3] = ["__macro__", "__seed__", "__dbt_ref__"];
const MODEL_MODE_CTE_PREFIXES: [&str; 4] = [
    REF_CTE_PREFIX,
    SOURCE_CTE_PREFIX,
    EXPECTED_CTE_PREFIX,
    ASSERT_CTE_PREFIX,
];

/// What one CTE is in its test.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CteRole {
    /// A helper CTE kept in the authored chain.
    Helper,
    /// A `__ref__<name>` mock; also kept in the authored chain.
    RefMock,
    /// A `__source__<name>` mock; also kept in the authored chain.
    SourceMock,
    Expected,
    Assertion,
    MacroActual,
    MacroExpected,
}

impl CteRole {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Helper => "helper",
            Self::RefMock => "ref_mock",
            Self::SourceMock => "source_mock",
            Self::Expected => "expected",
            Self::Assertion => "assertion",
            Self::MacroActual => "macro_actual",
            Self::MacroExpected => "macro_expected",
        }
    }
}

fn reject_unsupported(name: &str, mode: &str) -> Result<(), String> {
    if UNSUPPORTED_CTE_NAMES.contains(&name)
        || UNSUPPORTED_CTE_PREFIXES
            .iter()
            .any(|prefix| name.starts_with(prefix))
    {
        return Err(format!(
            "mode '{mode}' does not support CTE '{name}'; SQLTide supports \
             __source__, __ref__, __expected__, __assert__, __macro_actual__, and \
             __macro_expected__"
        ));
    }
    Ok(())
}

fn reject_reserved(name: &str) -> Result<(), String> {
    if RESERVED_CTE_NAMES.contains(&name) {
        return Err(format!("uses reserved helper CTE name '{name}'"));
    }
    Ok(())
}

/// The role of a `<prefix><name>` CTE, rejecting an empty `<name>`.
fn prefixed(name: &str) -> Result<Option<CteRole>, String> {
    for (prefix, role) in [
        (REF_CTE_PREFIX, CteRole::RefMock),
        (SOURCE_CTE_PREFIX, CteRole::SourceMock),
        (EXPECTED_CTE_PREFIX, CteRole::Expected),
        (ASSERT_CTE_PREFIX, CteRole::Assertion),
    ] {
        if let Some(target) = name.strip_prefix(prefix) {
            if target.is_empty() {
                return Err(format!("must use {prefix}<name> to identify a target"));
            }
            return Ok(Some(role));
        }
    }
    Ok(None)
}

/// Each model-mode CTE's role, in order. Error messages omit the `SQL test '<path>' ` prefix,
/// which the caller adds.
///
/// # Errors
///
/// Returns the parse error message for the first unsupported, macro-mode, reserved, or
/// untargeted CTE, then for a test with no mocks or no expectations and assertions.
pub fn classify_model_test_ctes(names: &[&str]) -> Result<Vec<CteRole>, String> {
    let mut roles: Vec<CteRole> = Vec::with_capacity(names.len());
    for name in names {
        reject_unsupported(name, "model")?;
        if *name == MACRO_ACTUAL_CTE_NAME || *name == MACRO_EXPECTED_CTE_NAME {
            return Err(format!(
                "is mode 'model' but defines macro-test CTE '{name}'; use \
                 TEST (mode macro)"
            ));
        }
        let role: CteRole = match prefixed(name)? {
            Some(role) => role,
            None => {
                reject_reserved(name)?;
                CteRole::Helper
            }
        };
        roles.push(role);
    }
    if !roles
        .iter()
        .any(|role| matches!(role, CteRole::RefMock | CteRole::SourceMock))
    {
        return Err("must define at least one __ref__* or __source__* mock CTE".to_owned());
    }
    if !roles
        .iter()
        .any(|role| matches!(role, CteRole::Expected | CteRole::Assertion))
    {
        return Err("must define at least one __expected__<model> or \
             __assert__<assertion> CTE"
            .to_owned());
    }
    Ok(roles)
}

/// Each macro-mode CTE's role, in order. When the pair repeats, the caller keeps the last.
///
/// # Errors
///
/// Returns the parse error message for the first unsupported, model-mode, or reserved CTE, then
/// for a test missing its actual or expected CTE.
pub fn classify_macro_test_ctes(names: &[&str]) -> Result<Vec<CteRole>, String> {
    let mut roles: Vec<CteRole> = Vec::with_capacity(names.len());
    for name in names {
        reject_unsupported(name, "macro")?;
        let role: CteRole = match *name {
            MACRO_ACTUAL_CTE_NAME => CteRole::MacroActual,
            MACRO_EXPECTED_CTE_NAME => CteRole::MacroExpected,
            _ => {
                if MODEL_MODE_CTE_PREFIXES
                    .iter()
                    .any(|prefix| name.starts_with(prefix))
                {
                    return Err(format!(
                        "is mode 'macro' but defines model-test CTE \
                         '{name}'"
                    ));
                }
                reject_reserved(name)?;
                CteRole::Helper
            }
        };
        roles.push(role);
    }
    if !roles.contains(&CteRole::MacroActual) || !roles.contains(&CteRole::MacroExpected) {
        return Err(format!(
            "mode 'macro' must define exactly one {MACRO_ACTUAL_CTE_NAME} \
             CTE and exactly one {MACRO_EXPECTED_CTE_NAME} CTE"
        ));
    }
    Ok(roles)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifies_model_ctes_and_checks_completeness_after_every_cte() {
        assert_eq!(
            classify_model_test_ctes(&["h", "__ref__a", "__assert__ok"]),
            Ok(vec![CteRole::Helper, CteRole::RefMock, CteRole::Assertion])
        );
        assert_eq!(
            classify_model_test_ctes(&["__expected__a", "__actual"]),
            Err("uses reserved helper CTE name '__actual'".to_owned())
        );
        assert_eq!(
            classify_model_test_ctes(&["__ref__"]),
            Err("must use __ref__<name> to identify a target".to_owned())
        );
    }

    #[test]
    fn rejects_model_prefixes_in_macro_mode() {
        assert!(classify_macro_test_ctes(&["__macro_actual__", "__ref__a"]).is_err());
        assert!(classify_macro_test_ctes(&["__macro_actual__"]).is_err());
        assert_eq!(
            classify_macro_test_ctes(&["__macro_expected__", "x", "__macro_actual__"]),
            Ok(vec![
                CteRole::MacroExpected,
                CteRole::Helper,
                CteRole::MacroActual
            ])
        );
    }
}
