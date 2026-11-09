//! Python bindings that realize one compiled project through the adapter profile's callbacks
//! and assemble the planner's desired state from the realized resources.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PySet, PyTuple};

const NATIVE: &str = "sqltide._native";
const ADAPTER_CONSTANTS: &str = "sqltide.adapter.constants";
const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const COMPILE_CONSTANTS: &str = "sqltide.compiler.compile.constants";
const COMPILE_EXCEPTIONS: &str = "sqltide.compiler.compile.exceptions";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const DISCOVERY_MODELS: &str = "sqltide.compiler.discovery.models";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const PIPELINE_MODELS: &str = "sqltide.compiler.pipeline.models";
const KAFKA_RETENTION_REFERENCE: &str = "KafkaRetentionReference";

/// The Python classes, constants and callables realization reads, imported once per call.
struct Realizer<'py> {
    py: Python<'py>,
    replace: Bound<'py, PyAny>,
    sorted: Bound<'py, PyAny>,
    dict: Bound<'py, PyAny>,
    frozenset: Bound<'py, PyAny>,
    compile: Bound<'py, PyModule>,
    adapter: Bound<'py, PyModule>,
    native: Bound<'py, PyModule>,
    compile_error: Bound<'py, PyAny>,
    external_source: Bound<'py, PyAny>,
    postgres_source: Bound<'py, PyAny>,
    table_model: Bound<'py, PyAny>,
    view_model: Bound<'py, PyAny>,
    managed_source: Bound<'py, PyAny>,
    adapter_table: Bound<'py, PyAny>,
    adapter_materialized_view: Bound<'py, PyAny>,
    adapter_view: Bound<'py, PyAny>,
    quarantine: Bound<'py, PyAny>,
    kafka_table_type: Bound<'py, PyAny>,
    table_type: Bound<'py, PyAny>,
    materialized_view_type: Bound<'py, PyAny>,
    view_type: Bound<'py, PyAny>,
}

impl<'py> Realizer<'py> {
    fn import(py: Python<'py>) -> PyResult<Self> {
        let builtins: Bound<'py, PyModule> = py.import("builtins")?;
        let compile: Bound<'py, PyModule> = py.import(COMPILE_MODELS)?;
        let adapter: Bound<'py, PyModule> = py.import(ADAPTER_MODELS)?;
        let discovery: Bound<'py, PyModule> = py.import(DISCOVERY_MODELS)?;
        let constants: Bound<'py, PyModule> = py.import(COMPILE_CONSTANTS)?;
        Ok(Self {
            py,
            replace: py.import("dataclasses")?.getattr("replace")?,
            sorted: builtins.getattr("sorted")?,
            dict: builtins.getattr("dict")?,
            frozenset: builtins.getattr("frozenset")?,
            native: py.import(NATIVE)?,
            compile_error: py
                .import(COMPILE_EXCEPTIONS)?
                .getattr("PipelineCompileError")?,
            external_source: discovery.getattr("ExternalTableSourceStep")?,
            postgres_source: discovery.getattr("PostgresRefreshSourceStep")?,
            table_model: compile.getattr("CompiledTableModel")?,
            view_model: compile.getattr("CompiledViewModel")?,
            managed_source: adapter.getattr("AdapterManagedSource")?,
            adapter_table: adapter.getattr("AdapterTable")?,
            adapter_materialized_view: adapter.getattr("AdapterMaterializedView")?,
            adapter_view: adapter.getattr("AdapterView")?,
            quarantine: py
                .import(DISCOVERY_TYPES)?
                .getattr("RowErrorPolicy")?
                .getattr("QUARANTINE")?,
            kafka_table_type: constants.getattr("DESIRED_OBJECT_TYPE_KAFKA_TABLE")?,
            table_type: constants.getattr("DESIRED_OBJECT_TYPE_TABLE")?,
            materialized_view_type: constants.getattr("DESIRED_OBJECT_TYPE_MATERIALIZED_VIEW")?,
            view_type: constants.getattr("DESIRED_OBJECT_TYPE_VIEW")?,
            compile,
            adapter,
        })
    }

