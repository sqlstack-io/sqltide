//! Authored `@name(...)` macro expansion: calls are scanned, nested and their arguments evaluated
//! natively, errors are located natively, and only the authored macro bodies run as Python.

use std::cell::RefCell;
use std::collections::HashSet;

use pyo3::IntoPyObjectExt;
use pyo3::exceptions::{PyException, PyKeyError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PySet, PyString, PyTuple};
use sqltide_discovery::declarations::{
    ConstantValue, Declarations, EnumValue, load_declarations as load,
};
use sqltide_sqltext::macro_arguments::{
    ArgumentHost, CallArguments, Failure, Node, NumberToken, parse_macro_arguments,
};
use sqltide_sqltext::python_text::{python_isalnum, python_isspace, python_strip};
use sqltide_sqltext::sql_scan::{find_matching_parenthesis, find_next_macro_start};

use crate::config::spec_error;
use crate::model_sql::{call, chained};
use crate::sql_scan::scan_failure;

const MACRO_CALL_CONTEXT: &str = "Macro call";
const MACRO_SIGIL: char = '@';
const MACRO_CONTEXT_PARAMETER: &str = "ctx";
const MACRO_CONTEXT_ANNOTATION: &str = "MacroContext";
const MACRO_EXCEPTIONS: &str = "sqltide.compiler.macros.exceptions";
const MACRO_MODELS: &str = "sqltide.compiler.macros.models";
const DIAGNOSTICS_MODELS: &str = "sqltide.diagnostics.models";
const MACRO_CONSTANTS: &str = "sqltide.compiler.macros.constants";
const MACRO_FILE_LOADER: &str = "sqltide.compiler.macros.main._load_macro_file";
const CONSTANT_CALL: &str = "const";
const ENUM_CALL: &str = "enum";

/// A project's validated constants and enums, which `@const` and `@enum` read.
#[pyclass(name = "Declarations", module = "sqltide._native", frozen)]
pub(crate) struct PyDeclarations {
    declarations: Declarations,
}

#[pymethods]
impl PyDeclarations {
    /// The rendered SQL of constant `name`, as `@const(name)` expands.
    fn render_constant(&self, py: Python<'_>, name: &str, file_path: &str) -> PyResult<String> {
        self.declarations
            .render_constant(name, file_path)
            .map_err(|error| spec_error(py, error))
    }

    /// The rendered SQL of one enum member, as `@enum(name).MEMBER` expands.
    fn render_enum_member(
        &self,
        py: Python<'_>,
        name: &str,
        member: &str,
        file_path: &str,
    ) -> PyResult<String> {
        self.declarations
            .render_enum_member(name, member, file_path)
            .map_err(|error| spec_error(py, error))
    }
}

/// The `(relative path, contents)` of discovered project files.
fn file_sources(files: &Bound<'_, PyAny>) -> PyResult<Vec<(String, String)>> {
    files
        .try_iter()?
        .map(|file| {
            let file: Bound<'_, PyAny> = file?;
            Ok((
                file.getattr("relative_path")?.str()?.extract()?,
                file.getattr("contents")?.extract()?,
            ))
        })
        .collect()
}

/// Validate every project-wide CONSTANT and ENUM declaration in the discovered files under
/// `constants/` and `enums/` once.
#[pyfunction]
#[pyo3(signature = (*, constant_files, enum_files))]
fn load_declarations(
    py: Python<'_>,
    constant_files: &Bound<'_, PyAny>,
    enum_files: &Bound<'_, PyAny>,
) -> PyResult<PyDeclarations> {
    load(&file_sources(constant_files)?, &file_sources(enum_files)?)
        .map(|declarations| PyDeclarations { declarations })
        .map_err(|error| spec_error(py, error))
}

/// `unicodedata` answers character names and identifier normalization as CPython does.
struct UnicodeHost<'py> {
    unicodedata: Bound<'py, PyModule>,
    /// An unexpected Python error, raised once parsing returns.
    raised: RefCell<Option<PyErr>>,
}

impl UnicodeHost<'_> {
    fn held<T>(&self, result: PyResult<T>) -> Option<T> {
        match result {
            Ok(value) => Some(value),
            Err(error) => {
                self.raised.borrow_mut().get_or_insert(error);
                None
            }
        }
    }
}

impl ArgumentHost for UnicodeHost<'_> {
    fn character_named(&self, name: &str) -> Option<String> {
        let py: Python<'_> = self.unicodedata.py();
        match self.unicodedata.call_method1("lookup", (name,)) {
            Ok(found) => self.held(found.extract::<String>()),
            Err(error) if error.is_instance_of::<PyKeyError>(py) => None,
            Err(error) => self.held(Err(error)),
        }
    }

    fn identifier(&self, text: &str) -> Option<String> {
        let candidate: Bound<'_, PyString> = PyString::new(self.unicodedata.py(), text);
        let valid: bool = self.held(
            candidate
                .call_method0("isidentifier")
                .and_then(|valid| valid.extract()),
        )?;
        if !valid {
            return None;
        }
        self.held(
            self.unicodedata
                .call_method1("normalize", ("NFKC", candidate))
                .and_then(|normalized| normalized.extract()),
        )
    }
}

