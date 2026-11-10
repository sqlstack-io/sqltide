//! `sqt`'s argument parsing: the native parser with Python's converters, `repr` and streams.

use pyo3::exceptions::{PyAttributeError, PyOSError, PySystemExit, PyValueError};
use pyo3::prelude::{Bound, Py, PyAny, PyAnyMethods, PyModule, PyModuleMethods, PyResult, Python};
use pyo3::types::{PyBool, PyDict, PyDictMethods, PyInt, PyList, PyListMethods, PyString};
use pyo3::{pyfunction, wrap_pyfunction};
use sqltide_cli::argv::main::parse_cli_arguments::parse_cli_arguments as parse_arguments;
use sqltide_cli::argv::models::{ArgvOutcome, ArgvSettings, Stream, TypeFailure, Value};
use sqltide_cli::argv::types::ArgvHost;

use crate::cli::surrogates::{python_text, rust_text, uses_placeholders};

const INT_CONVERTER: &str = "int";
const VARS_CONVERTER: &str = "parse_cli_vars";
const DISTRIBUTION: &str = "sqltide";

/// The interpreter side of a parse: converters, `repr` and the installed version.
struct PythonHost<'py> {
    py: Python<'py>,
    objects: Vec<Py<PyAny>>,
    /// Whether command-line text carries lone surrogates as placeholders.
    surrogates: bool,
}

impl<'py> PythonHost<'py> {
    /// Rust text as the Python `str` it came from.
    fn text(&self, text: &str) -> PyResult<Bound<'py, PyAny>> {
        python_text(self.py, text, self.surrogates)
    }

    fn keep(&mut self, object: Bound<'_, PyAny>) -> Value {
        self.objects.push(object.unbind());
        Value::Host(self.objects.len() - 1)
    }

    /// `_parse_cli_vars`: a JSON object, or the `ArgumentTypeError` it raised.
    fn vars(&mut self, text: &str) -> PyResult<Result<Value, TypeFailure>> {
        let json: Bound<'_, PyModule> = self.py.import("json")?;
        let parsed: Bound<'_, PyAny> = match json.getattr("loads")?.call1((self.text(text)?,)) {
            Ok(parsed) => parsed,
            Err(error) => {
                if !error
                    .value(self.py)
                    .is_instance(&json.getattr("JSONDecodeError")?)?
                {
                    return Err(error);
                }
                let message: String = error.value(self.py).getattr("msg")?.extract()?;
                return Ok(Err(TypeFailure::ArgumentType(format!(
                    "--vars must be valid JSON: {message}"
                ))));
            }
        };
        let Ok(parsed) = parsed.downcast_into::<PyDict>() else {
            return Ok(Err(TypeFailure::ArgumentType(
                "--vars must be a JSON object".to_owned(),
            )));
        };
        let object: Bound<'_, PyDict> = PyDict::new(self.py);
        for (key, item) in parsed.iter() {
            object.set_item(key.str()?, item)?;
        }
        Ok(Ok(self.keep(object.into_any())))
    }
}

impl ArgvHost for PythonHost<'_> {
    fn convert(&mut self, converter: &str, text: &str) -> Result<Value, TypeFailure> {
        match converter {
            INT_CONVERTER => {
                let number: Bound<'_, PyAny> = self
                    .text(text)
                    .and_then(|text| self.py.get_type::<PyInt>().call1((text,)))
                    .map_err(|_| TypeFailure::Invalid)?;
                Ok(self.keep(number))
            }
            VARS_CONVERTER => self.vars(text).unwrap_or(Err(TypeFailure::Invalid)),
            _ => Err(TypeFailure::Invalid),
        }
    }

    fn repr(&self, text: &str) -> String {
        self.text(text)
            .and_then(|text| text.repr())
            .map(|repr| repr.to_string())
            .unwrap_or_default()
    }

    fn program_version(&mut self) -> String {
        self.py
            .import("importlib.metadata")
            .and_then(|metadata| metadata.getattr("version")?.call1((DISTRIBUTION,)))
            .and_then(|version| version.extract())
            .unwrap_or_default()
    }
}