    fn none(&self) -> Bound<'py, PyAny> {
        self.py.None().into_bound(self.py)
    }

    fn boolean(&self, value: bool) -> Bound<'py, PyAny> {
        pyo3::types::PyBool::new(self.py, value)
            .to_owned()
            .into_any()
    }

    fn tuple(&self, items: Vec<Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
        Ok(PyTuple::new(self.py, items)?.into_any())
    }

    fn call(
        &self,
        callable: &Bound<'py, PyAny>,
        arguments: &[(&str, Bound<'py, PyAny>)],
    ) -> PyResult<Bound<'py, PyAny>> {
        let keywords: Bound<'py, PyDict> = PyDict::new(self.py);
        for (name, value) in arguments {
            keywords.set_item(name, value)?;
        }
        callable.call((), Some(&keywords))
    }

    fn build(
        &self,
        module: &Bound<'py, PyModule>,
        class: &str,
        arguments: &[(&str, Bound<'py, PyAny>)],
    ) -> PyResult<Bound<'py, PyAny>> {
        self.call(&module.getattr(class)?, arguments)
    }

    /// `dataclasses.replace(value, **changes)`.
    fn replace(
        &self,
        value: Bound<'py, PyAny>,
        changes: &[(&str, Bound<'py, PyAny>)],
    ) -> PyResult<Bound<'py, PyAny>> {
        let keywords: Bound<'py, PyDict> = PyDict::new(self.py);
        for (name, change) in changes {
            keywords.set_item(name, change)?;
        }
        self.replace.call((value,), Some(&keywords))
    }

    /// `dict(value)`.
    fn dict(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.dict.call1((value,))
    }

    fn fail(&self, message: String) -> PyErr {
        match self.compile_error.call1((message,)) {
            Ok(error) => PyErr::from_value(error),
            Err(error) => error,
        }
    }

    fn items(&self, value: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
        value.try_iter()?.collect()
    }

    /// `() if settings is None else tuple(sorted(settings.items()))`.
    fn sorted_settings(&self, settings: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        if settings.is_none() {
            return self.tuple(Vec::new());
        }
        let sorted: Bound<'py, PyAny> = self.sorted.call1((settings.call_method0("items")?,))?;
        self.tuple(self.items(&sorted)?)
    }

    /// `None if not settings else dict(settings)`.
    fn optional_dict(&self, settings: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        if settings.is_truthy()? {
            self.dict(settings)
        } else {
            Ok(self.none())
        }
    }

    fn object_key(
        &self,
        name: Bound<'py, PyAny>,
        object_type: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.build(
            &self.compile,
            "ObjectKey",
            &[
                ("database", self.none()),
                ("object_type", object_type.clone()),
                ("name", name),
            ],
        )
    }

    fn column(&self, column: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.build(
            &self.compile,
            "Column",
            &[
                ("name", column.getattr("name")?),
                ("type", column.getattr("type")?),
                ("default", column.getattr("default_expression")?),
            ],
        )
    }

    fn columns(&self, resource: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let columns: Vec<Bound<'py, PyAny>> = self
            .items(&resource.getattr("columns")?)?
            .iter()
            .map(|column| self.column(column))
            .collect::<PyResult<_>>()?;
        self.tuple(columns)
    }

    fn postgres_relation_sql(&self, source: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.native
            .getattr("clickhouse_postgres_source_relation_sql")?
            .call1((
                source.getattr("host")?,
                source.getattr("port")?.str()?,
                source.getattr("database")?,
                source.getattr("table")?,
                source.getattr("user")?,
                source.getattr("password_env")?,
            ))
    }

    fn external_relation_sql(&self, source: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let columns: Bound<'py, PyAny> = source.getattr("replay_boundary")?.getattr("columns")?;
        let names: Vec<Bound<'py, PyAny>> =
            ["partition", "offset", "timestamp", "landed_at", "cursor"]
                .iter()
                .map(|name| columns.getattr(*name))
                .collect::<PyResult<_>>()?;
        self.native
            .getattr("clickhouse_external_source_relation_sql")?
            .call1((
                source.getattr("table_name")?,
                pyo3::types::PyList::new(self.py, names)?,
            ))
    }

    fn source_request(
        &self,
        source: &Bound<'py, PyAny>,
        project: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let authored: Bound<'py, PyAny> = source.getattr("source")?;
        let logical_name: Bound<'py, PyAny> = source.getattr("key")?.getattr("name")?;
        if authored.is_instance(&self.external_source)? {
            return self.build(
                &self.adapter,
                "AdapterAdoptedSourceRealizationRequest",
                &[
                    ("logical_name", logical_name),
                    ("relation_name", authored.getattr("table_name")?),
                ],
            );
        }
        if authored.is_instance(&self.postgres_source)? {
            return self.build(
                &self.adapter,
                "AdapterAdoptedSourceRealizationRequest",
                &[
                    ("logical_name", logical_name),
                    ("relation_name", self.postgres_relation_sql(&authored)?),
                ],
            );
        }
        let kafka: Bound<'py, PyAny> = authored.getattr("kafka")?;
        let settings: Bound<'py, PyAny> = self.sorted_settings(&kafka.getattr("settings")?)?;
        let retention: Bound<'py, PyAny> = kafka.getattr("retention")?;
        let unset: Bound<'py, PyTuple> = PyTuple::new(self.py, [self.none(), self.boolean(false)])?;
        let ttl: Bound<'py, PyAny> = if unset.contains(&retention)? {
            kafka.getattr("ttl")?
        } else {
            let landed: Bound<'py, PyAny> = self
                .py
                .import(DISCOVERY_TYPES)?
                .getattr(KAFKA_RETENTION_REFERENCE)?
                .getattr("LANDED")?;
            sqltide_compile::retention::render_kafka_retention(
                &sqltide_compile::retention::KafkaRetention {
                    duration_seconds: retention.getattr("duration_seconds")?.extract()?,
                    fallback_landed: retention.getattr("fallback")?.eq(&landed)?,
                    cap_at_landed: retention.getattr("cap_at")?.eq(&landed)?,
                },
            )
            .into_pyobject(self.py)?
            .into_any()
        };
        self.build(
            &self.adapter,
            "AdapterManagedSourceRealizationRequest",
            &[
                ("logical_name", logical_name),
                (
                    "source_kind",
                    self.py
                        .import(ADAPTER_CONSTANTS)?
                        .getattr("MANAGED_SOURCE_KIND_KAFKA")?,
                ),
                ("broker_list", kafka.getattr("broker_list")?),
                ("topic", kafka.getattr("topic")?),
                ("consumer_group", kafka.getattr("consumer_group")?),
                ("format", kafka.getattr("format")?),
                ("project_name", project.getattr("project_name")?),
                ("target_name", project.getattr("target_name")?),
                ("ttl", ttl),
                ("settings", settings),
                (
                    "naming_macro_fingerprint",
                    authored.getattr("naming_macro_fingerprint")?,
                ),
            ],
        )
    }

    /// The source realization with each materialized view query in canonical form.
    fn canonical_source_realization(
        &self,
        realization: &Bound<'py, PyAny>,
        sql_analyzer: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let mut resources: Vec<Bound<'py, PyAny>> = Vec::new();
        for resource in self.items(&realization.getattr("resources")?)? {
            if resource.is_instance(&self.adapter_materialized_view)? {
                let query: Bound<'py, PyAny> = self.call(
                    &sql_analyzer.getattr("canonicalize_query")?,
                    &[("sql", resource.getattr("query")?)],
                )?;
                resources.push(self.replace(
                    resource,
                    &[
                        ("query", query.getattr("canonical_sql")?),
                        ("database_template", query.getattr("database_template")?),
                    ],
                )?);
            } else {
                resources.push(resource);
            }
        }
        self.replace(
            realization.clone(),
            &[("resources", self.tuple(resources)?)],
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn model_request(
        &self,
        model: &Bound<'py, PyAny>,
        resolved_query: &Bound<'py, PyAny>,
        relation_names: &Bound<'py, PyDict>,
        relation_name_by_logical_name: &Bound<'py, PyDict>,
        postgres_by_source_name: &Bound<'py, PyDict>,
        sql_analyzer: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let key: Bound<'py, PyAny> = model.getattr("key")?;
        let logical_name: Bound<'py, PyAny> = key.getattr("name")?;
        if model.is_instance(&self.view_model)? {
            return self.build(
                &self.adapter,
                "AdapterViewRealizationRequest",
                &[
                    ("logical_name", logical_name),
                    (
                        "target_relation_name",
                        relation_names.as_any().get_item(&key)?,
                    ),
                    ("resolved_query", resolved_query.getattr("canonical_sql")?),
                    (
                        "resolved_database_template",
                        resolved_query.getattr("database_template")?,
                    ),
                ],
            );
        }
        if !model.is_instance(&self.table_model)? {
            return Err(self.fail(format!(
                "Unsupported compiled model type for '{}'",
                logical_name.str()?
            )));
        }
        let transform: Bound<'py, PyAny> = model.getattr("transform")?;
        let settings: Bound<'py, PyAny> = self.sorted_settings(&transform.getattr("settings")?)?;
        let source_name: Bound<'py, PyAny> = transform.getattr("source")?;
        let source_relation_name: Bound<'py, PyAny> = relation_name_by_logical_name
            .as_any()
            .get_item(&source_name)?;
        let postgres: Option<Bound<'py, PyAny>> = postgres_by_source_name.get_item(&source_name)?;
        let canonical: Bound<'py, PyAny> = resolved_query.getattr("canonical_sql")?;
        let template: Bound<'py, PyAny> = resolved_query.getattr("database_template")?;
        let (query, database_template, dead_letter_query, dead_letter_template) =
            if model.getattr("on_row_error")?.is(&self.quarantine) {
                let split: Bound<'py, PyAny> = sql_analyzer.getattr("quarantine_split")?;
                let (query, dead_letter): (Bound<'py, PyAny>, Bound<'py, PyAny>) = self
                    .call(
                        &split,
                        &[("sql", canonical), ("model_name", logical_name.clone())],
                    )?
                    .extract()?;
                let (template, dead_letter_template): (Bound<'py, PyAny>, Bound<'py, PyAny>) = self
                    .call(
                        &split,
                        &[("sql", template), ("model_name", logical_name.clone())],
                    )?
                    .extract()?;
                (query, template, dead_letter, dead_letter_template)
            } else {
                (canonical, template, self.none(), self.none())
            };
        let columns: Vec<Bound<'py, PyAny>> = self
            .items(&model.getattr("output_columns")?)?
            .iter()
            .map(|column| {
                self.build(
                    &self.adapter,
                    "AdapterColumn",
                    &[
                        ("name", column.getattr("name")?),
                        ("type", column.getattr("type")?),
                        ("default_expression", column.getattr("default")?),
                    ],
                )
            })
            .collect::<PyResult<_>>()?;
        let order_by: Vec<Bound<'py, PyAny>> = self.items(&transform.getattr("order_by")?)?;
        let (refresh, append) = match &postgres {
            None => (self.none(), self.boolean(true)),
            Some(source) => (source.getattr("refresh")?, source.getattr("append")?),
        };
        self.build(
            &self.adapter,
            "AdapterModelRealizationRequest",
            &[
                ("logical_name", logical_name),
                (
                    "target_relation_name",
                    relation_names.as_any().get_item(&key)?,
                ),
                ("source_relation_name", source_relation_name),
                ("resolved_query", query),
                ("resolved_database_template", database_template),
                ("dead_letter_query", dead_letter_query),
                ("dead_letter_database_template", dead_letter_template),
                ("columns", self.tuple(columns)?),
                ("engine", transform.getattr("engine")?),
                ("order_by", self.tuple(order_by)?),
                ("partition_by", transform.getattr("partition_by")?),
                ("ttl", transform.getattr("ttl")?),
                ("settings", settings),
                ("refresh", refresh),
                ("append", append),
            ],
        )
    }

    /// The single resource of `class` (materialized views that hold dead letters excluded when
    /// `skip_dead_letters`), or the adapter contract error naming `what`.
    fn single_resource(
        &self,
        resources: &[Bound<'py, PyAny>],
        class: &Bound<'py, PyAny>,
        skip_dead_letters: bool,
        logical_name: &Bound<'py, PyAny>,
        what: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let mut matches: Vec<&Bound<'py, PyAny>> = Vec::new();
        for resource in resources {
            if resource.is_instance(class)?
                && !(skip_dead_letters && resource.getattr("dead_letters")?.is_truthy()?)
            {
                matches.push(resource);
            }
        }
        match matches.as_slice() {
            [single] => Ok((*single).clone()),
            _ => Err(self.fail(format!(
                "Adapter realization for '{}' requires exactly one {what}; got {}",
                logical_name.str()?,
                matches.len()
            ))),
        }
    }

    fn desired_table(
        &self,
        resource: &Bound<'py, PyAny>,
        key: Bound<'py, PyAny>,
        deps: Bound<'py, PyAny>,
        logical_model_name: Bound<'py, PyAny>,
        model: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let replay_fallback: Bound<'py, PyAny> = match model {
            None => self
                .py
                .import(DISCOVERY_TYPES)?
                .getattr("BoundedReplayFallback")?
                .getattr("FULL")?,
            Some(model) => model.getattr("effective_bounded_replay_fallback")?,
        };
        let storage: Bound<'py, PyAny> = self.build(
            &self.compile,
            "TableStorage",
            &[
                ("engine", resource.getattr("engine")?),
                ("order_by", resource.getattr("order_by")?),
                ("partition_by", resource.getattr("partition_by")?),
                ("ttl", resource.getattr("ttl")?),
                (
                    "settings",
                    self.optional_dict(&resource.getattr("settings")?)?,
                ),
            ],
        )?;
        let spec: Bound<'py, PyAny> = self.build(
            &self.compile,
            "TableSpec",
            &[("columns", self.columns(resource)?), ("storage", storage)],
        )?;
        let (replay_on_change, replay_settings) = match model {
            None => (self.none(), self.none()),
            Some(model) => {
                let replay: Bound<'py, PyAny> = model
                    .getattr("transform")?
                    .getattr("execution_settings")?
                    .getattr("replay")?;
                (
                    model.getattr("replay_on_change")?,
                    if replay.is_none() {
                        self.none()
                    } else {
                        self.dict(&replay)?
                    },
                )
            }
        };
        self.build(
            &self.compile,
            "DesiredTable",
            &[
                ("key", key),
                ("deps", deps),
                ("spec", spec),
                ("logical_model_name", logical_model_name),
                ("replay_on_change", replay_on_change),
                ("bounded_replay_fallback", replay_fallback),
                ("replay_settings", replay_settings),
            ],
        )
    }

    fn desired_view(
        &self,
        resource: &Bound<'py, PyAny>,
        deps: Bound<'py, PyAny>,
        logical_model_name: Bound<'py, PyAny>,
        filters_row_errors: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let key: Bound<'py, PyAny> =
            self.object_key(resource.getattr("name")?, &self.materialized_view_type)?;
        let spec: Bound<'py, PyAny> = self.build(
            &self.compile,
            "MaterializedViewSpec",
            &[
                (
                    "source_table_name",
                    resource.getattr("source_relation_name")?,
                ),
                (
                    "target_table_name",
                    resource.getattr("target_relation_name")?,
                ),
                ("query", resource.getattr("query")?),
                ("database_template", resource.getattr("database_template")?),
            ],
        )?;
        self.build(
            &self.compile,
            "DesiredMaterializedView",
            &[
                ("key", key),
                ("deps", deps),
                ("spec", spec),
                ("logical_model_name", logical_model_name),
                ("refresh", resource.getattr("refresh")?),
                ("append", resource.getattr("append")?),
                ("filters_row_errors", self.boolean(filters_row_errors)),
                ("dead_letters", resource.getattr("dead_letters")?),
            ],
        )
    }

    fn source_desired_objects(
        &self,
        source: &Bound<'py, PyAny>,
        realization: &Bound<'py, PyAny>,
    ) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let authored: Bound<'py, PyAny> = source.getattr("source")?;
        let logical_name: Bound<'py, PyAny> = source.getattr("key")?.getattr("name")?;
        let resources_value: Bound<'py, PyAny> = realization.getattr("resources")?;
        if authored.is_instance(&self.external_source)?
            || authored.is_instance(&self.postgres_source)?
        {
            if resources_value.is_truthy()? {
                return Err(self.fail(format!(
                    "Adapter claimed resources for adopted source '{}'",
                    logical_name.str()?
                )));
            }
            return Ok(Vec::new());
        }
        let resources: Vec<Bound<'py, PyAny>> = self.items(&resources_value)?;
        let managed: Bound<'py, PyAny> = self.single_resource(
            &resources,
            &self.managed_source,
            false,
            &logical_name,
            "managed source",
        )?;
        let table: Bound<'py, PyAny> = self.single_resource(
            &resources,
            &self.adapter_table,
            false,
            &logical_name,
            "table",
        )?;
        let view: Bound<'py, PyAny> = self.single_resource(
            &resources,
            &self.adapter_materialized_view,
            true,
            &logical_name,
            "materialized view",
        )?;
        let source_key: Bound<'py, PyAny> =
            self.object_key(managed.getattr("name")?, &self.kafka_table_type)?;
        let table_key: Bound<'py, PyAny> =
            self.object_key(table.getattr("name")?, &self.table_type)?;
        let kafka: Bound<'py, PyAny> = self.build(
            &self.compile,
            "KafkaSettings",
            &[
                ("broker_list", managed.getattr("broker_list")?),
                ("topic", managed.getattr("topic")?),
                ("consumer_group", managed.getattr("consumer_group")?),
                ("format", managed.getattr("format")?),
                (
                    "settings",
                    self.optional_dict(&managed.getattr("settings")?)?,
                ),
            ],
        )?;
        let kafka_spec: Bound<'py, PyAny> = self.build(
            &self.compile,
            "KafkaTableSpec",
            &[
                ("columns", self.columns(&managed)?),
                ("kafka", kafka),
                (
                    "naming_macro_fingerprint",
                    managed.getattr("naming_macro_fingerprint")?,
                ),
            ],
        )?;
        let desired_source: Bound<'py, PyAny> = self.build(
            &self.compile,
            "DesiredKafkaTable",
            &[
                ("key", source_key.clone()),
                ("deps", self.tuple(Vec::new())?),
                ("spec", kafka_spec),
            ],
        )?;
        let desired_table: Bound<'py, PyAny> = self.desired_table(
            &table,
            table_key.clone(),
            self.tuple(Vec::new())?,
            logical_name.clone(),
            None,
        )?;
        let desired_view: Bound<'py, PyAny> = self.desired_view(
            &view,
            self.tuple(vec![source_key, table_key])?,
            logical_name,
            false,
        )?;
        Ok(vec![desired_source, desired_table, desired_view])
    }

    /// `tuple(dict.fromkeys(relation_key_by_name[ref.name] for ref in model.parsed_refs))`.
    fn ref_keys(
        &self,
        model: &Bound<'py, PyAny>,
        relation_key_by_name: &Bound<'py, PyDict>,
    ) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let unique: Bound<'py, PyDict> = PyDict::new(self.py);
        for parsed_ref in self.items(&model.getattr("parsed_refs")?)? {
            unique.set_item(
                relation_key_by_name
                    .as_any()
                    .get_item(parsed_ref.getattr("name")?)?,
                self.none(),
            )?;
        }
        Ok(unique.keys().iter().collect())
    }

    fn model_desired_objects(
        &self,
        model: &Bound<'py, PyAny>,
        realization: &Bound<'py, PyAny>,
        relation_key_by_name: &Bound<'py, PyDict>,
        resolved_query: &Bound<'py, PyAny>,
    ) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let logical_name: Bound<'py, PyAny> = model.getattr("key")?.getattr("name")?;
        let resources: Vec<Bound<'py, PyAny>> = self.items(&realization.getattr("resources")?)?;
        let table: Bound<'py, PyAny> = self.single_resource(
            &resources,
            &self.adapter_table,
            false,
            &logical_name,
            "table",
        )?;
        let view: Bound<'py, PyAny> = self.single_resource(
            &resources,
            &self.adapter_materialized_view,
            true,
            &logical_name,
            "materialized view",
        )?;
        let table_key: Bound<'py, PyAny> =
            self.object_key(table.getattr("name")?, &self.table_type)?;
        let mut deps: Vec<Bound<'py, PyAny>> = self.ref_keys(model, relation_key_by_name)?;
        deps.push(table_key.clone());
        let quarantines: bool = model.getattr("on_row_error")?.is(&self.quarantine);
        let mut desired_view: Bound<'py, PyAny> = self.desired_view(
            &view,
            self.tuple(deps.clone())?,
            logical_name.clone(),
            quarantines,
        )?;
        if quarantines {
            desired_view = self.replace(
                desired_view,
                &[
                    ("authored_query", resolved_query.getattr("canonical_sql")?),
                    (
                        "authored_database_template",
                        resolved_query.getattr("database_template")?,
                    ),
                ],
            )?;
        }
        let mut dead_letter_views: Vec<Bound<'py, PyAny>> = Vec::new();
        for resource in &resources {
            if resource.is_instance(&self.adapter_materialized_view)?
                && resource.getattr("dead_letters")?.is_truthy()?
            {
                dead_letter_views.push(self.desired_view(
                    resource,
                    self.tuple(deps.clone())?,
                    logical_name.clone(),
                    false,
                )?);
            }
        }
        let source_key: Bound<'py, PyAny> = relation_key_by_name
            .as_any()
            .get_item(model.getattr("transform")?.getattr("source")?)?;
        let desired_table: Bound<'py, PyAny> = self.desired_table(
            &table,
            table_key,
            self.tuple(vec![source_key])?,
            logical_name,
            Some(model),
        )?;
        let mut objects: Vec<Bound<'py, PyAny>> = vec![desired_table, desired_view];
        objects.extend(dead_letter_views);
        Ok(objects)
    }

    fn view_model_desired_object(
        &self,
        model: &Bound<'py, PyAny>,
        realization: &Bound<'py, PyAny>,
        relation_key_by_name: &Bound<'py, PyDict>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let logical_name: Bound<'py, PyAny> = model.getattr("key")?.getattr("name")?;
        let resources: Vec<Bound<'py, PyAny>> = self.items(&realization.getattr("resources")?)?;
        let view: Bound<'py, PyAny> = self.single_resource(
            &resources,
            &self.adapter_view,
            false,
            &logical_name,
            "ordinary view",
        )?;
        let key: Bound<'py, PyAny> = self.object_key(view.getattr("name")?, &self.view_type)?;
        let deps: Bound<'py, PyAny> = self.tuple(self.ref_keys(model, relation_key_by_name)?)?;
        let spec: Bound<'py, PyAny> = self.build(
            &self.compile,
            "ViewSpec",
            &[
                ("query", view.getattr("query")?),
                ("database_template", view.getattr("database_template")?),
            ],
        )?;
        self.build(
            &self.compile,
            "DesiredView",
            &[
                ("key", key),
                ("deps", deps),
                ("spec", spec),
                ("logical_model_name", logical_name),
            ],
        )
    }

    fn external_replay_config(
        &self,
        source: &Bound<'py, PyAny>,
        key: &Bound<'py, PyAny>,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        let authored: Bound<'py, PyAny> = source.getattr("source")?;
        if !authored.is_instance(&self.external_source)? {
            return Ok(None);
        }
        let boundary: Bound<'py, PyAny> = authored.getattr("replay_boundary")?;
        let columns: Bound<'py, PyAny> = boundary.getattr("columns")?;
        self.build(
            &self.compile,
            "ExternalSourceReplayConfig",
            &[
                ("key", key.clone()),
                ("table_name", authored.getattr("table_name")?),
                ("source_kind", authored.getattr("kind")?),
                ("replay_boundary_mode", boundary.getattr("mode")?),
                ("partition_column_name", columns.getattr("partition")?),
                ("offset_column_name", columns.getattr("offset")?),
                ("timestamp_column_name", columns.getattr("timestamp")?),
                ("landed_at_column_name", columns.getattr("landed_at")?),
                ("cursor_column_name", columns.getattr("cursor")?),
            ],
        )
        .map(Some)
    }

    /// `items` stably sorted by the text of each one's `key(item)` parts.
    fn sorted_by(
        &self,
        items: Vec<Bound<'py, PyAny>>,
        key: impl Fn(&Bound<'py, PyAny>) -> PyResult<Vec<String>>,
    ) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let mut keyed: Vec<(Vec<String>, Bound<'py, PyAny>)> = items
            .into_iter()
            .map(|item| Ok((key(&item)?, item)))
            .collect::<PyResult<_>>()?;
        keyed.sort_by(|left, right| left.0.cmp(&right.0));
        Ok(keyed.into_iter().map(|(_, item)| item).collect())
    }

    fn build_desired_state(
        &self,
        project: &Bound<'py, PyAny>,
        resolved_sql_by_key: &Bound<'py, PyDict>,
        source_realizations: &Bound<'py, PyDict>,
        model_realizations: &Bound<'py, PyDict>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let mut objects: Vec<Bound<'py, PyAny>> = Vec::new();
        let replay_anchor_keys: Bound<'py, PySet> = PySet::empty(self.py)?;
        let mutable_ref_warning_keys: Bound<'py, PySet> = PySet::empty(self.py)?;
        let mut external_configs: Vec<Bound<'py, PyAny>> = Vec::new();
        let relation_key_by_name: Bound<'py, PyDict> = PyDict::new(self.py);
        for source in self.items(&project.getattr("sources")?)? {
            let key: Bound<'py, PyAny> = source.getattr("key")?;
            let realization: Bound<'py, PyAny> = source_realizations.as_any().get_item(&key)?;
            objects.extend(self.source_desired_objects(&source, &realization)?);
            let source_key: Bound<'py, PyAny> =
                self.object_key(realization.getattr("relation_name")?, &self.table_type)?;
            relation_key_by_name.set_item(key.getattr("name")?, &source_key)?;
            replay_anchor_keys.add(&source_key)?;
            if let Some(config) = self.external_replay_config(&source, &source_key)? {
                external_configs.push(config);
            }
        }
        let models: Vec<Bound<'py, PyAny>> = self.items(&project.getattr("models")?)?;
        for model in &models {
            let key: Bound<'py, PyAny> = model.getattr("key")?;
            let object_type: &Bound<'py, PyAny> = if model.is_instance(&self.table_model)? {
                &self.table_type
            } else {
                &self.view_type
            };
            let relation_name: Bound<'py, PyAny> = model_realizations
                .as_any()
                .get_item(&key)?
                .getattr("relation_name")?;
            relation_key_by_name.set_item(
                key.getattr("name")?,
                self.object_key(relation_name, object_type)?,
            )?;
        }
        for model in &models {
            let key: Bound<'py, PyAny> = model.getattr("key")?;
            if model.is_instance(&self.view_model)? {
                let realization: Bound<'py, PyAny> = model_realizations.as_any().get_item(&key)?;
                objects.push(self.view_model_desired_object(
                    model,
                    &realization,
                    &relation_key_by_name,
                )?);
            } else if model.is_instance(&self.table_model)? {
                let realization: Bound<'py, PyAny> = model_realizations.as_any().get_item(&key)?;
                let resolved_query: Bound<'py, PyAny> =
                    resolved_sql_by_key.as_any().get_item(&key)?;
                let model_objects: Vec<Bound<'py, PyAny>> = self.model_desired_objects(
                    model,
                    &realization,
                    &relation_key_by_name,
                    &resolved_query,
                )?;
                let first_key: Bound<'py, PyAny> = model_objects[0].getattr("key")?;
                objects.extend(model_objects);
                if model.getattr("replay_anchor_eligible")?.is_truthy()? {
                    replay_anchor_keys.add(&first_key)?;
                }
                if model.getattr("has_mutable_refs")?.is_truthy()? {
                    mutable_ref_warning_keys.add(&first_key)?;
                }
            }
        }
        let objects: Vec<Bound<'py, PyAny>> = self.sorted_by(objects, |object| {
            let key: Bound<'py, PyAny> = object.getattr("key")?;
            Ok(vec![
                key.getattr("object_type")?.extract()?,
                key.getattr("name")?.extract()?,
            ])
        })?;
        let external_configs: Vec<Bound<'py, PyAny>> = self
            .sorted_by(external_configs, |config| {
                Ok(vec![config.getattr("table_name")?.extract()?])
            })?;
        self.build(
            &self.compile,
            "DesiredState",
            &[
                ("objects", self.tuple(objects)?),
                (
                    "replay_anchor_keys",
                    self.frozenset.call1((replay_anchor_keys,))?,
                ),
                (
                    "mutable_ref_warning_keys",
                    self.frozenset.call1((mutable_ref_warning_keys,))?,
                ),
                (
                    "external_source_replay_configs",
                    self.tuple(external_configs)?,
                ),
            ],
        )
    }
}