/// The SQL body being expanded and the registry, context and diagnostic classes its macro calls
/// and errors use.
struct MacroSite<'py> {
    macros: Bound<'py, PyAny>,
    context: Bound<'py, PyAny>,
    path: Bound<'py, PyAny>,
    sql: Vec<char>,
    line: i64,
    column: i64,
    macro_error: Bound<'py, PyAny>,
    macro_context: Bound<'py, PyAny>,
    source_location: Bound<'py, PyAny>,
    related_location: Bound<'py, PyAny>,
    signature: Bound<'py, PyAny>,
}

impl<'py> MacroSite<'py> {
    /// The absolute location of top-level SQL index `index`.
    fn locate(&self, index: usize) -> PyResult<Bound<'py, PyAny>> {
        let before: &[char] = &self.sql[..index.min(self.sql.len())];
        let relative_line: i64 = before
            .iter()
            .filter(|character| **character == '\n')
            .count() as i64;
        let previous_newline: i64 = before
            .iter()
            .rposition(|character| *character == '\n')
            .map_or(-1, |position| position as i64);
        let relative_column: i64 = index as i64 - previous_newline;
        let column: i64 = if relative_line == 0 {
            self.column + relative_column - 1
        } else {
            relative_column
        };
        let py: Python<'py> = self.path.py();
        call(
            &self.source_location,
            &[
                ("path", self.path.clone()),
                ("line", (self.line + relative_line).into_bound_py_any(py)?),
                ("column", column.into_bound_py_any(py)?),
            ],
        )
    }

    /// The `macro definition` related location of a loaded macro.
    fn definition(&self, loaded: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py: Python<'py> = self.path.py();
        let location: Bound<'py, PyAny> = call(
            &self.source_location,
            &[
                ("path", loaded.getattr("file_path")?),
                ("line", loaded.getattr("definition_line")?),
                ("column", 1.into_bound_py_any(py)?),
            ],
        )?;
        let related: Bound<'py, PyAny> = call(
            &self.related_location,
            &[
                ("label", PyString::new(py, "macro definition").into_any()),
                ("location", location),
            ],
        )?;
        Ok(PyTuple::new(py, [related])?.into_any())
    }

    fn related(&self, name: Option<&str>) -> PyResult<Bound<'py, PyAny>> {
        match name {
            Some(name) => self.definition(&self.macros.get_item(name)?),
            None => Ok(PyTuple::empty(self.path.py()).into_any()),
        }
    }

    fn located_error(
        &self,
        message: &str,
        location: Bound<'py, PyAny>,
        related: Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let kwargs: Bound<'py, PyDict> = PyDict::new(self.path.py());
        kwargs.set_item("location", location)?;
        kwargs.set_item("related_locations", related)?;
        self.macro_error.call((message,), Some(&kwargs))
    }

    /// `MacroError(message)`, located at the call at `index` when there is one.
    fn fail(
        &self,
        message: &str,
        index: Option<usize>,
        name: Option<&str>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let Some(index) = index else {
            return self.macro_error.call1((message,));
        };
        let location: Bound<'py, PyAny> = self.locate(index)?;
        self.located_error(message, location, self.related(name)?)
    }

    /// An unlocated `MacroError` placed at the call at `index`, caused by the original.
    fn locate_error(
        &self,
        error: &Bound<'py, PyAny>,
        index: usize,
        name: Option<&str>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if !error.is_instance(&self.macro_error)? || !error.getattr("location")?.is_none() {
            return Ok(error.clone());
        }
        let location: Bound<'py, PyAny> = self.locate(index)?;
        let located: Bound<'py, PyAny> =
            self.located_error(&error.str()?.to_cow()?, location, self.related(name)?)?;
        located.setattr("__cause__", error)?;
        Ok(located)
    }

    /// Run macro `name` with the evaluated arguments; failures other than `MacroError` become a
    /// located `MacroError` caused by the original.
    fn run(
        &self,
        name: &str,
        args: Bound<'py, PyTuple>,
        kwargs: Bound<'py, PyDict>,
        index: usize,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py: Python<'py> = self.path.py();
        let loaded: Bound<'py, PyAny> = self.macros.get_item(name)?;
        let location: Bound<'py, PyAny> = self.locate(index)?;
        let error: PyErr = match self.run_loaded(&loaded, args, kwargs, &location) {
            Ok(result) => return Ok(result),
            Err(error) => error,
        };
        let raised: Bound<'py, PyAny> = error.value(py).clone().into_any();
        let reason: &str = if raised.is_instance(&self.macro_error)? {
            return Err(error);
        } else if error.is_instance_of::<PyTypeError>(py) {
            "could not be called"
        } else if error.is_instance_of::<PyException>(py) {
            "failed"
        } else {
            return Err(error);
        };
        let wrapped: Bound<'py, PyAny> = self.located_error(
            &format!(
                "Macro '@{name}' in '{}' {reason}: {}",
                self.path.str()?,
                raised.str()?
            ),
            location,
            self.definition(&loaded)?,
        )?;
        chained(&wrapped, &raised, Some(&raised))?;
        Err(PyErr::from_value(wrapped))
    }

    fn run_loaded(
        &self,
        loaded: &Bound<'py, PyAny>,
        args: Bound<'py, PyTuple>,
        kwargs: Bound<'py, PyDict>,
        location: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py: Python<'py> = self.path.py();
        let function: Bound<'py, PyAny> = loaded.getattr("function")?;
        let parameters: Bound<'py, PyTuple> = PyTuple::new(
            py,
            self.signature
                .call1((&function,))?
                .getattr("parameters")?
                .call_method0("values")?
                .try_iter()?
                .collect::<PyResult<Vec<_>>>()?,
        )?;
        let first: Option<Bound<'py, PyAny>> = parameters.iter().next();
        let accepts_context: bool = match &first {
            Some(first) => first.getattr("name")?.eq(MACRO_CONTEXT_PARAMETER)?,
            None => false,
        };
        if kwargs.contains(MACRO_CONTEXT_PARAMETER)? {
            return Err(PyErr::from_value(self.located_error(
                &format!(
                    "Macro '@{}' in '{}' reserves keyword 'ctx'",
                    loaded.getattr("name")?.str()?,
                    self.path.str()?
                ),
                location.clone(),
                self.definition(loaded)?,
            )?));
        }
        let Some(first) = first.filter(|_| accepts_context) else {
            return function.call(args, Some(&kwargs));
        };
        let allowed: Bound<'py, PySet> = PySet::new(
            py,
            [
                self.macro_context.clone(),
                PyString::new(py, MACRO_CONTEXT_ANNOTATION).into_any(),
            ],
        )?;
        if !allowed.contains(first.getattr("annotation")?)? {
            return Err(PyErr::from_value(self.located_error(
                &format!(
                    "Macro '@{}' in '{}' must annotate ctx as MacroContext",
                    loaded.getattr("name")?.str()?,
                    loaded.getattr("file_path")?.str()?
                ),
                location.clone(),
                self.definition(loaded)?,
            )?));
        }
        let mut arguments: Vec<Bound<'py, PyAny>> = vec![self.context.clone()];
        arguments.extend(args.iter());
        function.call(PyTuple::new(py, arguments)?, Some(&kwargs))
    }
}

