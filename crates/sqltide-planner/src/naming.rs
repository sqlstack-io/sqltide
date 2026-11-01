//! Deployment identifiers and the deployment-suffixed physical names built from them.

use std::sync::LazyLock;

use regex::Regex;

/// `YYYYMMDDTHHMMSSZ_<alphanumeric suffix>`, where digits are any Unicode decimal digit.
static DEPLOYMENT_ID: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\A\d{8}T\d{6}Z_[A-Za-z0-9]+\z").expect("the deployment ID pattern is valid")
});

/// Whether `text` is a deployment identifier.
#[must_use]
pub fn is_deployment_id(text: &str) -> bool {
    DEPLOYMENT_ID.is_match(text)
}

/// `(logical name, deployment ID)` split at the last `__`, if there is one.
#[must_use]
pub fn split_physical_name(physical_name: &str) -> Option<(&str, &str)> {
    physical_name.rsplit_once("__")
}

/// Whether `physical_name` is a non-empty logical name suffixed with `__<deployment ID>`.
#[must_use]
pub fn is_deployment_physical_name(physical_name: &str) -> bool {
    split_physical_name(physical_name).is_some_and(|(logical_name, deployment_id)| {
        !logical_name.is_empty() && is_deployment_id(deployment_id)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recognizes_deployment_physical_names() {
        assert!(is_deployment_physical_name(
            "orders__20260131T120000Z_ab12cd"
        ));
        assert!(is_deployment_physical_name("orders___20260131T120000Z_ab"));
        assert!(is_deployment_physical_name("orders__٢٠٢٦٠١٣١T120000Z_ab"));
        assert!(!is_deployment_physical_name("__20260131T120000Z_ab"));
        assert!(!is_deployment_physical_name("orders__20260131T120000Z_"));
        assert!(!is_deployment_physical_name(
            "orders__20260131T120000Z_ab\n"
        ));
        assert!(!is_deployment_physical_name("orders__20260131T120000Z_é"));
        assert_eq!(split_physical_name("a___b"), Some(("a_", "b")));
    }
}
