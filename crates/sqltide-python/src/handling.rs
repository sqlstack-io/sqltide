//! Exception chaining for failures raised while the native executor handles another failure.

use pyo3::prelude::*;

/// `raised` as Python chains an exception raised inside `except` or `finally` while `handled`
/// is being handled: the oldest exception of its implicit context chain gains `handled` as its
/// context, so the chain ends at `handled` unless it already reaches it. Re-raising `handled`
/// itself leaves it unchanged.
pub(crate) fn while_handling(py: Python<'_>, raised: PyErr, handled: &PyErr) -> PyErr {
    let handled_value: Bound<'_, PyAny> = handled.value(py).clone().into_any();
    let mut current: Bound<'_, PyAny> = raised.value(py).clone().into_any();
    // A chain longer than this is cyclic or pathological; leave it as raised.
    for _ in 0..256 {
        if current.is(&handled_value) {
            return raised;
        }
        let Ok(context) = current.getattr("__context__") else {
            return raised;
        };
        if context.is_none() {
            if !chain_contains(&handled_value, &current) {
                let _ = current.setattr("__context__", &handled_value);
            }
            return raised;
        }
        current = context;
    }
    raised
}

/// Whether `exception` or its implicit context chain holds `wanted`, which would make a cycle.
fn chain_contains(exception: &Bound<'_, PyAny>, wanted: &Bound<'_, PyAny>) -> bool {
    let mut current: Bound<'_, PyAny> = exception.clone();
    for _ in 0..256 {
        if current.is(wanted) {
            return true;
        }
        match current.getattr("__context__") {
            Ok(context) if !context.is_none() => current = context,
            _ => return false,
        }
    }
    true
}

/// `raise value from cause` inside an `except` block handling `handled`, as CPython chains it:
/// `cause` becomes the explicit cause, `handled` the context, and a link back to `value` in the
/// context chain of `handled` is cut so no cycle forms. Re-raising `handled` itself keeps its
/// context.
pub(crate) fn raise_from(
    value: &Bound<'_, PyAny>,
    handled: &Bound<'_, PyAny>,
    cause: Option<&Bound<'_, PyAny>>,
) -> PyResult<PyErr> {
    match cause {
        Some(cause) => value.setattr("__cause__", cause)?,
        None => value.setattr("__cause__", value.py().None())?,
    }
    if !value.is(handled) {
        let mut current: Bound<'_, PyAny> = handled.clone();
        for _ in 0..256 {
            let context: Bound<'_, PyAny> = current.getattr("__context__")?;
            if context.is_none() {
                break;
            }
            if context.is(value) {
                current.setattr("__context__", value.py().None())?;
                break;
            }
            current = context;
        }
        value.setattr("__context__", handled)?;
    }
    Ok(PyErr::from_value(value.clone()))
}

/// `with manager: body()`. `__exit__` receives the body's exception, and an exception raised by
/// `__exit__` itself is chained to it. The managers used here never suppress an exception, so
/// the body's exception always propagates.
pub(crate) fn within<'py, T>(
    manager: &Bound<'py, PyAny>,
    body: impl FnOnce() -> PyResult<T>,
) -> PyResult<T> {
    within_entered(manager, |_| body())
}

/// `with manager as entered: body(entered)`, with the same exit handling as `within`.
pub(crate) fn within_entered<'py, T>(
    manager: &Bound<'py, PyAny>,
    body: impl FnOnce(&Bound<'py, PyAny>) -> PyResult<T>,
) -> PyResult<T> {
    let py: Python<'py> = manager.py();
    let kind: Bound<'py, pyo3::types::PyType> = manager.get_type();
    let enter: Bound<'py, PyAny> = kind.getattr("__enter__")?;
    let exit: Bound<'py, PyAny> = kind.getattr("__exit__")?;
    let entered: Bound<'py, PyAny> = enter.call1((manager,))?;
    match body(&entered) {
        Ok(value) => {
            exit.call1((manager, py.None(), py.None(), py.None()))?;
            Ok(value)
        }
        Err(error) => {
            let raised: Bound<'py, PyAny> = error.value(py).clone().into_any();
            let traceback: Bound<'py, PyAny> = raised.getattr("__traceback__")?;
            if let Err(exit_error) = exit.call1((manager, raised.get_type(), &raised, traceback)) {
                return Err(while_handling(py, exit_error, &error));
            }
            Err(error)
        }
    }
}