/// One expansion: the known macros and the site whose macro bodies run as Python.
struct Expansion<'a, 'py> {
    py: Python<'py>,
    names: HashSet<String>,
    sorted_names: &'a [String],
    declarations: &'a Declarations,
    file_path: &'a str,
    host: UnicodeHost<'py>,
    site: &'a MacroSite<'py>,
}

/// An argument failure or a Python error raised while building argument values.
enum Evaluation {
    Failure(Failure),
    Raised(PyErr),
}

impl From<PyErr> for Evaluation {
    fn from(error: PyErr) -> Self {
        Self::Raised(error)
    }
}

impl<'py> Expansion<'_, 'py> {
    fn error(&self, message: &str, index: Option<usize>, name: Option<&str>) -> PyErr {
        self.site
            .fail(message, index, name)
            .map_or_else(|error| error, PyErr::from_value)
    }

    fn located(&self, error: PyErr, index: usize, name: Option<&str>) -> PyErr {
        let relocated: PyResult<Bound<'py, PyAny>> =
            self.site
                .locate_error(&error.value(self.py).clone().into_any(), index, name);
        match relocated {
            Ok(relocated) if relocated.is(error.value(self.py)) => error,
            Ok(relocated) => PyErr::from_value(relocated),
            Err(failure) => failure,
        }
    }

    /// The SQL with every top-level macro call replaced by its result; when any call expanded,
    /// `capture(key, (rendered, spans))` records the source map.
    fn expand(
        &self,
        sql: &[char],
        capture: &Bound<'py, PyAny>,
        key: &Bound<'py, PyTuple>,
    ) -> PyResult<String> {
        let mut rendered: String = String::new();
        let mut spans: Vec<(usize, usize, usize, usize)> = Vec::new();
        let mut output_position: usize = 0;
        let mut cursor: usize = 0;
        while cursor < sql.len() {
            let Some(start) = find_next_macro_start(sql, cursor).map_err(scan_failure)? else {
                rendered.extend(&sql[cursor..]);
                break;
            };
            rendered.extend(&sql[cursor..start]);
            output_position += start - cursor;
            let (result, next): (Bound<'py, PyAny>, usize) =
                self.evaluate_call(sql, start, 0, true)?;
            let output = result.str()?;
            let output = output.to_cow()?;
            let output_end = output_position + output.chars().count();
            spans.push((start, next, output_position, output_end));
            rendered.push_str(&output);
            output_position = output_end;
            cursor = next;
        }
        if !spans.is_empty() {
            capture.call1((key, (rendered.clone(), spans)))?;
        }
        Ok(rendered)
    }

    /// The value of the call at `start` of `text`, which begins at `offset` of the top-level
    /// SQL, and the index after its closing parenthesis.
    fn evaluate_call(
        &self,
        text: &[char],
        start: usize,
        offset: usize,
        top_level: bool,
    ) -> PyResult<(Bound<'py, PyAny>, usize)> {
        let name: String = macro_name(text, start);
        let index: usize = offset + start;
        if name == CONSTANT_CALL || name == ENUM_CALL {
            return self.declaration_call(text, (start, offset), top_level, &name);
        }
        if !self.names.contains(&name) {
            let available: String = if self.sorted_names.is_empty() {
                "none".to_owned()
            } else {
                self.sorted_names.join(", ")
            };
            return Err(self.error(
                &format!(
                    "Unknown macro '@{name}' in '{}'. Available macros: {available}",
                    self.file_path
                ),
                Some(index),
                None,
            ));
        }
        let open: usize = skip_whitespace(text, start + 1 + name.chars().count());
        if text.get(open) != Some(&'(') {
            return Err(self.error("expected opening parenthesis", Some(index), Some(&name)));
        }
        let close: usize = find_matching_parenthesis(text, open, MACRO_CALL_CONTEXT)
            .map_err(|error| self.error(&error.message, Some(index), Some(&name)))?;
        let (args, kwargs) = self
            .call_arguments(&text[open + 1..close], offset + open + 1)
            .map_err(|error| self.located(error, index, Some(&name)))?;
        let result: Bound<'py, PyAny> = self.site.run(&name, args, kwargs, index)?;
        if top_level {
            let Ok(sql) = result.downcast::<PyString>() else {
                return Err(self.error(
                    &format!(
                        "Macro '@{name}' in '{}' must return a SQL string when used directly in SQL",
                        self.file_path
                    ),
                    Some(index),
                    Some(&name),
                ));
            };
            let produced: Vec<char> = sql.to_cow()?.chars().collect();
            if let Some(unexpanded) = find_next_macro_start(&produced, 0).map_err(scan_failure)? {
                return Err(self.error(
                    &format!(
                        "Macro expansion in '{}' produced output containing unexpanded macro \
                         call '@{}('. Compose macros in Python instead.",
                        self.file_path,
                        macro_name(&produced, unexpanded)
                    ),
                    Some(index),
                    Some(&name),
                ));
            }
        } else if let Some(message) = nested_result_problem(&result, self.file_path)? {
            return Err(self.error(&message, Some(index), Some(&name)));
        }
        Ok((result, close + 1))
    }

    /// `@const("name")` or `@enum("name").MEMBER` at `start`: rendered SQL at the top level,
    /// or the declared value as an argument of another macro.
    fn declaration_call(
        &self,
        text: &[char],
        (start, offset): (usize, usize),
        top_level: bool,
        name: &str,
    ) -> PyResult<(Bound<'py, PyAny>, usize)> {
        let index: usize = offset + start;
        let open: usize = skip_whitespace(text, start + 1 + name.chars().count());
        let close: usize = find_matching_parenthesis(text, open, MACRO_CALL_CONTEXT)
            .map_err(|error| self.error(&error.message, Some(index), None))?;
        let (args, kwargs) = self
            .call_arguments(&text[open + 1..close], offset + open + 1)
            .map_err(|error| self.located(error, index, None))?;
        let kind: &str = if name == CONSTANT_CALL {
            "constant"
        } else {
            "enum"
        };
        let declared: Option<String> = (kwargs.is_empty() && args.len() == 1)
            .then(|| args.get_item(0).ok()?.downcast_into::<PyString>().ok())
            .flatten()
            .map(|declared| declared.to_string());
        let Some(declared) = declared else {
            return Err(self.error(
                &format!(
                    "@{name}(...) in '{}' takes one quoted {kind} name",
                    self.file_path
                ),
                Some(index),
                None,
            ));
        };
        let failed = |error: sqltide_discovery::errors::SpecError| {
            self.error(&error.message, Some(index), None)
        };
        if name == CONSTANT_CALL {
            let value: Bound<'py, PyAny> = if top_level {
                let rendered: String = self
                    .declarations
                    .render_constant(&declared, self.file_path)
                    .map_err(failed)?;
                PyString::new(self.py, &rendered).into_any()
            } else {
                let constant = self
                    .declarations
                    .constant(&declared, self.file_path)
                    .map_err(failed)?;
                constant_object(self.py, &constant.value)?
            };
            return Ok((value, close + 1));
        }
        let member: String = if text.get(close + 1) == Some(&'.') {
            text[close + 2..]
                .iter()
                .take_while(|character| character.is_ascii_alphanumeric() || **character == '_')
                .collect()
        } else {
            String::new()
        };
        if member.is_empty() {
            return Err(self.error(
                &format!(
                    "@enum(\"{declared}\") in '{}' must be followed by .MEMBER",
                    self.file_path
                ),
                Some(index),
                None,
            ));
        }
        let next: usize = close + 2 + member.chars().count();
        let value: Bound<'py, PyAny> = if top_level {
            let rendered: String = self
                .declarations
                .render_enum_member(&declared, &member, self.file_path)
                .map_err(failed)?;
            PyString::new(self.py, &rendered).into_any()
        } else {
            match self
                .declarations
                .enum_member(&declared, &member, self.file_path)
                .map_err(failed)?
            {
                EnumValue::Text(text) => PyString::new(self.py, text).into_any(),
                EnumValue::Integer(number) => number.into_bound_py_any(self.py)?,
            }
        };
        Ok((value, next))
    }

    /// The positional and keyword values of one call's argument text, nested calls first.
    fn call_arguments(
        &self,
        text: &[char],
        offset: usize,
    ) -> PyResult<(Bound<'py, PyTuple>, Bound<'py, PyDict>)> {
        if python_strip(text).is_empty() {
            return Ok((PyTuple::empty(self.py), PyDict::new(self.py)));
        }
        let mut spans: Vec<(usize, usize)> = Vec::new();
        let mut values: Vec<Bound<'py, PyAny>> = Vec::new();
        let mut cursor: usize = 0;
        while cursor < text.len() {
            let Some(start) = find_next_macro_start(text, cursor).map_err(scan_failure)? else {
                break;
            };
            let (value, next) = self.evaluate_call(text, start, offset, false)?;
            spans.push((start, next));
            values.push(value);
            cursor = next;
        }
        let parsed: Result<CallArguments, Failure> =
            parse_macro_arguments(&self.host, text, &spans);
        if let Some(error) = self.host.raised.borrow_mut().take() {
            return Err(error);
        }
        let evaluated: Result<_, Evaluation> = parsed
            .map_err(Evaluation::Failure)
            .and_then(|arguments| self.arguments(&arguments, &values));
        evaluated.map_err(|failure| match failure {
            Evaluation::Raised(error) => error,
            Evaluation::Failure(failure) => self.error(
                &format!("Macro arguments in '{}' {}", self.file_path, failure.detail),
                None,
                None,
            ),
        })
    }

    fn arguments(
        &self,
        arguments: &CallArguments,
        nested: &[Bound<'py, PyAny>],
    ) -> Result<(Bound<'py, PyTuple>, Bound<'py, PyDict>), Evaluation> {
        let mut positional: Vec<Bound<'py, PyAny>> = Vec::with_capacity(arguments.args.len());
        for node in &arguments.args {
            positional.push(self.value(node, nested)?);
        }
        let kwargs: Bound<'py, PyDict> = PyDict::new(self.py);
        for keyword in &arguments.keywords {
            let Some(name) = &keyword.name else {
                return Err(Evaluation::Failure(Failure::keyword_expansion()));
            };
            if kwargs.contains(name)? {
                return Err(Evaluation::Failure(Failure::repeated_keyword(name)));
            }
            kwargs.set_item(name, self.value(&keyword.value, nested)?)?;
        }
        Ok((PyTuple::new(self.py, positional)?, kwargs))
    }

    /// One node as the Python object `ast` evaluation builds, in Python's evaluation order.
    fn value(
        &self,
        node: &Node,
        nested: &[Bound<'py, PyAny>],
    ) -> Result<Bound<'py, PyAny>, Evaluation> {
        let py: Python<'py> = self.py;
        Ok(match node {
            Node::Str {
                formatted: true, ..
            } => return Err(Evaluation::Failure(Failure::unsupported())),
            Node::Str { bytes: true, .. } | Node::Number(NumberToken::Complex) | Node::Ellipsis => {
                return Err(Evaluation::Failure(Failure::unsupported_literal()));
            }
            Node::Str { value, .. } => python_string(py, value)?,
            Node::Number(NumberToken::Int { radix, digits }) => {
                py.get_type::<PyInt>().call1((digits.as_str(), *radix))?
            }
            Node::Number(NumberToken::Float(text)) => {
                py.get_type::<PyFloat>().call1((text.as_str(),))?
            }
            Node::Name(name) => match name.as_str() {
                "True" => PyBool::new(py, true).to_owned().into_any(),
                "False" => PyBool::new(py, false).to_owned().into_any(),
                "None" => py.None().into_bound(py),
                _ => return Err(Evaluation::Failure(Failure::unsupported())),
            },
            Node::Nested(call) => nested[*call].clone(),
            Node::Call | Node::Set | Node::Starred => {
                return Err(Evaluation::Failure(Failure::unsupported()));
            }
            Node::List(items) => {
                let list: Bound<'py, PyList> = PyList::empty(py);
                for item in items {
                    list.append(self.value(item, nested)?)?;
                }
                list.into_any()
            }
            Node::Tuple(items) => {
                let mut values: Vec<Bound<'py, PyAny>> = Vec::with_capacity(items.len());
                for item in items {
                    values.push(self.value(item, nested)?);
                }
                PyTuple::new(py, values)?.into_any()
            }
            Node::Dict(pairs) => {
                let dict: Bound<'py, PyDict> = PyDict::new(py);
                for (key, item) in pairs {
                    let Some(key) = key else {
                        return Err(Evaluation::Failure(Failure::dict_unpacking()));
                    };
                    let key: Bound<'py, PyAny> = self.value(key, nested)?;
                    if !is_scalar(&key) {
                        return Err(Evaluation::Failure(Failure::dictionary_key()));
                    }
                    dict.set_item(key, self.value(item, nested)?)?;
                }
                dict.into_any()
            }
            Node::Unary { negative, operand } => {
                let operand: Bound<'py, PyAny> = self.value(operand, nested)?;
                if operand.is_instance_of::<PyBool>()
                    || !(operand.is_instance_of::<PyInt>() || operand.is_instance_of::<PyFloat>())
                {
                    return Err(Evaluation::Failure(Failure::unary()));
                }
                if *negative { operand.neg()? } else { operand }
            }
        })
    }
}

