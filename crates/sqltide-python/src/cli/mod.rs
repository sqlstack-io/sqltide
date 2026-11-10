//! The `sqt` command line: argument parsing and the commands it dispatches.

use pyo3::prelude::{Bound, PyModule, PyResult};

mod argv;
mod surrogates;

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    argv::register(module)
}
