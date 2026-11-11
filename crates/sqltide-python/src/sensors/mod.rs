//! The native sensor runtime: registry loading, dispatch, durable steps and state persistence.
//! User handlers still run through the Python evaluation shim and receive the Python contexts.

mod dispatch;
mod providers;
mod queries;
mod registry;
mod repository;
mod steps;
mod support;

use pyo3::prelude::*;

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<dispatch::SensorDispatcher>()?;
    module.add_class::<repository::SensorStateRepository>()?;
    module.add_class::<steps::DurableStepRunner>()?;
    module.add_class::<steps::InMemoryStepStore>()?;
    module.add_class::<steps::RepositoryStepStore>()?;
    module.add_function(wrap_pyfunction!(dispatch::effective_sensor_status, module)?)?;
    module.add_function(wrap_pyfunction!(providers::provider_name, module)?)?;
    registry::register(module)
}