/// A constant as the Python value macros receive: sets as tuples in canonical order, and
/// decimals as `decimal.Decimal`.
fn constant_object<'py>(py: Python<'py>, value: &ConstantValue) -> PyResult<Bound<'py, PyAny>> {
    Ok(match value {
        ConstantValue::Null => py.None().into_bound(py),
        ConstantValue::Boolean(flag) => PyBool::new(py, *flag).to_owned().into_any(),
        ConstantValue::Integer(number) => number.into_bound_py_any(py)?,
        ConstantValue::Float(number) => number.into_bound_py_any(py)?,
        ConstantValue::Decimal(text) => py.import("decimal")?.getattr("Decimal")?.call1((text,))?,
        ConstantValue::String(text) => PyString::new(py, text).into_any(),
        ConstantValue::List(items) => PyList::new(
            py,
            items
                .iter()
                .map(|item| constant_object(py, item))
                .collect::<PyResult<Vec<_>>>()?,
        )?
        .into_any(),
        ConstantValue::Set(items) => PyTuple::new(
            py,
            items
                .iter()
                .map(|item| constant_object(py, item))
                .collect::<PyResult<Vec<_>>>()?,
        )?
        .into_any(),
        ConstantValue::Object(entries) => {
            let object: Bound<'py, PyDict> = PyDict::new(py);
            for (key, item) in entries {
                object.set_item(key, constant_object(py, item)?)?;
            }
            object.into_any()
        }
    })
}

