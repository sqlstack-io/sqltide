//! The Python built-ins the command line uses: `getattr` defaults, `print`, raising and chaining.

use pyo3::exceptions::PyAttributeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::dev_server::support::attr;

const CLI_EXCEPTIONS: &str = "sqltide.cli.entry.exceptions";

/// `getattr(object, name, default)`.
pub(crate) fn getattr_or<'py>(
    object: &Bound<'py, PyAny>,
    name: &str,
    default: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    match object.getattr(name) {
        Ok(value) => Ok(value),
        Err(error) if error.is_instance_of::<PyAttributeError>(object.py()) => Ok(default),
        Err(error) => Err(error),
    }
}

/// `getattr(object, name, None)`.
pub(crate) fn getattr_none<'py>(
    object: &Bound<'py, PyAny>,
    name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    getattr_or(object, name, object.py().None().into_bound(object.py()))
}

/// `bool(getattr(object, name, False))`.
pub(crate) fn flag(object: &Bound<'_, PyAny>, name: &str) -> PyResult<bool> {
    getattr_none(object, name)?.is_truthy()
}

/// `print(text, file=sys.<stream>)`: nothing when the stream is `None`.
pub(crate) fn print_to(py: Python<'_>, stream: &str, text: &str) -> PyResult<()> {
    let file: Bound<'_, PyAny> = py.import("sys")?.getattr(stream)?;
    if file.is_none() {
        return Ok(());
    }
    file.call_method1("write", (text,))?;
    file.call_method1("write", ("\n",))?;
    Ok(())
}

/// `print(text)`.
pub(crate) fn print(py: Python<'_>, text: &str) -> PyResult<()> {
    print_to(py, "stdout", text)
}

/// `error` raised while `context` was being handled: Python sets `__context__` implicitly.
pub(crate) fn raised_during(py: Python<'_>, error: PyErr, context: PyErr) -> PyErr {
    let value: Bound<'_, PyAny> = error.value(py).clone().into_any();
    if value
        .getattr("__context__")
        .is_ok_and(|existing| existing.is_none())
        && !value.is(context.value(py))
    {
        let _ = value.setattr("__context__", context.value(py));
    }
    error
}

/// `raise error from cause`.
pub(crate) fn raised_from(py: Python<'_>, error: PyErr, cause: PyErr) -> PyErr {
    let context: PyErr = cause.clone_ref(py);
    error.set_cause(py, Some(cause));
    raised_during(py, error, context)
}

/// `CliUserError(message, hint=hint)`.
pub(crate) fn user_error(py: Python<'_>, message: &str, hint: Option<&str>) -> PyErr {
    let built: PyResult<Bound<'_, PyAny>> = (|| {
        let kwargs: Bound<'_, PyDict> = PyDict::new(py);
        kwargs.set_item("hint", hint)?;
        attr(py, CLI_EXCEPTIONS, "CliUserError")?.call((message,), Some(&kwargs))
    })();
    match built {
        Ok(instance) => PyErr::from_value(instance),
        Err(error) => error,
    }
}

/// Whether `error` is an instance of `module.name`.
pub(crate) fn raised(py: Python<'_>, error: &PyErr, module: &str, name: &str) -> PyResult<bool> {
    error.value(py).is_instance(&attr(py, module, name)?)
}
