//! Shapes of the compiled project: compile models, macros, the compilation lifecycle and access
//! policy, plus the authored spec, compile and access value enums, exceptions and constants.

use pyo3::exceptions::{PyException, PyValueError};
use pyo3::prelude::*;
use pyo3::pyclass::CompareOp;
use pyo3::types::{PyBytes, PyDict, PyFrozenSet, PyString, PyTuple};
use sha2::{Digest, Sha256};

use super::{
    Coerce, Given, add, add_class, base_init, exception, factory, native_enum, path, shapes,
    str_enum,
};
use crate::project_discovery::immutable_config_pairs;

const COMPILE_EXCEPTIONS: &str = "sqltide.compiler.compile.exceptions";
const COMPILE_TYPES: &str = "sqltide.compiler.compile.types";
const DISCOVERY_EXCEPTIONS: &str = "sqltide.compiler.discovery.exceptions";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const ACCESS_TYPES: &str = "sqltide.compiler.access.types";

const READ_ONLY_MAPPING: Coerce = Coerce::Maybe(&Coerce::ReadOnlyMapping);

fn truthy(value: &Bound<'_, PyAny>, other: &Bound<'_, PyAny>, op: CompareOp) -> PyResult<bool> {
    value.rich_compare(other, op)?.is_truthy()
}