/// The string of `codes`, built by Python's `chr` when it holds lone surrogates.
fn python_string<'py>(py: Python<'py>, codes: &[u32]) -> PyResult<Bound<'py, PyAny>> {
    let text: Option<String> = codes.iter().map(|code| char::from_u32(*code)).collect();
    if let Some(text) = text {
        return Ok(PyString::new(py, &text).into_any());
    }
    let characters: Vec<Bound<'py, PyAny>> = codes
        .iter()
        .map(|code| py.import("builtins")?.getattr("chr")?.call1((*code,)))
        .collect::<PyResult<_>>()?;
    PyString::new(py, "").call_method1("join", (characters,))
}

/// Python's `isinstance(value, str | bool | int | float)` or `None`.
fn is_scalar(value: &Bound<'_, PyAny>) -> bool {
    value.is_none()
        || value.is_instance_of::<PyString>()
        || value.is_instance_of::<PyInt>()
        || value.is_instance_of::<PyFloat>()
}

/// Why a nested macro's result cannot be an argument value, or `None` when it can.
fn nested_result_problem(result: &Bound<'_, PyAny>, file_path: &str) -> PyResult<Option<String>> {
    if is_scalar(result) {
        return Ok(None);
    }
    if result.is_instance_of::<PyList>() || result.is_instance_of::<PyTuple>() {
        for item in result.try_iter()? {
            if let Some(problem) = nested_result_problem(&item?, file_path)? {
                return Ok(Some(problem));
            }
        }
        return Ok(None);
    }
    if let Ok(mapping) = result.downcast::<PyDict>() {
        for (key, value) in mapping
            .call_method0("items")?
            .try_iter()?
            .map(|item| {
                item.and_then(|pair| pair.extract::<(Bound<'_, PyAny>, Bound<'_, PyAny>)>())
            })
            .collect::<PyResult<Vec<_>>>()?
        {
            if !is_scalar(&key) {
                return Ok(Some(format!(
                    "Nested macro result in '{file_path}' contains an unsupported dictionary key"
                )));
            }
            if let Some(problem) = nested_result_problem(&value, file_path)? {
                return Ok(Some(problem));
            }
        }
        return Ok(None);
    }
    Ok(Some(format!(
        "Nested macro result in '{file_path}' uses an unsupported literal value"
    )))
}

