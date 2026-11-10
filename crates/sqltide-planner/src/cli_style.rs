//! Semantic ANSI styling for human CLI output.

const RESET: &str = "\x1b[0m";
const BOLD: &str = "\x1b[1m";
const DIM: &str = "\x1b[2m";
const BLUE: &str = "\x1b[34m";
const GREEN: &str = "\x1b[32m";
const YELLOW: &str = "\x1b[33m";
const RED: &str = "\x1b[31m";

/// Styles text by its role, or leaves it plain when colour is off.
#[derive(Clone, Copy, Debug)]
pub struct CliStyle {
    pub use_color: bool,
}

impl CliStyle {
    /// `text` between `prefix` and the reset, when colour is on and there is text to style.
    fn apply(self, prefix: &str, text: &str) -> String {
        if !self.use_color || prefix.is_empty() || text.is_empty() {
            return text.to_owned();
        }
        format!("{prefix}{text}{RESET}")
    }

    /// A top-level title.
    pub fn title(self, text: &str) -> String {
        self.apply(&format!("{BOLD}{BLUE}"), text)
    }

    /// A section heading, which carries its own colon.
    pub fn section(self, text: &str) -> String {
        self.apply(&format!("{BOLD}{BLUE}"), &format!("{text}:"))
    }

    /// A subsection heading.
    pub fn subsection(self, text: &str) -> String {
        self.apply(BOLD, text)
    }

    /// A field label.
    pub fn label(self, text: &str) -> String {
        self.apply(DIM, text)
    }

    /// A styled label beside its unstyled value.
    pub fn label_value(self, label: &str, value: &str) -> String {
        format!("{}: {value}", self.label(label))
    }

    /// A warehouse object name.
    pub fn object_name(self, text: &str) -> String {
        self.apply(BOLD, text)
    }

    /// De-emphasised text.
    pub fn muted(self, text: &str) -> String {
        self.apply(DIM, text)
    }

    /// Warning text.
    pub fn warning(self, text: &str) -> String {
        self.apply(YELLOW, text)
    }

    /// De-emphasised heading text.
    pub fn muted_strong(self, text: &str) -> String {
        self.apply(&format!("{DIM}{BOLD}"), text)
    }

    /// Text representing a passing result.
    pub fn passed(self, text: &str) -> String {
        self.apply(GREEN, text)
    }

    /// Text representing a failing result.
    pub fn failed(self, text: &str) -> String {
        self.apply(RED, text)
    }

    /// An outcome word, strong green when it passed and strong red when it did not.
    pub fn outcome(self, text: &str, passed: bool) -> String {
        if passed {
            self.apply(&format!("{GREEN}{BOLD}"), text)
        } else {
            self.apply(&format!("{RED}{BOLD}"), text)
        }
    }

    /// Text coloured by an audit assessment (`ready`, `not_ready`, else caution), optionally bold.
    pub fn assessment_value(self, text: &str, assessment: &str, bold: bool) -> String {
        let colour: &str = match assessment {
            "ready" => GREEN,
            "not_ready" => RED,
            _ => YELLOW,
        };
        let weight: &str = if bold { BOLD } else { "" };
        self.apply(&format!("{weight}{colour}"), text)
    }

    /// One unified-diff line, styled by its leading marker.
    pub fn diff_line(self, line: &str) -> String {
        if ["+++", "---", "@@"]
            .iter()
            .any(|prefix| line.starts_with(prefix))
        {
            self.apply(DIM, line)
        } else if line.starts_with('+') {
            self.apply(GREEN, line)
        } else if line.starts_with('-') {
            self.apply(RED, line)
        } else {
            line.to_owned()
        }
    }
}