shapes! {
    module = "sqltide.compiler.compile.models";

    /// Stable identity for a comparable deployed object.
    ObjectKey { database, object_type(coerce = Coerce::Enum("DesiredObjectType")), name }

    /// A normalized comparable column definition.
    Column { name, r#type, default(default = Given::None) }

    /// Normalized comparable Kafka engine settings.
    KafkaSettings {
        broker_list,
        topic,
        consumer_group,
        format,
        settings(default = Given::None, coerce = READ_ONLY_MAPPING),
    }

    /// Comparable Kafka table specification.
    KafkaTableSpec {
        columns,
        kafka,
        naming_macro_fingerprint(default = Given::None, compare = false),
    }

    /// Comparable managed-table storage definition.
    TableStorage {
        engine,
        order_by,
        partition_by(default = Given::None),
        ttl(default = Given::None),
        settings(default = Given::None, coerce = READ_ONLY_MAPPING),
    }

    /// Comparable managed-table specification.
    TableSpec { columns, storage }

    /// Comparable materialized-view specification.
    MaterializedViewSpec {
        source_table_name,
        target_table_name,
        query,
        database_template(default = Given::None, compare = false),
    }

    /// Comparable ordinary-view specification.
    ViewSpec { query, database_template(default = Given::None, compare = false) }

    /// A desired Kafka engine table for a landing step.
    DesiredKafkaTable { key, deps, spec }
    @impl {
        #[getter]
        fn name(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.key, &["name"])
        }

        #[getter]
        fn columns(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["columns"])
        }

        #[getter]
        fn kafka(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["kafka"])
        }
    }

    /// A desired managed ClickHouse table.
    DesiredTable {
        key,
        deps,
        spec,
        logical_model_name(default = Given::None, compare = false),
        replay_on_change(default = Given::None),
        bounded_replay_fallback(default = Given::Member("BoundedReplayFallback", "FULL")),
        replay_settings(default = Given::None, coerce = READ_ONLY_MAPPING, compare = false),
    }
    @impl {
        #[getter]
        fn name(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.key, &["name"])
        }

        #[getter]
        fn columns(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["columns"])
        }

        #[getter]
        fn engine(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["storage", "engine"])
        }

        #[getter]
        fn order_by(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["storage", "order_by"])
        }

        #[getter]
        fn partition_by(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["storage", "partition_by"])
        }

        #[getter]
        fn ttl(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["storage", "ttl"])
        }

        #[getter]
        fn settings(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["storage", "settings"])
        }
    }

    /// A desired materialized view between a source and target relation.
    DesiredMaterializedView {
        key,
        deps,
        spec,
        logical_model_name(default = Given::None, compare = false),
        refresh(default = Given::None, compare = false),
        append(default = Given::Bool(true), compare = false),
        filters_row_errors(default = Given::Bool(false), compare = false),
        dead_letters(default = Given::Bool(false), compare = false),
        authored_query(default = Given::None, compare = false),
        authored_database_template(default = Given::None, compare = false),
    }
    @impl {
        /// Return the spec change detection uses, ignoring the row-error rewrite.
        #[getter]
        fn comparison_spec(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            let query: &Bound<'_, PyAny> = self.authored_query.bind(py);
            if query.is_none() {
                return Ok(self.spec.clone_ref(py));
            }
            let template: &Bound<'_, PyAny> = self.authored_database_template.bind(py);
            let changes: Bound<'_, PyDict> = PyDict::new(py);
            changes.set_item("query", query)?;
            changes.set_item(
                "database_template",
                if template.is_truthy()? { template } else { query },
            )?;
            Ok(py
                .import("dataclasses")?
                .getattr("replace")?
                .call((&self.spec,), Some(&changes))?
                .unbind())
        }

        #[getter]
        fn name(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.key, &["name"])
        }

        #[getter]
        fn source_table_name(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["source_table_name"])
        }

        #[getter]
        fn target_table_name(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["target_table_name"])
        }

        #[getter]
        fn query(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["query"])
        }

        #[getter]
        fn database_template(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["database_template"])
        }
    }

    /// A desired ordinary query view.
    DesiredView { key, deps, spec, logical_model_name(default = Given::None, compare = false) }
    @impl {
        #[getter]
        fn name(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.key, &["name"])
        }

        #[getter]
        fn query(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["query"])
        }

        #[getter]
        fn database_template(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["database_template"])
        }
    }

    /// A parsed logical `__source(...)` or `__ref(...)` occurrence from transform SQL.
    ParsedRef {
        name,
        relation_type(coerce = Coerce::Enum("DiscoverySqlRelationType")),
        ref_type(default = Given::None),
        span(default = Given::None),
    }

    /// Stable identity for one selectable logical source or model.
    LogicalResourceKey { resource_type(coerce = Coerce::Enum("LogicalResourceType")), name }

    /// One semantically compiled logical streaming source.
    CompiledSource { key, source, effective_replay_lineage_mode }

    /// Effective typed retention choice and its authored scope.
    ModelRetentionResolution {
        value(default = Given::None),
        origin(default = Given::None, coerce = Coerce::Maybe(&Coerce::Enum("RetentionOrigin"))),
    }

    /// One semantically compiled logical model.
    CompiledModel {
        key,
        pipeline_name,
        relation_name,
        kind(coerce = Coerce::Enum("ModelKind")),
        sql_analysis,
    }
    @impl {
        #[getter]
        fn query(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.sql_analysis, &["authored_sql"])
        }

        #[getter]
        fn output_columns<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
            let mut columns: Vec<Bound<'py, PyAny>> = Vec::new();
            for column in self.sql_analysis.bind(py).getattr("output_columns")?.try_iter()? {
                let column: Bound<'py, PyAny> = column?;
                let shape: Column = Column {
                    name: column.getattr("name")?.unbind(),
                    r#type: column.getattr("type")?.unbind(),
                    default: py.None(),
                };
                columns.push(Bound::new(py, shape)?.into_any());
            }
            PyTuple::new(py, columns)
        }

        #[getter]
        fn parsed_refs<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
            let relation_type: Bound<'py, PyAny> = native_enum(py, "DiscoverySqlRelationType")?;
            let ref_type: Bound<'py, PyAny> = native_enum(py, "DiscoveryRefType")?;
            let mut refs: Vec<Bound<'py, PyAny>> = Vec::new();
            for reference in self.sql_analysis.bind(py).getattr("references")?.try_iter()? {
                let reference: Bound<'py, PyAny> = reference?;
                let fields: Bound<'py, PyDict> = PyDict::new(py);
                fields.set_item("name", reference.getattr("name")?)?;
                fields.set_item(
                    "relation_type",
                    relation_type.call1((reference.getattr("relation_type")?,))?,
                )?;
                let authored: Bound<'py, PyAny> = if reference.getattr("ref_type")?.is_none() {
                    py.None().into_bound(py)
                } else {
                    ref_type.call1((reference.getattr("ref_type")?,))?
                };
                fields.set_item("ref_type", authored)?;
                fields.set_item("span", reference.getattr("span")?)?;
                refs.push(py.get_type::<ParsedRef>().call((), Some(&fields))?);
            }
            PyTuple::new(py, refs)
        }

        #[getter]
        fn refs<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
            let mut names: Vec<Bound<'py, PyAny>> = Vec::new();
            for reference in self.sql_analysis.bind(py).getattr("references")?.try_iter()? {
                names.push(reference?.getattr("name")?);
            }
            PyTuple::new(py, names)
        }

        #[getter]
        fn has_aggregate_semantics(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.sql_analysis, &["aggregate_facts", "has_semantics"])
        }
    }

    /// One compiled streaming table model and its replay semantics.
    CompiledTableModel: CompiledModel {
        key,
        pipeline_name,
        relation_name,
        kind(coerce = Coerce::Enum("ModelKind")),
        sql_analysis,
        transform,
        preserves_required_lineage,
        replay_anchor_eligible,
        effective_bounded_replay_fallback,
        replay_on_change(default = Given::None),
        retention(default = Given::Factory(factory::<ModelRetentionResolution>)),
        retention_applied(default = Given::Bool(false)),
        on_row_error(default = Given::Member("RowErrorPolicy", "FAIL")),
    }
    @impl {
        #[getter]
        fn has_mutable_refs(&self, py: Python<'_>) -> PyResult<bool> {
            let reference_relation: Bound<'_, PyAny> =
                native_enum(py, "DiscoverySqlRelationType")?.getattr("REF")?;
            let mutable: Bound<'_, PyAny> = native_enum(py, "DiscoveryRefType")?.getattr("MUTABLE")?;
            for reference in self.sql_analysis.bind(py).getattr("references")?.try_iter()? {
                let reference: Bound<'_, PyAny> = reference?;
                if truthy(&reference.getattr("relation_type")?, &reference_relation, CompareOp::Eq)?
                    && truthy(
                        &reference.getattr("name")?,
                        &path(py, &self.transform, &["source"])?.into_bound(py),
                        CompareOp::Ne,
                    )?
                    && truthy(&reference.getattr("ref_type")?, &mutable, CompareOp::Eq)?
                {
                    return Ok(true);
                }
            }
            Ok(false)
        }
    }

    /// One compiled query-only terminal view model.
    CompiledViewModel: CompiledModel {
        key,
        pipeline_name,
        relation_name,
        kind(coerce = Coerce::Enum("ModelKind")),
        sql_analysis,
        view,
    }

    /// Replay metadata for one adopted external source root.
    ExternalSourceReplayConfig {
        key,
        table_name,
        source_kind(coerce = Coerce::Enum("SourceKind")),
        replay_boundary_mode(coerce = Coerce::Enum("ReplayBoundaryMode")),
        partition_column_name(default = Given::None),
        offset_column_name(default = Given::None),
        timestamp_column_name(default = Given::None),
        landed_at_column_name(default = Given::None),
        cursor_column_name(default = Given::None),
    }

    /// Logical sources and models compiled from one authored pipeline.
    CompiledPipeline {
        pipeline,
        project,
        file_path,
        effective_replay_lineage_mode,
        source,
        models,
    }

    /// Project-level flat desired object graph.
    DesiredState {
        objects,
        replay_anchor_keys,
        mutable_ref_warning_keys,
        external_source_replay_configs(default = Given::EmptyTuple),
    }
    @impl {
        /// Return whether a relation is a Kafka source's raw landing table.
        fn is_managed_landing_table(
            &self,
            py: Python<'_>,
            name: &Bound<'_, PyAny>,
        ) -> PyResult<bool> {
            let kafka_table: Bound<'_, PyAny> = py.get_type::<DesiredKafkaTable>().into_any();
            let view: Bound<'_, PyAny> = py.get_type::<DesiredMaterializedView>().into_any();
            let mut names: Vec<Bound<'_, PyAny>> = Vec::new();
            for desired in self.objects.bind(py).try_iter()? {
                let desired: Bound<'_, PyAny> = desired?;
                if desired.is_instance(&kafka_table)? {
                    names.push(desired.getattr("name")?);
                }
            }
            let kafka_tables: Bound<'_, PyFrozenSet> = PyFrozenSet::new(py, &names)?;
            for desired in self.objects.bind(py).try_iter()? {
                let desired: Bound<'_, PyAny> = desired?;
                if desired.is_instance(&view)?
                    && truthy(&desired.getattr("target_table_name")?, name, CompareOp::Eq)?
                    && kafka_tables
                        .as_any()
                        .contains(desired.getattr("source_table_name")?)?
                {
                    return Ok(true);
                }
            }
            Ok(false)
        }
    }

    /// Static SQL expression inference behavior exposed by an adapter.
    CompilerExpressionInferenceProfile { sql_analysis_dialect }

    /// Connection-free adapter defaults used during semantic compilation.
    CompilerTargetMetadata {
        default_database,
        default_schema,
        production_target(default = Given::Bool(false)),
        destruction_relation_drop_size_limit(default = Given::None),
    }

    /// Immutable compiler-facing adapter identity, analysis, target, and realization contract.
    CompilerAdapterProfile {
        identity,
        sql_analysis_dialect,
        type_inference_profile,
        target_metadata,
        realize_source,
        model_relation_name,
        realize_model,
        render_resource,
        render_set_difference_comparison,
        run_sql_rules(default = Given::Bool(true)),
    }

    /// Attached whole-project inputs used by semantic assembly.
    CompileProjectInputs {
        discovered_inputs,
        adapter_profile,
        effective_target,
        variables,
        macro_registry,
        macro_context,
        pipelines,
        tests,
        audits,
        sources(default = Given::EmptyTuple),
        virtual_environments(default = Given::Bool(false)),
        project_name(default = Given::None),
        target_name(default = Given::None),
        model_reference_scope(
            default = Given::Member("ModelReferenceScope", "PROJECT"),
            coerce = Coerce::Enum("ModelReferenceScope"),
        ),
        allowed_cross_pipeline_references(default = Given::EmptyTuple, coerce = Coerce::Tuple),
    }

    /// Current whole-project compile output consumed by every command.
    CompiledProject {
        sources,
        models,
        pipelines,
        tests,
        test_cases,
        audits,
        macro_registry(default = Given::Factory(factory::<MacroRegistry>)),
        macro_context(default = Given::None),
        project_name(default = Given::None),
        target_name(default = Given::None),
        production_target(default = Given::Bool(false)),
        destruction_relation_drop_size_limit(default = Given::None),
        model_reference_scope(
            default = Given::Member("ModelReferenceScope", "PROJECT"),
            coerce = Coerce::Enum("ModelReferenceScope"),
        ),
        allowed_cross_pipeline_references(default = Given::EmptyTuple, coerce = Coerce::Tuple),
    }
}

