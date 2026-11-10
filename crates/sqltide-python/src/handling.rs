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
