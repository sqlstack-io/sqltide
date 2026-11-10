//! The terminal's colour decision and the `CliStyle` facade Python-facing renderers call.

use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};
use sqltide_planner::cli_style::CliStyle as Style;

use crate::dev_server::support::attr;

const TRUTHY_ENV_VALUES: [&str; 3] = ["1", "true", "yes"];
const AUDIT_TYPES: &str = "sqltide.executor.readiness.types";

/// `os.getenv(name)`.
fn environment_value(py: Python<'_>, name: &str) -> PyResult<Option<String>> {
    attr(py, "os", "environ")?
        .call_method1("get", (name,))?
        .extract()
}

/// Colour is off under `NO_COLOR`, forced on by `CLICOLOR_FORCE` or `FORCE_COLOR`, and otherwise
/// on exactly when `sys.stdout` is a terminal.
pub(crate) fn color_enabled(py: Python<'_>) -> PyResult<bool> {
    if environment_value(py, "NO_COLOR")?.is_some_and(|value| !value.is_empty()) {
        return Ok(false);
    }
    for name in ["CLICOLOR_FORCE", "FORCE_COLOR"] {
        if environment_value(py, name)?.is_some_and(|value| TRUTHY_ENV_VALUES.contains(&&*value)) {
            return Ok(true);
        }
    }
    attr(py, "sys", "stdout")?
        .call_method0("isatty")?
        .is_truthy()
}

/// The native style for the terminal as it is now.
pub(crate) fn terminal_style(py: Python<'_>) -> PyResult<Style> {
    Ok(Style {
        use_color: color_enabled(py)?,
    })
}

/// Semantic CLI styling facade used by human-output formatters.
#[pyclass(module = "sqltide._native")]
pub(crate) struct CliStyle {
    #[pyo3(get, set)]
    use_color: bool,
}

impl CliStyle {
    fn style(&self) -> Style {
        Style {
            use_color: self.use_color,
        }
    }

    /// `text` styled by `render` when colour is on and `text` is truthy, else `text` itself.
    fn apply<'py>(
        &self,
        text: &Bound<'py, PyAny>,
        render: impl FnOnce(Style, &str) -> String,
    ) -> PyResult<Bound<'py, PyAny>> {
        if !self.use_color || !text.is_truthy()? {
            return Ok(text.clone());
        }
        let plain: String = text.str()?.to_str()?.to_owned();
        Ok(PyString::new(text.py(), &render(self.style(), &plain)).into_any())
    }
}

#[pymethods]
impl CliStyle {
    #[new]
    #[pyo3(signature = (*, use_color))]
    fn new(use_color: bool) -> Self {
        Self { use_color }
    }

    /// Style a top-level title.
    fn title<'py>(&self, text: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.apply(text, Style::title)
    }

    /// Style a section heading, which carries its own colon.
    fn section(&self, text: &Bound<'_, PyAny>) -> PyResult<String> {
        Ok(self.style().section(text.str()?.to_str()?))
    }

    /// Style a subsection heading.
    fn subsection<'py>(&self, text: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.apply(text, Style::subsection)
    }

    /// Style a field label.
    fn label<'py>(&self, text: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.apply(text, Style::label)
    }

    /// Render a styled label beside its unstyled value.
    #[pyo3(signature = (*, label, value))]
    fn label_value(&self, label: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>) -> PyResult<String> {
        Ok(format!("{}: {}", self.label(label)?.str()?, value.str()?))
    }

    /// Style a warehouse object name, optionally coloured by assessment.
    #[pyo3(signature = (*, text, assessment=None))]
    fn object_name<'py>(
        &self,
        text: &Bound<'py, PyAny>,
        assessment: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        match assessment {
            None => self.apply(text, Style::object_name),
            Some(assessment) => self.assessment_value(text, assessment, true),
        }
    }

    /// Style warning text.
    fn warning<'py>(&self, text: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.apply(text, Style::warning)
    }

    /// Style an assessment word using the assessment it names.
    fn assessment<'py>(&self, text: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let assessment: Bound<'py, PyAny> =
            attr(text.py(), AUDIT_TYPES, "AuditAssessment")?.call1((text,))?;
        self.assessment_value(text, &assessment, false)
    }

    /// Style text according to an audit assessment.
    #[pyo3(signature = (*, text, assessment, bold=false))]
    fn assessment_value<'py>(
        &self,
        text: &Bound<'py, PyAny>,
        assessment: &Bound<'py, PyAny>,
        bold: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let assessment: String = assessment.str()?.to_str()?.to_owned();
        self.apply(text, |style, text| {
            style.assessment_value(text, &assessment, bold)
        })
    }

    /// Style unified-diff lines by their leading marker.
    fn diff_lines<'py>(
        &self,
        py: Python<'py>,
        diff_lines: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyList>> {
        let mut styled: Vec<String> = Vec::new();
        for line in diff_lines.try_iter()? {
            styled.push(self.style().diff_line(&line?.extract::<String>()?));
        }
        PyList::new(py, styled)
    }

    /// Style de-emphasised text.
    fn muted<'py>(&self, text: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.apply(text, Style::muted)
    }

    /// Style de-emphasised heading text.
    fn muted_strong<'py>(&self, text: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.apply(text, Style::muted_strong)
    }

    /// Style text representing a passing result.
    fn passed<'py>(&self, text: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.apply(text, Style::passed)
    }

    /// Style text representing a failing result.
    fn failed<'py>(&self, text: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.apply(text, Style::failed)
    }

    /// Style an outcome word by whether it passed.
    #[pyo3(signature = (*, text, passed))]
    fn outcome<'py>(
        &self,
        text: &Bound<'py, PyAny>,
        passed: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let passed: bool = passed.is_truthy()?;
        self.apply(text, |style, text| style.outcome(text, passed))
    }
}

/// Return a style facade reflecting whether colour is currently enabled.
#[pyfunction]
fn cli_style(py: Python<'_>) -> PyResult<CliStyle> {
    Ok(CliStyle {
        use_color: color_enabled(py)?,
    })
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<CliStyle>()?;
    module.add_function(wrap_pyfunction!(cli_style, module)?)
}