/// `MacroContext.variables`: the authored pairs frozen recursively, as a read-only mapping.
fn frozen_variables<'py>(variables: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = variables.py();
    let items: Bound<'py, PyAny> = py
        .get_type::<PyTuple>()
        .call1((variables.call_method0("items")?,))?;
    let pairs: Bound<'py, PyTuple> = immutable_config_pairs(py, items)?;
    py.import("types")?
        .getattr("MappingProxyType")?
        .call1((py.get_type::<PyDict>().call1((pairs,))?,))
}

shapes! {
    module = "sqltide.compiler.macros.models";

    /// One registered authored Python macro.
    LoadedMacro {
        name,
        file_path,
        relative_path,
        source,
        definition_line,
        function,
        description(default = Given::None),
    }
    @impl {
        /// Fingerprint the macro identity and retained implementation source.
        #[getter]
        fn identity_fingerprint(&self, py: Python<'_>) -> PyResult<String> {
            let parts: [Bound<'_, PyString>; 3] = [
                self.name.bind(py).str()?,
                self.relative_path.bind(py).call_method0("as_posix")?.str()?,
                self.source.bind(py).str()?,
            ];
            let payload: Bound<'_, PyBytes> = PyString::new(py, "\0")
                .call_method1("join", (parts,))?
                .call_method1("encode", ("utf-8",))?
                .cast_into()?;
            Ok(Sha256::digest(payload.as_bytes())
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect())
        }
    }

    /// Immutable macro definitions and invocation-local expansion source maps.
    MacroRegistry @no_repr {
        macros(default = Given::Factory(factory::<PyDict>), coerce = Coerce::ReadOnlyMapping),
        declarations(default = Given::None),
        expansions(default = Given::Factory(factory::<PyDict>), compare = false, repr = false),
    }

    /// Immutable authored pipeline metadata supplied to a naming macro.
    PipelineNamingContext { name, source_name, model_names, relative_directory, mode }

    /// Connection-free immutable context optionally injected into macros.
    MacroContext @no_repr {
        adapter_name,
        dialect,
        target_name,
        database,
        schema,
        virtual_environments,
        variables(coerce = Coerce::With(frozen_variables)),
    }
}