/// A parse value as the Python object `argparse` would have stored.
fn object_of(py: Python<'_>, host: &PythonHost<'_>, value: Value) -> PyResult<Py<PyAny>> {
    Ok(match value {
        Value::None => py.None(),
        Value::Bool(flag) => PyBool::new(py, flag).to_owned().into_any().unbind(),
        Value::Int(text) => py.get_type::<PyInt>().call1((text,))?.unbind(),
        Value::Str(text) => host.text(&text)?.unbind(),
        Value::Path(text) => py
            .import("pathlib")?
            .getattr("Path")?
            .call1((host.text(&text)?,))?
            .unbind(),
        Value::List(items) => {
            let list: Bound<'_, PyList> = PyList::empty(py);
            for item in items {
                list.append(object_of(py, host, item)?)?;
            }
            list.into_any().unbind()
        }
        Value::EmptyDict => PyDict::new(py).into_any().unbind(),
        Value::Host(index) => host
            .objects
            .get(index)
            .map(|object| object.clone_ref(py))
            .ok_or_else(|| PyValueError::new_err("unknown parsed value handle"))?,
    })
}

/// `build_cli_parser().parse_args(argv)`: help, version and usage errors print and raise
/// `SystemExit`; otherwise the `argparse.Namespace` of the command.
#[pyfunction]
#[pyo3(signature = (argv=None))]
fn parse_cli_arguments<'py>(
    py: Python<'py>,
    argv: Option<Vec<Bound<'py, PyString>>>,
) -> PyResult<Bound<'py, PyAny>> {
    parse(py, argv)
}

/// [`parse_cli_arguments`] for callers in this crate; `None` parses `sys.argv[1:]`.
pub(crate) fn parse<'py>(
    py: Python<'py>,
    argv: Option<Vec<Bound<'py, PyString>>>,
) -> PyResult<Bound<'py, PyAny>> {
    let sys: Bound<'py, PyModule> = py.import("sys")?;
    let argv: Vec<Bound<'py, PyString>> = match argv {
        Some(argv) => argv,
        None => sys
            .getattr("argv")?
            .extract::<Vec<Bound<'py, PyString>>>()?
            .into_iter()
            .skip(1)
            .collect(),
    };
    let mut arguments: Vec<String> = Vec::with_capacity(argv.len());
    let mut plain_placeholders: bool = false;
    let mut surrogates: bool = false;
    for arg in &argv {
        let (text, mapped) = rust_text(arg)?;
        surrogates |= mapped;
        plain_placeholders |= !mapped && uses_placeholders(&text);
        arguments.push(text);
    }
    if surrogates && plain_placeholders {
        return Err(PyValueError::new_err(
            "a command-line argument mixes undecodable bytes with U+10F800..U+10FFFF",
        ));
    }
    let columns: i64 = py
        .import("shutil")?
        .call_method0("get_terminal_size")?
        .getattr("columns")?
        .extract()?;
    let mut host: PythonHost<'py> = PythonHost {
        py,
        objects: Vec::new(),
        surrogates,
    };
    let outcome: ArgvOutcome = parse_arguments(&arguments, ArgvSettings { columns }, &mut host);
    for (stream, text) in outcome.writes {
        let name: &str = match stream {
            Stream::Stdout => "stdout",
            Stream::Stderr => "stderr",
        };
        let file: Bound<'py, PyAny> = sys.getattr(name)?;
        if let Err(error) = file.call_method1("write", (host.text(&text)?,))
            && !(error.is_instance_of::<PyAttributeError>(py)
                || error.is_instance_of::<PyOSError>(py))
        {
            return Err(error);
        }
    }
    let namespace = match outcome.parsed {
        Err(code) => return Err(PySystemExit::new_err(code)),
        Ok(namespace) => namespace,
    };
    let args: Bound<'py, PyAny> = py.import("argparse")?.getattr("Namespace")?.call0()?;
    for (name, value) in namespace.entries {
        args.setattr(name.as_str(), object_of(py, &host, value)?)?;
    }
    Ok(args)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(parse_cli_arguments, module)?)
}
