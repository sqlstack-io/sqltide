//! Python bindings for adopted external source validation.

use pyo3::prelude::*;
use sqltide_executor::adopted_sources::{AdoptedSource, CatalogRelation, validate_adopted_sources};

const CLI_EXCEPTIONS: &str = "sqltide.cli.entry.exceptions";

fn adopted_source(config: &Bound<'_, PyAny>) -> PyResult<AdoptedSource> {
    let column = |name: &str| -> PyResult<Option<String>> { config.getattr(name)?.extract() };
    Ok(AdoptedSource {
        table_name: config.getattr("table_name")?.extract()?,
        replay_boundary_mode: config.getattr("replay_boundary_mode")?.str()?.extract()?,
        partition_column: column("partition_column_name")?,
        offset_column: column("offset_column_name")?,
        timestamp_column: column("timestamp_column_name")?,
        landed_at_column: column("landed_at_column_name")?,
        cursor_column: column("cursor_column_name")?,
    })
}

fn catalog_relation(relation: &Bound<'_, PyAny>) -> PyResult<CatalogRelation> {
    let columns = relation
        .getattr("columns")?
        .try_iter()?
        .map(|column| {
            let column = column?;
            Ok((
                column.getattr("name")?.extract()?,
                column.getattr("type")?.extract()?,
            ))
        })
        .collect::<PyResult<_>>()?;
    Ok((relation.getattr("name")?.extract()?, columns))
}

/// Check every adopted source in `external_source_replay_configs` against `catalog`, raising
/// `CliUserError` for the first missing table, missing or mistyped replay column, injected alias
/// collision, or conflicting mapping.
#[pyfunction]
#[pyo3(signature = (*, catalog, external_source_replay_configs, database))]
fn validate_declared_external_sources(
    py: Python<'_>,
    catalog: &Bound<'_, PyAny>,
    external_source_replay_configs: &Bound<'_, PyAny>,
    database: &str,
) -> PyResult<()> {
    let sources = external_source_replay_configs
        .try_iter()?
        .map(|config| adopted_source(&config?))
        .collect::<PyResult<Vec<_>>>()?;
    let relations = catalog
        .getattr("relations")?
        .try_iter()?
        .map(|relation| catalog_relation(&relation?))
        .collect::<PyResult<Vec<_>>>()?;
    validate_adopted_sources(&sources, &relations, database).map_err(|message| {
        match py
            .import(CLI_EXCEPTIONS)
            .and_then(|module| module.getattr("CliUserError"))
            .and_then(|class| class.call1((message,)))
        {
            Ok(exception) => PyErr::from_value(exception),
            Err(error) => error,
        }
    })
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(
        validate_declared_external_sources,
        module
    )?)
}