shapes! {
    module = "sqltide.compiler.pipeline.models";

    /// Elapsed milliseconds for each named project compilation phase.
    CompilationTimings {
        discovery_ms,
        compile_inputs_ms,
        assembly_ms,
        graph_ms,
        realization_ms,
        rules_ms(default = Given::Int(0)),
    }

    /// Adapter resources mapped back to their sole logical project aggregate.
    RealizedProject {
        project,
        resources_by_logical_key(coerce = Coerce::ReadOnlyMapping),
        relation_name_by_logical_key(coerce = Coerce::ReadOnlyMapping),
        resolved_query_by_model_key(coerce = Coerce::ReadOnlyMapping),
        desired_state,
    }

    /// Command-neutral output shared by every compile consumer.
    CompileAnalysis {
        discovered_inputs,
        compile_inputs,
        adapter_profile,
        compiled_project,
        realized_project,
        graph,
        diagnostics,
        timings,
        access_policy(default = Given::None),
        sensors(default = Given::None),
    }
}

shapes! {
    module = "sqltide.compiler.access.models";

    /// One normalized project, target, or exact-pipeline grant.
    CompiledAccessGrant {
        permissions,
        pipelines(default = Given::EmptyTuple),
        scope(default = Given::None),
    }

    /// One project-authored operational role.
    CompiledAccessRole { name, description, grants }

    /// Complete normalized policy compiled with one project analysis.
    CompiledAccessPolicy { roles, fingerprint }
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, message, *, span = None, location = None))]
fn pipeline_compile_init<'py>(
    instance: &Bound<'py, PyAny>,
    message: Bound<'py, PyAny>,
    span: Option<Bound<'py, PyAny>>,
    location: Option<Bound<'py, PyAny>>,
) -> PyResult<()> {
    base_init(instance, [message])?;
    instance.setattr("span", span)?;
    instance.setattr("location", location)?;
    instance.setattr("diagnostic", instance.py().None())
}

/// `TransformSqlContractError.__init__`: the transform name as `.args`, with no span or
/// diagnostic yet.
fn contract_init<'py>(
    instance: &Bound<'py, PyAny>,
    transform_name: Bound<'py, PyAny>,
) -> PyResult<()> {
    let py: Python<'py> = instance.py();
    base_init(instance, [transform_name.clone()])?;
    instance.setattr("transform_name", transform_name)?;
    instance.setattr("span", py.None())?;
    instance.setattr("diagnostic", py.None())
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, transform_name))]
fn transform_contract_init<'py>(
    instance: &Bound<'py, PyAny>,
    transform_name: Bound<'py, PyAny>,
) -> PyResult<()> {
    contract_init(instance, transform_name)
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, transform_name, query, details))]
fn transform_parse_init<'py>(
    instance: &Bound<'py, PyAny>,
    transform_name: Bound<'py, PyAny>,
    query: Bound<'py, PyAny>,
    details: Bound<'py, PyAny>,
) -> PyResult<()> {
    contract_init(instance, transform_name)?;
    instance.setattr("query", query)?;
    instance.setattr("details", details)
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, transform_name, statement_count))]
fn transform_statements_init<'py>(
    instance: &Bound<'py, PyAny>,
    transform_name: Bound<'py, PyAny>,
    statement_count: Bound<'py, PyAny>,
) -> PyResult<()> {
    contract_init(instance, transform_name)?;
    instance.setattr("statement_count", statement_count)
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, transform_name, statement_type))]
fn transform_shape_init<'py>(
    instance: &Bound<'py, PyAny>,
    transform_name: Bound<'py, PyAny>,
    statement_type: Bound<'py, PyAny>,
) -> PyResult<()> {
    contract_init(instance, transform_name)?;
    instance.setattr("statement_type", statement_type)
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, transform_name, statement_sql))]
fn transform_set_operation_init<'py>(
    instance: &Bound<'py, PyAny>,
    transform_name: Bound<'py, PyAny>,
    statement_sql: Bound<'py, PyAny>,
) -> PyResult<()> {
    contract_init(instance, transform_name)?;
    instance.setattr("statement_sql", statement_sql)
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, transform_name, column_index))]
fn transform_star_init<'py>(
    instance: &Bound<'py, PyAny>,
    transform_name: Bound<'py, PyAny>,
    column_index: Bound<'py, PyAny>,
) -> PyResult<()> {
    contract_init(instance, transform_name)?;
    instance.setattr("column_index", column_index)
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, transform_name, alias))]
fn transform_alias_init<'py>(
    instance: &Bound<'py, PyAny>,
    transform_name: Bound<'py, PyAny>,
    alias: Bound<'py, PyAny>,
) -> PyResult<()> {
    contract_init(instance, transform_name)?;
    instance.setattr("alias", alias)
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, transform_name, column_index, projection_sql))]
fn transform_untyped_init<'py>(
    instance: &Bound<'py, PyAny>,
    transform_name: Bound<'py, PyAny>,
    column_index: Bound<'py, PyAny>,
    projection_sql: Bound<'py, PyAny>,
) -> PyResult<()> {
    contract_init(instance, transform_name)?;
    instance.setattr("column_index", column_index)?;
    instance.setattr("projection_sql", projection_sql)
}