/// Map one complete logical project to adapter resources and planner desired state through
/// the adapter profile's connection-free callbacks.
#[pyfunction]
#[pyo3(signature = (*, project, adapter_profile, sql_analyzer))]
fn realize_project<'py>(
    py: Python<'py>,
    project: &Bound<'py, PyAny>,
    adapter_profile: &Bound<'py, PyAny>,
    sql_analyzer: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let realizer: Realizer<'py> = Realizer::import(py)?;
    let realize_source: Bound<'py, PyAny> = adapter_profile.getattr("realize_source")?;
    let realize_model: Bound<'py, PyAny> = adapter_profile.getattr("realize_model")?;
    let sources: Vec<Bound<'py, PyAny>> = realizer.items(&project.getattr("sources")?)?;
    let models: Vec<Bound<'py, PyAny>> = realizer.items(&project.getattr("models")?)?;
    let source_realizations: Bound<'py, PyDict> = PyDict::new(py);
    for source in &sources {
        let key: Bound<'py, PyAny> = source.getattr("key")?;
        let request: Bound<'py, PyAny> = realizer.source_request(source, project)?;
        let realization: Bound<'py, PyAny> =
            realizer.call(&realize_source, &[("request", request)])?;
        source_realizations.set_item(
            key,
            realizer.canonical_source_realization(&realization, sql_analyzer)?,
        )?;
    }
    let relation_names: Bound<'py, PyDict> = PyDict::new(py);
    for (key, realization) in source_realizations.iter() {
        relation_names.set_item(key, realization.getattr("relation_name")?)?;
    }
    for model in &models {
        relation_names.set_item(model.getattr("key")?, model.getattr("relation_name")?)?;
    }
    let relation_name_by_logical_name: Bound<'py, PyDict> = PyDict::new(py);
    for (key, relation_name) in relation_names.iter() {
        relation_name_by_logical_name.set_item(key.getattr("name")?, relation_name)?;
    }
    let relation_sql_by_name: Bound<'py, PyDict> = relation_name_by_logical_name.copy()?;
    let postgres_by_source_name: Bound<'py, PyDict> = PyDict::new(py);
    for source in &sources {
        let authored: Bound<'py, PyAny> = source.getattr("source")?;
        let name: Bound<'py, PyAny> = source.getattr("key")?.getattr("name")?;
        if authored.is_instance(&realizer.external_source)? {
            relation_sql_by_name.set_item(&name, realizer.external_relation_sql(&authored)?)?;
        }
        if authored.is_instance(&realizer.postgres_source)? {
            relation_sql_by_name.set_item(&name, realizer.postgres_relation_sql(&authored)?)?;
        }
    }
    for source in &sources {
        let authored: Bound<'py, PyAny> = source.getattr("source")?;
        if authored.is_instance(&realizer.postgres_source)? {
            postgres_by_source_name.set_item(source.getattr("key")?.getattr("name")?, authored)?;
        }
    }
    let model_realizations: Bound<'py, PyDict> = PyDict::new(py);
    let resolved_queries: Bound<'py, PyDict> = PyDict::new(py);
    let resolved_sql_by_key: Bound<'py, PyDict> = PyDict::new(py);
    let view_owner: Bound<'py, PyDict> = PyDict::new(py);
    let resolve: Bound<'py, PyAny> = sql_analyzer.getattr("resolve")?;
    for model in &models {
        let key: Bound<'py, PyAny> = model.getattr("key")?;
        let resolved_query: Bound<'py, PyAny> = realizer.call(
            &resolve,
            &[
                ("analysis", model.getattr("sql_analysis")?),
                ("resolver", relation_sql_by_name.clone().into_any()),
            ],
        )?;
        let request: Bound<'py, PyAny> = realizer.model_request(
            model,
            &resolved_query,
            &relation_names,
            &relation_name_by_logical_name,
            &postgres_by_source_name,
            sql_analyzer,
        )?;
        let realization: Bound<'py, PyAny> =
            realizer.call(&realize_model, &[("request", request)])?;
        let expected: Bound<'py, PyAny> = relation_names.as_any().get_item(&key)?;
        let realized: Bound<'py, PyAny> = realization.getattr("relation_name")?;
        if realized.ne(&expected)? {
            return Err(realizer.fail(format!(
                "Adapter realized model '{}' with relation '{}' after naming it '{}'",
                key.getattr("name")?.str()?,
                realized.str()?,
                expected.str()?
            )));
        }
        model_realizations.set_item(&key, &realization)?;
        let model_name: Bound<'py, PyAny> = key.getattr("name")?;
        for resource in realizer.items(&realization.getattr("resources")?)? {
            if !resource.is_instance(&realizer.adapter_materialized_view)? {
                continue;
            }
            let name: Bound<'py, PyAny> = resource.getattr("name")?;
            if let Some(owner) = view_owner.get_item(&name)?
                && !owner.is_none()
            {
                return Err(realizer.fail(format!(
                    "Models '{}' and '{}' both realize materialized view '{}'; rename one of \
                         them or set on_row_error = fail",
                    owner.str()?,
                    model_name.str()?,
                    name.str()?
                )));
            }
            view_owner.set_item(name, &model_name)?;
        }
        resolved_queries.set_item(&key, resolved_query.getattr("canonical_sql")?)?;
        resolved_sql_by_key.set_item(&key, &resolved_query)?;
    }
    let resources_by_key: Bound<'py, PyDict> = PyDict::new(py);
    for (key, realization) in source_realizations.iter() {
        resources_by_key.set_item(key, realization.getattr("resources")?)?;
    }
    for (key, realization) in model_realizations.iter() {
        resources_by_key.set_item(key, realization.getattr("resources")?)?;
    }
    let desired_state: Bound<'py, PyAny> = realizer.build_desired_state(
        project,
        &resolved_sql_by_key,
        &source_realizations,
        &model_realizations,
    )?;
    realizer.build(
        &py.import(PIPELINE_MODELS)?,
        "RealizedProject",
        &[
            ("project", project.clone()),
            ("resources_by_logical_key", resources_by_key.into_any()),
            ("relation_name_by_logical_key", relation_names.into_any()),
            ("resolved_query_by_model_key", resolved_queries.into_any()),
            ("desired_state", desired_state),
        ],
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(realize_project, module)?)
}