fn is_name_character(character: char) -> bool {
    character == '_' || python_isalnum(character)
}

fn macro_name(text: &[char], start: usize) -> String {
    text[start + 1..]
        .iter()
        .take_while(|character| is_name_character(**character))
        .collect()
}

fn skip_whitespace(text: &[char], start: usize) -> usize {
    text[start..]
        .iter()
        .position(|character| !python_isspace(*character))
        .map_or(text.len(), |offset| start + offset)
}

/// Expand every authored macro call and `@const`/`@enum` reference in `sql`, a SQL body that
/// starts at `source_line:source_column` of `file_path`, running macros from `registry` with
/// `context` and recording the expansion source map in `registry.expansions`.
pub(crate) fn expand_sql_macros_with<'py>(
    sql: &Bound<'py, PyString>,
    file_path: &Bound<'py, PyAny>,
    registry: &Bound<'py, PyAny>,
    context: &Bound<'py, PyAny>,
    source_line: &Bound<'py, PyAny>,
    source_column: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyString>> {
    let py: Python<'py> = sql.py();
    let text: String = sql.to_cow()?.into_owned();
    if !text.contains(MACRO_SIGIL) {
        return Ok(sql.clone());
    }
    let macros: Bound<'py, PyAny> = registry.getattr("macros")?;
    let macro_names: Vec<String> = py
        .import("builtins")?
        .getattr("sorted")?
        .call1((&macros,))?
        .extract()?;
    let declarations: Option<PyRef<'py, PyDeclarations>> =
        registry.getattr("declarations")?.extract()?;
    let capture_key: Bound<'py, PyTuple> = PyTuple::new(
        py,
        [
            file_path.clone(),
            sql.clone().into_any(),
            source_line.clone(),
            source_column.clone(),
        ],
    )?;
    let capture: Bound<'py, PyAny> = registry.getattr("expansions")?.getattr("__setitem__")?;
    let diagnostics: Bound<'py, PyModule> = py.import(DIAGNOSTICS_MODELS)?;
    let site: MacroSite<'py> = MacroSite {
        macros,
        context: context.clone(),
        path: file_path.clone(),
        sql: text.chars().collect(),
        line: source_line.extract()?,
        column: source_column.extract()?,
        macro_error: py.import(MACRO_EXCEPTIONS)?.getattr("MacroError")?,
        macro_context: py.import(MACRO_MODELS)?.getattr("MacroContext")?,
        source_location: diagnostics.getattr("SourceLocation")?,
        related_location: diagnostics.getattr("RelatedDiagnosticLocation")?,
        signature: py.import("inspect")?.getattr("signature")?,
    };
    let path: String = file_path.str()?.to_cow()?.into_owned();
    let empty: Declarations = Declarations::default();
    let expansion: Expansion<'_, 'py> = Expansion {
        py,
        names: macro_names.iter().cloned().collect(),
        sorted_names: &macro_names,
        declarations: declarations
            .as_ref()
            .map_or(&empty, |declarations| &declarations.declarations),
        file_path: &path,
        host: UnicodeHost {
            unicodedata: py.import("unicodedata")?,
            raised: RefCell::new(None),
        },
        site: &site,
    };
    let rendered: String = expansion.expand(&site.sql, &capture, &capture_key)?;
    Ok(PyString::new(py, &rendered))
}