#[pyfunction]
#[pyo3(
    name = "__init__",
    signature = (instance, transform_name, expression, unknown_column_names, available_column_names)
)]
fn transform_unknown_column_init<'py>(
    instance: &Bound<'py, PyAny>,
    transform_name: Bound<'py, PyAny>,
    expression: Bound<'py, PyAny>,
    unknown_column_names: Bound<'py, PyAny>,
    available_column_names: Bound<'py, PyAny>,
) -> PyResult<()> {
    contract_init(instance, transform_name)?;
    instance.setattr("expression", expression)?;
    instance.setattr("unknown_column_names", unknown_column_names)?;
    instance.setattr("available_column_names", available_column_names)
}

/// `str(instance.<name>)`, as an f-string placeholder renders it.
fn field(instance: &Bound<'_, PyAny>, name: &str) -> PyResult<String> {
    Ok(instance.getattr(name)?.str()?.to_str()?.to_owned())
}

#[pyfunction]
#[pyo3(name = "__str__")]
fn transform_parse_str(instance: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(format!(
        "Transform '{}' contains invalid SQL and could not be parsed: {}",
        field(instance, "transform_name")?,
        field(instance, "details")?
    ))
}

#[pyfunction]
#[pyo3(name = "__str__")]
fn transform_statements_str(instance: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(format!(
        "Transform '{}' must contain exactly one SQL statement, but found {}.",
        field(instance, "transform_name")?,
        field(instance, "statement_count")?
    ))
}

#[pyfunction]
#[pyo3(name = "__str__")]
fn transform_shape_str(instance: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(format!(
        "Transform '{}' must end in one outermost SELECT that defines the output schema, but \
         found top-level statement type '{}'.",
        field(instance, "transform_name")?,
        field(instance, "statement_type")?
    ))
}

#[pyfunction]
#[pyo3(name = "__str__")]
fn transform_set_operation_str(instance: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(format!(
        "Transform '{}' must end in one outermost SELECT that defines the output schema. \
         Top-level set operations like UNION or UNION ALL are not allowed. Move the set \
         operation into a CTE or subquery, then add a final SELECT with typed projections like \
         CAST(expr AS Type) AS name or expr::Type AS name.",
        field(instance, "transform_name")?
    ))
}

#[pyfunction]
#[pyo3(name = "__str__")]
fn transform_star_str(instance: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(format!(
        "Transform '{}' has an invalid output contract at column {}. Wildcard projections like \
         '*' are not allowed in the outermost SELECT. Use explicit typed projections like \
         CAST(expr AS Type) AS name or expr::Type AS name.",
        field(instance, "transform_name")?,
        field(instance, "column_index")?
    ))
}

#[pyfunction]
#[pyo3(name = "__str__")]
fn transform_alias_str(instance: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(format!(
        "Transform '{}' has an invalid output contract. Duplicate outermost SELECT alias '{}' is \
         not allowed. Each derived output column must appear exactly once.",
        field(instance, "transform_name")?,
        field(instance, "alias")?
    ))
}

#[pyfunction]
#[pyo3(name = "__str__")]
fn transform_untyped_str(instance: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(format!(
        "Transform '{}' has an invalid output contract at column {}. Expected outermost \
         projection {} to use an explicit typed alias like CAST(expr AS Type) AS name or \
         expr::Type AS name, but found `{}`.",
        field(instance, "transform_name")?,
        field(instance, "column_index")?,
        field(instance, "column_index")?,
        field(instance, "projection_sql")?
    ))
}

/// The unknown-column message for one storage clause, naming both column lists.
fn unknown_column_str(instance: &Bound<'_, PyAny>, clause: &str) -> PyResult<String> {
    let py: Python<'_> = instance.py();
    let separator: Bound<'_, PyString> = PyString::new(py, ", ");
    let unknown: Bound<'_, PyAny> =
        separator.call_method1("join", (instance.getattr("unknown_column_names")?,))?;
    let available: Bound<'_, PyAny> =
        separator.call_method1("join", (instance.getattr("available_column_names")?,))?;
    Ok(format!(
        "Transform '{}' has an invalid {clause} expression `{}`. Referenced output columns not \
         found in the derived schema: {}. Available columns: {}.",
        field(instance, "transform_name")?,
        field(instance, "expression")?,
        unknown.str()?,
        available.str()?
    ))
}

#[pyfunction]
#[pyo3(name = "__str__")]
fn order_by_unknown_str(instance: &Bound<'_, PyAny>) -> PyResult<String> {
    unknown_column_str(instance, "ORDER BY")
}

#[pyfunction]
#[pyo3(name = "__str__")]
fn partition_by_unknown_str(instance: &Bound<'_, PyAny>) -> PyResult<String> {
    unknown_column_str(instance, "PARTITION BY")
}

#[pyfunction]
#[pyo3(name = "__str__")]
fn ttl_unknown_str(instance: &Bound<'_, PyAny>) -> PyResult<String> {
    unknown_column_str(instance, "TTL")
}

fn empty_tuple() -> Py<PyAny> {
    Python::attach(|py| PyTuple::empty(py).into_any().unbind())
}

