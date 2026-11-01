//! Authored `@name(...)` macro expansion: calls are scanned, nested and their arguments evaluated
//! natively, and only the macro bodies run in Python through a callback.

use std::cell::RefCell;
use std::collections::HashSet;

use pyo3::IntoPyObjectExt;
use pyo3::exceptions::PyKeyError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use sqltide_discovery::declarations::{
    ConstantValue, Declarations, EnumValue, load_declarations as load,
};
use sqltide_sqltext::macro_arguments::{
    ArgumentHost, CallArguments, Failure, Node, NumberToken, parse_macro_arguments,
};
use sqltide_sqltext::python_text::{python_isalnum, python_isspace, python_strip};
use sqltide_sqltext::sql_scan::{find_matching_parenthesis, find_next_macro_start};

use crate::config::spec_error;
use crate::sql_scan::raise;

const MACRO_CALL_CONTEXT: &str = "Macro call";
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

/// Parse and validate the `(path, contents)` files under `constants/` and `enums/`.
#[pyfunction]
fn load_declarations(
    py: Python<'_>,
    constant_files: Vec<(String, String)>,
    enum_files: Vec<(String, String)>,
) -> PyResult<PyDeclarations> {
    load(&constant_files, &enum_files)
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

/// One expansion: the known macros and the Python callbacks that run and report them.
struct Expansion<'a, 'py> {
    py: Python<'py>,
    names: HashSet<String>,
    sorted_names: &'a [String],
    declarations: &'a Declarations,
    file_path: &'a str,
    host: UnicodeHost<'py>,
    /// `call(name=, args=, kwargs=, index=)` runs one macro body.
    call: &'a Bound<'py, PyAny>,
    /// `fail(message=, index=, name=)` builds the error for a call at `index`, or unlocated.
    fail: &'a Bound<'py, PyAny>,
    /// `locate(error=, index=, name=)` places an unlocated error at the enclosing call.
    locate: &'a Bound<'py, PyAny>,
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
    fn kwargs(&self, entries: &[(&str, Bound<'py, PyAny>)]) -> PyResult<Bound<'py, PyDict>> {
        let kwargs: Bound<'py, PyDict> = PyDict::new(self.py);
        for (key, value) in entries {
            kwargs.set_item(key, value)?;
        }
        Ok(kwargs)
    }

    fn error(&self, message: &str, index: Option<usize>, name: Option<&str>) -> PyErr {
        let built: PyResult<Bound<'py, PyAny>> = (|| {
            let kwargs: Bound<'py, PyDict> = self.kwargs(&[
                ("message", message.into_bound_py_any(self.py)?),
                ("index", index.into_bound_py_any(self.py)?),
                ("name", name.into_bound_py_any(self.py)?),
            ])?;
            self.fail.call((), Some(&kwargs))
        })();
        built.map_or_else(|error| error, PyErr::from_value)
    }

    fn located(&self, error: PyErr, index: usize, name: Option<&str>) -> PyErr {
        let relocated: PyResult<Bound<'py, PyAny>> = (|| {
            let kwargs: Bound<'py, PyDict> = self.kwargs(&[
                ("error", error.value(self.py).clone().into_any()),
                ("index", index.into_bound_py_any(self.py)?),
                ("name", name.into_bound_py_any(self.py)?),
            ])?;
            self.locate.call((), Some(&kwargs))
        })();
        match relocated {
            Ok(relocated) if relocated.is(error.value(self.py)) => error,
            Ok(relocated) => PyErr::from_value(relocated),
            Err(failure) => failure,
        }
    }

    /// The SQL with every top-level macro call replaced by its result.
    fn expand(&self, sql: &[char]) -> PyResult<String> {
        let mut rendered: String = String::new();
        let mut cursor: usize = 0;
        while cursor < sql.len() {
            let Some(start) = find_next_macro_start(sql, cursor).map_err(raise)? else {
                rendered.extend(&sql[cursor..]);
                break;
            };
            rendered.extend(&sql[cursor..start]);
            let (result, next): (Bound<'py, PyAny>, usize) =
                self.evaluate_call(sql, start, 0, true)?;
            rendered.push_str(&result.str()?.to_cow()?);
            cursor = next;
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
        let call_kwargs: Bound<'py, PyDict> = self.kwargs(&[
            ("name", PyString::new(self.py, &name).into_any()),
            ("args", args.into_any()),
            ("kwargs", kwargs.into_any()),
            ("index", index.into_bound_py_any(self.py)?),
        ])?;
        let result: Bound<'py, PyAny> = self.call.call((), Some(&call_kwargs))?;
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
            if let Some(unexpanded) = find_next_macro_start(&produced, 0).map_err(raise)? {
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
            let Some(start) = find_next_macro_start(text, cursor).map_err(raise)? else {
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

/// Expand every authored macro call and `@const`/`@enum` reference in `sql`. `macro_names` are
/// the known macros, sorted; `call`, `fail` and `locate` are the Python callbacks `Expansion`
/// describes.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn expand_macro_calls<'py>(
    py: Python<'py>,
    sql: &str,
    macro_names: Vec<String>,
    file_path: &str,
    call: &Bound<'py, PyAny>,
    fail: &Bound<'py, PyAny>,
    locate: &Bound<'py, PyAny>,
    declarations: Option<PyRef<'py, PyDeclarations>>,
) -> PyResult<String> {
    let empty: Declarations = Declarations::default();
    let expansion: Expansion<'_, 'py> = Expansion {
        py,
        names: macro_names.iter().cloned().collect(),
        sorted_names: &macro_names,
        declarations: declarations
            .as_ref()
            .map_or(&empty, |declarations| &declarations.declarations),
        file_path,
        host: UnicodeHost {
            unicodedata: py.import("unicodedata")?,
            raised: RefCell::new(None),
        },
        call,
        fail,
        locate,
    };
    expansion.expand(&sql.chars().collect::<Vec<char>>())
}

/// Unique top-level macro call names in encounter order, without `@const` and `@enum`, and the
/// error of a call whose parentheses do not close.
#[pyfunction]
fn find_macro_call_names(sql: &str) -> PyResult<(Vec<String>, Option<String>)> {
    let sql: Vec<char> = sql.chars().collect();
    let mut names: Vec<String> = Vec::new();
    let mut cursor: usize = 0;
    while cursor < sql.len() {
        let Some(start) = find_next_macro_start(&sql, cursor).map_err(raise)? else {
            break;
        };
        let name: String = macro_name(&sql, start);
        let open: usize = skip_whitespace(&sql, start + 1 + name.chars().count());
        if !names.contains(&name) && name != CONSTANT_CALL && name != ENUM_CALL {
            names.push(name);
        }
        if sql.get(open) != Some(&'(') {
            return Ok((names, Some("expected opening parenthesis".to_owned())));
        }
        match find_matching_parenthesis(&sql, open, MACRO_CALL_CONTEXT) {
            Ok(close) => cursor = close + 1,
            Err(error) => return Ok((names, Some(error.message))),
        }
    }
    Ok((names, None))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(expand_macro_calls, module)?)?;
    module.add_function(wrap_pyfunction!(find_macro_call_names, module)?)?;
    module.add_function(wrap_pyfunction!(load_declarations, module)?)?;
    module.add_class::<PyDeclarations>()?;
    Ok(())
}