/// `expand_sql_macros_with` for Python callers.
#[pyfunction]
#[pyo3(signature = (*, sql, file_path, registry, context, source_line=None, source_column=None))]
fn expand_sql_macros<'py>(
    py: Python<'py>,
    sql: &Bound<'py, PyString>,
    file_path: &Bound<'py, PyAny>,
    registry: &Bound<'py, PyAny>,
    context: &Bound<'py, PyAny>,
    source_line: Option<Bound<'py, PyAny>>,
    source_column: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyString>> {
    let one = || 1.into_bound_py_any(py);
    expand_sql_macros_with(
        sql,
        file_path,
        registry,
        context,
        &source_line.map_or_else(one, Ok)?,
        &source_column.map_or_else(one, Ok)?,
    )
}

/// Unique top-level macro call names in encounter order, without `@const` and `@enum`; a call
/// whose parentheses do not close raises `MacroError`.
#[pyfunction]
pub(crate) fn find_macro_call_names<'py>(
    py: Python<'py>,
    sql: &str,
) -> PyResult<Bound<'py, PyTuple>> {
    if !sql.contains(MACRO_SIGIL) {
        return Ok(PyTuple::empty(py));
    }
    let sql: Vec<char> = sql.chars().collect();
    let mut names: Vec<String> = Vec::new();
    let mut cursor: usize = 0;
    let failure = |message: String| -> PyErr {
        py.import(MACRO_EXCEPTIONS)
            .and_then(|module| module.getattr("MacroError"))
            .and_then(|class| class.call1((message,)))
            .map_or_else(|error| error, PyErr::from_value)
    };
    while cursor < sql.len() {
        let Some(start) = find_next_macro_start(&sql, cursor).map_err(scan_failure)? else {
            break;
        };
        let name: String = macro_name(&sql, start);
        let open: usize = skip_whitespace(&sql, start + 1 + name.chars().count());
        if !names.contains(&name) && name != CONSTANT_CALL && name != ENUM_CALL {
            names.push(name);
        }
        if sql.get(open) != Some(&'(') {
            return Err(failure("expected opening parenthesis".to_owned()));
        }
        match find_matching_parenthesis(&sql, open, MACRO_CALL_CONTEXT) {
            Ok(close) => cursor = close + 1,
            Err(error) => return Err(failure(error.message)),
        }
    }
    PyTuple::new(py, names)
}