#[pyfunction]
#[pyo3(
    name = "__init__",
    signature = (instance, message, *, location = None, related_locations = empty_tuple())
)]
fn macro_init<'py>(
    instance: &Bound<'py, PyAny>,
    message: Bound<'py, PyAny>,
    location: Option<Bound<'py, PyAny>>,
    related_locations: Py<PyAny>,
) -> PyResult<()> {
    let py: Python<'py> = instance.py();
    base_init(instance, [message.clone()])?;
    instance.setattr("location", &location)?;
    let fields: Bound<'py, PyDict> = PyDict::new(py);
    fields.set_item(
        "phase",
        native_enum(py, "DiagnosticPhase")?.getattr("COMPILATION")?,
    )?;
    fields.set_item(
        "severity",
        native_enum(py, "DiagnosticSeverity")?.getattr("ERROR")?,
    )?;
    fields.set_item("code", "SQT-COMPILE-001")?;
    fields.set_item("message", message)?;
    fields.set_item("location", location)?;
    fields.set_item("related_locations", related_locations)?;
    let diagnostic: Bound<'py, PyAny> = py
        .import("sqltide.diagnostics.models")?
        .getattr("CompilerDiagnostic")?
        .call((), Some(&fields))?;
    instance.setattr("diagnostic", diagnostic)
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, message))]
fn pipeline_discovery_init<'py>(
    instance: &Bound<'py, PyAny>,
    message: Bound<'py, PyAny>,
) -> PyResult<()> {
    base_init(instance, [message])?;
    instance.setattr("diagnostic", instance.py().None())
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, message, *, location))]
fn access_policy_init<'py>(
    instance: &Bound<'py, PyAny>,
    message: Bound<'py, PyAny>,
    location: Bound<'py, PyAny>,
) -> PyResult<()> {
    base_init(instance, [message])?;
    instance.setattr("location", location)
}

/// A native enum: its `sqltide._native` attribute, class name, docstring and members.
type EnumSpec = (
    &'static str,
    &'static str,
    Option<&'static str>,
    &'static [(&'static str, &'static str)],
);

/// The authored spec enums of `sqltide.compiler.discovery.types`.
const DISCOVERY_ENUMS: [EnumSpec; 17] = [
    (
        "PipelineMode",
        "PipelineMode",
        None,
        &[("DIRECT", "direct"), ("VIRTUAL", "virtual")],
    ),
    (
        "RetentionMissingBehavior",
        "RetentionMissingBehavior",
        None,
        &[("ERROR", "error"), ("SKIP", "skip")],
    ),
    (
        "KafkaRetentionReference",
        "KafkaRetentionReference",
        None,
        &[("BROKER", "broker"), ("LANDED", "landed")],
    ),
    (
        "KafkaRetentionOrigin",
        "KafkaRetentionOrigin",
        None,
        &[("SOURCE", "source"), ("PROJECT", "project")],
    ),
    (
        "ModelReferenceScope",
        "ModelReferenceScope",
        None,
        &[("PROJECT", "project"), ("PIPELINE", "pipeline")],
    ),
    (
        "RowErrorPolicy",
        "RowErrorPolicy",
        Some("What a streaming model does with a row its typed projection cannot produce."),
        &[("FAIL", "fail"), ("QUARANTINE", "quarantine")],
    ),
    (
        "ReplayAnchorMode",
        "ReplayAnchorMode",
        None,
        &[("AUTO", "auto"), ("NEVER", "never")],
    ),
    (
        "ReplayLineageMode",
        "ReplayLineageMode",
        None,
        &[
            ("OFFSETS", "offsets"),
            ("TIMESTAMP", "timestamp"),
            ("LANDED_AT", "landed_at"),
            ("CURSOR", "cursor"),
        ],
    ),
    (
        "SourceKind",
        "SourceKind",
        None,
        &[
            ("KAFKA", "kafka"),
            ("STREAM_TABLE", "stream_table"),
            ("POSTGRES", "postgres"),
        ],
    ),
    (
        "SourceNameOrigin",
        "SourceNameOrigin",
        None,
        &[("EXPLICIT", "explicit"), ("DERIVED", "derived")],
    ),
    (
        "ModelKind",
        "ModelKind",
        None,
        &[("TABLE", "table"), ("VIEW", "view")],
    ),
    (
        "ReplayBoundaryMode",
        "ReplayBoundaryMode",
        None,
        &[
            ("OFFSETS", "offsets"),
            ("TIMESTAMP", "timestamp"),
            ("LANDED_AT", "landed_at"),
            ("CURSOR", "cursor"),
        ],
    ),
    (
        "BoundedReplayFallback",
        "BoundedReplayFallback",
        None,
        &[
            ("FULL", "full"),
            ("BOUNDED_WITHOUT_HISTORY", "bounded_without_history"),
        ],
    ),
    (
        "DiscoveryRefType",
        "RefType",
        None,
        &[("REFERENCE", "reference"), ("MUTABLE", "mutable")],
    ),
    (
        "DiscoverySqlRelationType",
        "SqlRelationType",
        None,
        &[("SOURCE", "source"), ("REF", "ref")],
    ),
    (
        "ReplayOnChangeMode",
        "ReplayOnChangeMode",
        None,
        &[("FULL", "full"), ("BOUNDED", "bounded")],
    ),
    (
        "SchemaChangeKind",
        "SchemaChangeKind",
        None,
        &[("BREAKING", "breaking"), ("NON_BREAKING", "non_breaking")],
    ),
];

