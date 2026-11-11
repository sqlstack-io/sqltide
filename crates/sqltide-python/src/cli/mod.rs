//! The `sqt` command line: argument parsing, invocation resolution and command dispatch.

use pyo3::prelude::{Bound, PyModule, PyResult};

mod admin;
mod argv;
pub(crate) mod commands;
mod dispatch;
mod entry;
pub(crate) mod invocation;
pub(crate) mod output;
mod playground;
pub(crate) mod python;
mod readiness;
pub(crate) mod render;
mod sql;
pub(crate) mod style;
mod surrogates;
mod workflow_artifacts;

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    argv::register(module)?;
    commands::register(module)?;
    dispatch::register(module)?;
    entry::register(module)?;
    invocation::register(module)?;
    readiness::register(module)?;
    style::register(module)?;
    workflow_artifacts::register(module)?;
    render::register(module)
}