fn keywords<'py>(
    py: Python<'py>,
    entries: &[(&str, Bound<'py, PyAny>)],
) -> PyResult<Bound<'py, PyDict>> {
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    for (name, value) in entries {
        keywords.set_item(name, value)?;
    }
    Ok(keywords)
}

/// Where one loaded macro is defined: its file and `def` line.
fn definition_location<'py>(loaded_macro: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = loaded_macro.py();
    py.import(DIAGNOSTICS_MODELS)?
        .getattr("SourceLocation")?
        .call(
            (),
            Some(&keywords(
                py,
                &[
                    ("path", loaded_macro.getattr("file_path")?),
                    ("line", loaded_macro.getattr("definition_line")?),
                    ("column", 1.into_bound_py_any(py)?),
                ],
            )?),
        )
}

/// One deterministic registry of the public macros of every retained macro file, loaded in
/// order. A reserved macro name or a name defined twice raises `MacroError` at its definition.
#[pyfunction]
#[pyo3(signature = (*, macro_files))]
pub(crate) fn load_macro_registry<'py>(
    py: Python<'py>,
    macro_files: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let load_file: Bound<'py, PyAny> = py.import(MACRO_FILE_LOADER)?.getattr("load_macro_file")?;
    let reserved: Bound<'py, PyAny> = py
        .import(MACRO_CONSTANTS)?
        .getattr("RESERVED_MACRO_NAMES")?;
    let error_class: Bound<'py, PyAny> = py.import(MACRO_EXCEPTIONS)?.getattr("MacroError")?;
    let loaded_macros: Bound<'py, PyDict> = PyDict::new(py);
    for macro_file in macro_files.try_iter()? {
        let macro_file: Bound<'py, PyAny> = macro_file?;
        let file_path: String = macro_file.getattr("file_path")?.str()?.extract()?;
        let file_macros: Bound<'py, PyAny> = load_file.call(
            (),
            Some(&keywords(py, &[("macro_file", macro_file.clone())])?),
        )?;
        for loaded_macro in file_macros.try_iter()? {
            let loaded_macro: Bound<'py, PyAny> = loaded_macro?;
            let name: Bound<'py, PyAny> = loaded_macro.getattr("name")?;
            if reserved.contains(&name)? {
                return Err(PyErr::from_value(error_class.call(
                    (format!(
                        "Macro '{}' in '{file_path}' uses a reserved name; @const and @enum \
                         read project constants and enums",
                        name.str()?
                    ),),
                    Some(&keywords(
                        py,
                        &[("location", definition_location(&loaded_macro)?)],
                    )?),
                )?));
            }
            if let Some(existing) = loaded_macros.get_item(&name)? {
                let related: Bound<'py, PyAny> = py
                    .import(DIAGNOSTICS_MODELS)?
                    .getattr("RelatedDiagnosticLocation")?
                    .call(
                        (),
                        Some(&keywords(
                            py,
                            &[
                                ("label", "first macro definition".into_bound_py_any(py)?),
                                ("location", definition_location(&existing)?),
                            ],
                        )?),
                    )?;
                return Err(PyErr::from_value(error_class.call(
                    (format!(
                        "Macro name collision for '{}' in '{file_path}' and '{}'",
                        name.str()?,
                        existing.getattr("file_path")?.str()?
                    ),),
                    Some(&keywords(
                        py,
                        &[
                            ("location", definition_location(&loaded_macro)?),
                            ("related_locations", PyTuple::new(py, [related])?.into_any()),
                        ],
                    )?),
                )?));
            }
            loaded_macros.set_item(&name, &loaded_macro)?;
        }
    }
    py.import(MACRO_MODELS)?.getattr("MacroRegistry")?.call(
        (),
        Some(&keywords(py, &[("macros", loaded_macros.into_any())])?),
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(load_macro_registry, module)?)?;
    module.add_function(wrap_pyfunction!(expand_sql_macros, module)?)?;
    module.add_function(wrap_pyfunction!(find_macro_call_names, module)?)?;
    module.add_function(wrap_pyfunction!(load_declarations, module)?)?;
    module.add_class::<PyDeclarations>()?;
    Ok(())
}