fn enums(module: &Bound<'_, PyModule>) -> PyResult<()> {
    for (attribute, qualname, doc, members) in DISCOVERY_ENUMS {
        str_enum(module, attribute, DISCOVERY_TYPES, qualname, doc, members)?;
    }
    str_enum(
        module,
        "DesiredObjectType",
        COMPILE_TYPES,
        "DesiredObjectType",
        None,
        &[
            ("KAFKA_TABLE", "kafka_table"),
            ("TABLE", "table"),
            ("MATERIALIZED_VIEW", "materialized_view"),
            ("VIEW", "view"),
        ],
    )?;
    str_enum(
        module,
        "LogicalResourceType",
        COMPILE_TYPES,
        "LogicalResourceType",
        Some("Kinds of selectable logical compiler resources."),
        &[("SOURCE", "source"), ("MODEL", "model")],
    )?;
    str_enum(
        module,
        "RetentionOrigin",
        COMPILE_TYPES,
        "RetentionOrigin",
        None,
        &[
            ("MODEL", "model"),
            ("PIPELINE", "pipeline"),
            ("PROJECT", "project"),
        ],
    )?;
    str_enum(
        module,
        "Permission",
        ACCESS_TYPES,
        "Permission",
        Some("Finite operation vocabulary enforced by SQLTide."),
        &[
            ("PROJECT_RELOAD", "project.reload"),
            ("SOURCE_MESSAGES_READ", "source.messages.read"),
            ("QUALITY_TEST_RUN", "quality.test.run"),
            ("QUALITY_AUDIT_RUN", "quality.audit.run"),
            ("BUILD_DIRECT_RUN", "build.direct.run"),
            ("DEPLOYMENT_CREATE", "deployment.create"),
            ("BUILD_CANCEL", "build.cancel"),
            ("BUILD_KILL", "build.kill"),
            ("DEPLOYMENT_PROMOTE", "deployment.promote"),
            ("DEPLOYMENT_CLEANUP", "deployment.cleanup"),
            ("PIPELINE_DESTROY", "pipeline.destroy"),
            ("TARGET_RESET", "target.reset"),
            ("AUTOMATION_MANAGE", "automation.manage"),
            ("ACCOUNT_MANAGE", "account.manage"),
            ("ROLE_ASSIGN", "role.assign"),
        ],
    )?;
    str_enum(
        module,
        "GrantScope",
        ACCESS_TYPES,
        "GrantScope",
        Some("Explicit non-pipeline grant scopes accepted in project policy."),
        &[("PROJECT", "project"), ("TARGET", "target")],
    )
}

fn exceptions(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py: Python<'_> = module.py();
    let value_error: Bound<'_, PyAny> = py.get_type::<PyValueError>().into_any();
    exception(
        module,
        COMPILE_EXCEPTIONS,
        "PipelineCompileError",
        "Raised when pipeline compilation input or state is invalid.",
        std::slice::from_ref(&value_error),
        Some(wrap_pyfunction!(pipeline_compile_init, module)?.into_any()),
        None,
    )?;
    exception(
        module,
        COMPILE_EXCEPTIONS,
        "AuditCompileError",
        "Raised when audit compilation input or state is invalid.",
        std::slice::from_ref(&value_error),
        None,
        None,
    )?;
    let contract: Bound<'_, PyAny> = exception(
        module,
        COMPILE_EXCEPTIONS,
        "TransformSqlContractError",
        "Base error for transform SQL contract validation failures.",
        &[py.get_type::<PyException>().into_any()],
        Some(wrap_pyfunction!(transform_contract_init, module)?.into_any()),
        None,
    )?;
    let unknown_column_init: Bound<'_, PyAny> =
        wrap_pyfunction!(transform_unknown_column_init, module)?.into_any();
    let contract_errors: [(&str, &str, Bound<'_, PyAny>, Bound<'_, PyAny>); 10] = [
        (
            "TransformSqlParseError",
            "Raised when transform SQL cannot be parsed.",
            wrap_pyfunction!(transform_parse_init, module)?.into_any(),
            wrap_pyfunction!(transform_parse_str, module)?.into_any(),
        ),
        (
            "TransformSqlMultipleStatementsError",
            "Raised when transform SQL contains multiple statements.",
            wrap_pyfunction!(transform_statements_init, module)?.into_any(),
            wrap_pyfunction!(transform_statements_str, module)?.into_any(),
        ),
        (
            "TransformSqlFinalQueryShapeError",
            "Raised when transform SQL does not expose one outermost final SELECT.",
            wrap_pyfunction!(transform_shape_init, module)?.into_any(),
            wrap_pyfunction!(transform_shape_str, module)?.into_any(),
        ),
        (
            "TransformSqlTopLevelSetOperationError",
            "Raised when transform SQL uses a top-level set operation.",
            wrap_pyfunction!(transform_set_operation_init, module)?.into_any(),
            wrap_pyfunction!(transform_set_operation_str, module)?.into_any(),
        ),
        (
            "TransformSqlStarProjectionError",
            "Raised when the outermost projection uses '*'.",
            wrap_pyfunction!(transform_star_init, module)?.into_any(),
            wrap_pyfunction!(transform_star_str, module)?.into_any(),
        ),
        (
            "TransformSqlDuplicateAliasError",
            "Raised when the outermost projection repeats an output alias.",
            wrap_pyfunction!(transform_alias_init, module)?.into_any(),
            wrap_pyfunction!(transform_alias_str, module)?.into_any(),
        ),
        (
            "TransformSqlUntypedProjectionError",
            "Raised when an outermost projection is not an explicitly typed alias.",
            wrap_pyfunction!(transform_untyped_init, module)?.into_any(),
            wrap_pyfunction!(transform_untyped_str, module)?.into_any(),
        ),
        (
            "TransformOrderByUnknownColumnError",
            "Raised when an ORDER BY expression references unknown derived columns.",
            unknown_column_init.clone(),
            wrap_pyfunction!(order_by_unknown_str, module)?.into_any(),
        ),
        (
            "TransformPartitionByUnknownColumnError",
            "Raised when a PARTITION BY expression references unknown derived columns.",
            unknown_column_init.clone(),
            wrap_pyfunction!(partition_by_unknown_str, module)?.into_any(),
        ),
        (
            "TransformTtlUnknownColumnError",
            "Raised when a TTL expression references unknown derived columns.",
            unknown_column_init,
            wrap_pyfunction!(ttl_unknown_str, module)?.into_any(),
        ),
    ];
    for (name, doc, init, str) in contract_errors {
        exception(
            module,
            COMPILE_EXCEPTIONS,
            name,
            doc,
            std::slice::from_ref(&contract),
            Some(init),
            Some(str),
        )?;
    }
    exception(
        module,
        "sqltide.compiler.macros.exceptions",
        "MacroError",
        "Raised when macros input or state is invalid.",
        std::slice::from_ref(&value_error),
        Some(wrap_pyfunction!(macro_init, module)?.into_any()),
        None,
    )?;
    exception(
        module,
        DISCOVERY_EXCEPTIONS,
        "PipelineDiscoveryError",
        "Raised when discovery input or state is invalid.",
        std::slice::from_ref(&value_error),
        Some(wrap_pyfunction!(pipeline_discovery_init, module)?.into_any()),
        None,
    )?;
    exception(
        module,
        DISCOVERY_EXCEPTIONS,
        "ModelHeaderSyntaxError",
        "Raised when a MODEL header does not follow SQLBuild syntax.",
        std::slice::from_ref(&value_error),
        None,
        None,
    )?;
    let spec: Bound<'_, PyAny> = exception(
        module,
        DISCOVERY_EXCEPTIONS,
        "ProjectSpecError",
        "Raised when authored specification input or state is invalid.",
        std::slice::from_ref(&value_error),
        None,
        None,
    )?;
    let spec_errors: [(&str, &str); 3] = [
        (
            "ProjectConfigError",
            "Raised when project or local configuration is invalid.",
        ),
        (
            "DurationParseError",
            "Raised when a compact authored duration is invalid.",
        ),
        (
            "RetentionConfigError",
            "Raised when a typed retention policy is invalid.",
        ),
    ];
    for (name, doc) in spec_errors {
        exception(
            module,
            DISCOVERY_EXCEPTIONS,
            name,
            doc,
            std::slice::from_ref(&spec),
            None,
            None,
        )?;
    }
    exception(
        module,
        "sqltide.compiler.access.exceptions",
        "AccessPolicyError",
        "Authored access policy is unsafe or invalid.",
        std::slice::from_ref(&value_error),
        Some(wrap_pyfunction!(access_policy_init, module)?.into_any()),
        None,
    )?;
    Ok(())
}

