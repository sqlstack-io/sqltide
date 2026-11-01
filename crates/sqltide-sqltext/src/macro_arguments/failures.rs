//! The argument errors parsing and evaluation report.

/// One argument error; `detail` completes "Macro arguments in '<file>' ...".
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Failure {
    pub detail: String,
}

fn failure(detail: &str) -> Failure {
    Failure {
        detail: detail.to_owned(),
    }
}

/// Text Python cannot tokenize or parse.
pub(crate) fn syntax(detail: &str) -> Failure {
    failure(&format!("could not be parsed: {detail}"))
}

impl Failure {
    /// Valid Python that is not a supported argument value.
    #[must_use]
    pub fn unsupported() -> Self {
        failure("must use only Python literals and nested macro calls")
    }

    /// A bytes, complex or `...` constant.
    #[must_use]
    pub fn unsupported_literal() -> Self {
        failure("use an unsupported literal value")
    }

    #[must_use]
    pub fn unary() -> Self {
        failure("use unsupported unary value")
    }

    #[must_use]
    pub fn keyword_expansion() -> Self {
        failure("must not use **kwargs expansion syntax")
    }

    #[must_use]
    pub fn repeated_keyword(name: &str) -> Self {
        failure(&format!("repeat keyword '{name}'"))
    }

    #[must_use]
    pub fn dict_unpacking() -> Self {
        failure("must not use dictionary unpacking")
    }

    #[must_use]
    pub fn dictionary_key() -> Self {
        failure("use an unsupported dictionary key")
    }
}
