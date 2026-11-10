//! The `sqt` command line: argument parsing, invocation resolution and command dispatch.

use pyo3::prelude::{Bound, PyModule, PyResult};

mod admin;
mod argv;
mod dispatch;
mod entry;
pub(crate) mod invocation;
mod playground;
mod python;
mod render;
mod sql;
mod surrogates;

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    argv::register(module)?;
    dispatch::register(module)?;
    entry::register(module)?;
    invocation::register(module)?;
    render::register(module)
}