fn constants(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py: Python<'_> = module.py();
    let object_type: Bound<'_, PyAny> = native_enum(py, "DesiredObjectType")?;
    for member in ["KAFKA_TABLE", "TABLE", "MATERIALIZED_VIEW", "VIEW"] {
        add(
            module,
            &format!("DESIRED_OBJECT_TYPE_{member}"),
            object_type.getattr(member)?,
        )?;
    }
    add(module, "RAW_TABLE_NAME_PREFIX", "raw__")?;
    add(module, "MATERIALIZED_VIEW_NAME_PREFIX", "mv__")?;
    add(module, "REPLAY_PARTITION_COLUMN_NAME", "_replay_partition")?;
    add(module, "REPLAY_OFFSET_COLUMN_NAME", "_replay_offset")?;
    add(module, "REPLAY_TIMESTAMP_COLUMN_NAME", "_replay_timestamp")?;
    add(module, "REPLAY_LANDED_AT_COLUMN_NAME", "_replay_landed_at")?;
    add(module, "REPLAY_CURSOR_COLUMN_NAME", "_replay_cursor")?;
    let seconds: Bound<'_, PyDict> = PyDict::new(py);
    for (unit, count) in [("d", 24 * 60 * 60), ("h", 60 * 60), ("m", 60), ("s", 1)] {
        seconds.set_item(unit, count)?;
    }
    add(module, "SECONDS_BY_DURATION_UNIT", seconds)?;
    add(module, "ACCESS_POLICY_FILE_NAME", "access.yml")
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    enums(module)?;
    exceptions(module)?;
    constants(module)?;
    add_class::<ObjectKey>(module)?;
    add_class::<Column>(module)?;
    add_class::<KafkaSettings>(module)?;
    add_class::<KafkaTableSpec>(module)?;
    add_class::<TableStorage>(module)?;
    add_class::<TableSpec>(module)?;
    add_class::<MaterializedViewSpec>(module)?;
    add_class::<ViewSpec>(module)?;
    add_class::<DesiredKafkaTable>(module)?;
    add_class::<DesiredTable>(module)?;
    add_class::<DesiredMaterializedView>(module)?;
    add_class::<DesiredView>(module)?;
    add_class::<ParsedRef>(module)?;
    add_class::<LogicalResourceKey>(module)?;
    add_class::<CompiledSource>(module)?;
    add_class::<ModelRetentionResolution>(module)?;
    add_class::<CompiledModel>(module)?;
    add_class::<CompiledTableModel>(module)?;
    add_class::<CompiledViewModel>(module)?;
    add_class::<ExternalSourceReplayConfig>(module)?;
    add_class::<CompiledPipeline>(module)?;
    add_class::<DesiredState>(module)?;
    add_class::<CompilerExpressionInferenceProfile>(module)?;
    add_class::<CompilerTargetMetadata>(module)?;
    add_class::<CompilerAdapterProfile>(module)?;
    add_class::<CompileProjectInputs>(module)?;
    add_class::<CompiledProject>(module)?;
    add_class::<LoadedMacro>(module)?;
    add_class::<MacroRegistry>(module)?;
    add_class::<PipelineNamingContext>(module)?;
    add_class::<MacroContext>(module)?;
    add_class::<CompilationTimings>(module)?;
    add_class::<RealizedProject>(module)?;
    add_class::<CompileAnalysis>(module)?;
    add_class::<CompiledAccessGrant>(module)?;
    add_class::<CompiledAccessRole>(module)?;
    add_class::<CompiledAccessPolicy>(module)
}
