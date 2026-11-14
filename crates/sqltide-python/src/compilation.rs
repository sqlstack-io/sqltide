//! Python bindings that compile loaded pipelines into compiled sources and models, and assemble
//! one compiled project, through the invocation's SQL analyzer and reference rewriter.

use std::collections::HashMap;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use sqltide_compile::audits::{self, AuditDefaults, AuditPolicyInput, PipelineAuditScope};
use sqltide_compile::relations::{self, ModelClaim, ModelNaming, ModelRelationKind, SourceClaim};
use sqltide_compile::retention::{self, ModelRetention};
use sqltide_compile::transforms::{self, Reference};
use sqltide_discovery::model_spec::DEFAULT_SQL_MODEL_ENGINE;

use crate::testing::{build_audit_quality_identity, build_sql_test_cases, replace_refs};

const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const COMPILE_TYPES: &str = "sqltide.compiler.compile.types";
const COMPILE_EXCEPTIONS: &str = "sqltide.compiler.compile.exceptions";
const DISCOVERY_MODELS: &str = "sqltide.compiler.discovery.models";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const AUDIT_TYPES: &str = "sqltide.compiler.audit_discovery.types";
const SQL_ANALYSIS_EXCEPTIONS: &str = "sqltide.compiler.sql_analysis.exceptions";
const SQL_ANALYSIS_TYPES: &str = "sqltide.compiler.sql_analysis.types";
const DIAGNOSTICS_MODELS: &str = "sqltide.diagnostics.models";
const DIAGNOSTICS_TYPES: &str = "sqltide.diagnostics.types";

/// One transform's `(name, relation type, ref type)` references in query order.
type References = Vec<(String, String, Option<String>)>;

/// The modules compilation reads, imported once per call.
struct Compiler<'py> {
    py: Python<'py>,
    analyzer: Bound<'py, PyAny>,
    replace: Bound<'py, PyAny>,
    value_error: Bound<'py, PyAny>,
    models: Bound<'py, PyModule>,
    compile_types: Bound<'py, PyModule>,
    exceptions: Bound<'py, PyModule>,
    discovery: Bound<'py, PyModule>,
    discovery_types: Bound<'py, PyModule>,
    sql_errors: Bound<'py, PyModule>,
}

impl<'py> Compiler<'py> {
    fn import(py: Python<'py>, analyzer: &Bound<'py, PyAny>) -> PyResult<Self> {
        Ok(Self {
            py,
            analyzer: analyzer.clone(),
            replace: py.import("dataclasses")?.getattr("replace")?,
            value_error: py.import("builtins")?.getattr("ValueError")?,
            models: py.import(COMPILE_MODELS)?,
            compile_types: py.import(COMPILE_TYPES)?,
            exceptions: py.import(COMPILE_EXCEPTIONS)?,
            discovery: py.import(DISCOVERY_MODELS)?,
            discovery_types: py.import(DISCOVERY_TYPES)?,
            sql_errors: py.import(SQL_ANALYSIS_EXCEPTIONS)?,
        })
    }

    fn none(&self) -> Bound<'py, PyAny> {
        self.py.None().into_bound(self.py)
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
        value: &Bound<'py, PyAny>,
        changes: &[(&str, Bound<'py, PyAny>)],
    ) -> PyResult<Bound<'py, PyAny>> {
        let keywords: Bound<'py, PyDict> = PyDict::new(self.py);
        for (name, change) in changes {
            keywords.set_item(name, change)?;
        }
        self.replace.call((value,), Some(&keywords))
    }

    fn member(
        &self,
        module: &Bound<'py, PyModule>,
        class: &str,
        name: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        module.getattr(class)?.getattr(name)
    }

    /// `Enum(value)` for one of the discovery enums.
    fn discovery_enum(
        &self,
        class: &str,
        value: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.discovery_types.getattr(class)?.call1((value,))
    }

    fn is_a(&self, value: &Bound<'py, PyAny>, class: &str) -> PyResult<bool> {
        value.is_instance(&self.discovery.getattr(class)?)
    }

    fn items(value: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
        value.try_iter()?.collect()
    }

    fn tuple(&self, items: Vec<Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
        Ok(PyTuple::new(self.py, items)?.into_any())
    }

    /// `PipelineCompileError(message)` raised outside any handler.
    fn compile_error(&self, message: String) -> PyResult<PyErr> {
        Ok(PyErr::from_value(
            self.exceptions
                .getattr("PipelineCompileError")?
                .call1((message,))?,
        ))
    }

    /// `PipelineCompileError(message)` raised `from None` while handling `ValueError(message)`.
    fn compile_error_from_none(&self, message: String) -> PyResult<PyErr> {
        self.error_from_none("PipelineCompileError", message)
    }

    fn error_from_none(&self, class: &str, message: String) -> PyResult<PyErr> {
        let context: Bound<'py, PyAny> = self.value_error.call1((&message,))?;
        let error: Bound<'py, PyAny> = self.exceptions.getattr(class)?.call1((message,))?;
        error.setattr("__context__", context)?;
        error.setattr("__cause__", self.none())?;
        Ok(PyErr::from_value(error))
    }

    fn logical_key(
        &self,
        resource_type: &str,
        name: Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.build(
            &self.models,
            "LogicalResourceKey",
            &[
                (
                    "resource_type",
                    self.member(&self.compile_types, "LogicalResourceType", resource_type)?,
                ),
                ("name", name),
            ],
        )
    }

    /// The replay lineage mode one source-owned physical replay contract selects.
    fn source_lineage_mode(&self, source: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        if self.is_a(source, "ExternalTableSourceStep")? {
            let mode: Bound<'py, PyAny> = source.getattr("replay_boundary")?.getattr("mode")?;
            if let Some(lineage_mode) = self.boundary_lineage_mode(&mode)? {
                return Ok(lineage_mode);
            }
            return Err(self.compile_error(format!(
                "External source replay boundary mode '{}' is not supported by compile/backfill",
                mode.str()?
            ))?);
        }
        if self.is_a(source, "KafkaLandingStep")? {
            let boundary: Bound<'py, PyAny> = source.getattr("replay_boundary")?;
            if !boundary.is_none()
                && let Some(lineage_mode) =
                    self.boundary_lineage_mode(&boundary.getattr("mode")?)?
            {
                return Ok(lineage_mode);
            }
        }
        self.member(&self.discovery_types, "ReplayLineageMode", "OFFSETS")
    }

    /// The lineage mode a replay boundary mode maps to; every boundary mode has one.
    fn boundary_lineage_mode(
        &self,
        mode: &Bound<'py, PyAny>,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        let Ok(mode) = mode.extract::<String>() else {
            return Ok(None);
        };
        let member: &str = match mode.as_str() {
            "offsets" => "OFFSETS",
            "timestamp" => "TIMESTAMP",
            "landed_at" => "LANDED_AT",
            "cursor" => "CURSOR",
            _ => return Ok(None),
        };
        self.member(&self.discovery_types, "ReplayLineageMode", member)
            .map(Some)
    }

    fn compiled_source(
        &self,
        source: &Bound<'py, PyAny>,
        lineage_mode: Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.build(
            &self.models,
            "CompiledSource",
            &[
                ("key", self.logical_key("SOURCE", source.getattr("name")?)?),
                ("source", source.clone()),
                ("effective_replay_lineage_mode", lineage_mode),
            ],
        )
    }

    fn compile_pipeline(&self, loaded_pipeline: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let pipeline: Bound<'py, PyAny> = loaded_pipeline.getattr("pipeline")?;
        let source: Bound<'py, PyAny> = pipeline.getattr("source")?;
        let lineage_mode: Option<Bound<'py, PyAny>> = if source.is_none() {
            None
        } else {
            Some(self.source_lineage_mode(&source)?)
        };
        let compiled_source: Bound<'py, PyAny> = match &lineage_mode {
            Some(mode) => self.compiled_source(&source, mode.clone())?,
            None => self.none(),
        };
        let mut models: Vec<Bound<'py, PyAny>> = Vec::new();
        for model in Self::items(&pipeline.getattr("transforms")?)? {
            let compiled: Bound<'py, PyAny> =
                match self.compile_model(loaded_pipeline, &model, lineage_mode.as_ref()) {
                    Ok(compiled) => compiled,
                    Err(error) => {
                        return Err(self.with_diagnostic(error, &model, loaded_pipeline)?);
                    }
                };
            models.push(compiled);
        }
        self.build(
            &self.models,
            "CompiledPipeline",
            &[
                ("pipeline", pipeline),
                ("project", loaded_pipeline.getattr("project")?),
                ("file_path", loaded_pipeline.getattr("file_path")?),
                (
                    "effective_replay_lineage_mode",
                    lineage_mode.unwrap_or_else(|| self.none()),
                ),
                ("source", compiled_source),
                ("models", self.tuple(models)?),
            ],
        )
    }

    fn compile_model(
        &self,
        loaded_pipeline: &Bound<'py, PyAny>,
        model: &Bound<'py, PyAny>,
        lineage_mode: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let pipeline: Bound<'py, PyAny> = loaded_pipeline.getattr("pipeline")?;
        let project: Bound<'py, PyAny> = loaded_pipeline.getattr("project")?;
        let pipeline_dir: Bound<'py, PyAny> = loaded_pipeline.getattr("file_path")?;
        let relation_name: Bound<'py, PyAny> =
            self.model_relation_name(model, &pipeline, &project)?;
        if self.is_a(model, "ViewStep")? {
            return self.compile_view(model, &pipeline, &pipeline_dir, relation_name);
        }
        let Some(lineage_mode) = lineage_mode else {
            return Err(self.compile_error(format!(
                "Table model '{}' belongs to a source-less pipeline",
                model.getattr("name")?.str()?
            ))?);
        };
        let project_ttl: Option<Bound<'py, PyAny>> = if model.getattr("ttl")?.is_none()
            && model.getattr("retention")?.is_none()
            && pipeline.getattr("model_retention")?.is_none()
            && !project.is_none()
        {
            Some(project.getattr("model_ttl")?).filter(|ttl| !ttl.is_none())
        } else {
            None
        };
        let mut transform: Bound<'py, PyAny> = match project_ttl {
            Some(ttl) => self.replace(model, &[("ttl", ttl)])?,
            None => model.clone(),
        };
        let execution_settings: Bound<'py, PyAny> =
            self.execution_settings(&pipeline, &transform)?;
        transform = self.replace(&transform, &[("execution_settings", execution_settings)])?;
        let replay_on_change: Bound<'py, PyAny> = self.first_set(
            &transform,
            &pipeline,
            &project,
            "replay_on_change",
            "replay_on_change",
        )?;
        let bounded_replay_fallback: Bound<'py, PyAny> =
            self.bounded_replay_fallback(&transform, &pipeline, &project)?;
        let model_retention: Bound<'py, PyAny> =
            self.model_retention(&transform, &pipeline, &project)?;
        let inherited_policy: Bound<'py, PyAny> =
            self.inherited_row_error_policy(&pipeline, &project)?;
        let refresh_source: bool =
            self.is_a(&pipeline.getattr("source")?, "PostgresRefreshSourceStep")?;
        self.compile_table(
            transform,
            &pipeline,
            &pipeline_dir,
            lineage_mode,
            relation_name,
            replay_on_change,
            bounded_replay_fallback,
            model_retention,
            &inherited_policy,
            refresh_source,
        )
    }

    /// The model's validated relation name from its exact name, pipeline, then project prefix.
    fn model_relation_name(
        &self,
        model: &Bound<'py, PyAny>,
        pipeline: &Bound<'py, PyAny>,
        project: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let is_table: bool = self.is_a(model, "TransformStep")?;
        let prefix: &str = if is_table {
            "table_prefix"
        } else {
            "view_prefix"
        };
        let logical_name: String = model.getattr("name")?.extract()?;
        let exact_name: Option<String> = model.getattr("relation_name")?.extract()?;
        let pipeline_prefix: Option<String> =
            pipeline.getattr("naming")?.getattr(prefix)?.extract()?;
        let project_prefix: Option<String> = if project.is_none() {
            None
        } else {
            project.getattr("naming")?.getattr(prefix)?.extract()?
        };
        match relations::resolve_model_relation_name(&ModelNaming {
            logical_name: &logical_name,
            kind: if is_table {
                ModelRelationKind::Table
            } else {
                ModelRelationKind::View
            },
            exact_name: exact_name.as_deref(),
            pipeline_prefix: pipeline_prefix.as_deref(),
            project_prefix: project_prefix.as_deref(),
        }) {
            Ok(name) => Ok(name.into_pyobject(self.py)?.into_any()),
            Err(message) => Err(self.compile_error_from_none(message)?),
        }
    }

    /// Pipeline replay settings overlaid with the model's.
    fn execution_settings(
        &self,
        pipeline: &Bound<'py, PyAny>,
        transform: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let replay: Bound<'py, PyDict> = PyDict::new(self.py);
        for owner in [pipeline, transform] {
            let settings: Bound<'py, PyAny> =
                owner.getattr("execution_settings")?.getattr("replay")?;
            if settings.is_truthy()? {
                replay.call_method1("update", (settings,))?;
            }
        }
        let replay: Bound<'py, PyAny> = if replay.is_empty() {
            self.none()
        } else {
            replay.into_any()
        };
        self.build(&self.discovery, "ExecutionSettings", &[("replay", replay)])
    }

    /// The model's, else the pipeline's, else the project's value of one policy, else `None`.
    fn first_set(
        &self,
        transform: &Bound<'py, PyAny>,
        pipeline: &Bound<'py, PyAny>,
        project: &Bound<'py, PyAny>,
        model_field: &str,
        scope_field: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        for value in [
            transform.getattr(model_field)?,
            pipeline.getattr(scope_field)?,
        ] {
            if !value.is_none() {
                return Ok(value);
            }
        }
        if project.is_none() {
            return Ok(self.none());
        }
        project.getattr(scope_field)
    }

    fn bounded_replay_fallback(
        &self,
        transform: &Bound<'py, PyAny>,
        pipeline: &Bound<'py, PyAny>,
        project: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let value: Bound<'py, PyAny> = self.first_set(
            transform,
            pipeline,
            project,
            "bounded_replay_fallback",
            "bounded_replay_fallback",
        )?;
        if value.is_none() {
            return self.member(&self.discovery_types, "BoundedReplayFallback", "FULL");
        }
        self.discovery_enum("BoundedReplayFallback", &value)
    }

    /// Explicit TTL, else model, pipeline, then project retention.
    fn model_retention(
        &self,
        transform: &Bound<'py, PyAny>,
        pipeline: &Bound<'py, PyAny>,
        project: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let resolution =
            |value: Bound<'py, PyAny>, origin: Option<&str>| -> PyResult<Bound<'py, PyAny>> {
                let origin: Bound<'py, PyAny> = match origin {
                    Some(origin) => self.member(&self.compile_types, "RetentionOrigin", origin)?,
                    None => self.none(),
                };
                self.build(
                    &self.models,
                    "ModelRetentionResolution",
                    &[("value", value), ("origin", origin)],
                )
            };
        if !transform.getattr("ttl")?.is_none() {
            return self.build(&self.models, "ModelRetentionResolution", &[]);
        }
        let model_value: Bound<'py, PyAny> = transform.getattr("retention")?;
        if !model_value.is_none() {
            return resolution(model_value, Some("MODEL"));
        }
        let pipeline_value: Bound<'py, PyAny> = pipeline.getattr("model_retention")?;
        if !pipeline_value.is_none() {
            return resolution(pipeline_value, Some("PIPELINE"));
        }
        if !project.is_none() {
            let project_value: Bound<'py, PyAny> = project.getattr("model_retention")?;
            let origin: Option<&str> = (!project_value.is_none()).then_some("PROJECT");
            return resolution(project_value, origin);
        }
        self.build(&self.models, "ModelRetentionResolution", &[])
    }

    /// The pipeline's, else the project's, row-error policy for a model that sets neither.
    fn inherited_row_error_policy(
        &self,
        pipeline: &Bound<'py, PyAny>,
        project: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let pipeline_policy: Bound<'py, PyAny> = pipeline.getattr("model_on_row_error")?;
        if !pipeline_policy.is_none() {
            return self.discovery_enum("RowErrorPolicy", &pipeline_policy);
        }
        if project.is_none() {
            return Ok(self.none());
        }
        let project_policy: Bound<'py, PyAny> = project.getattr("model_on_row_error")?;
        if project_policy.is_none() {
            return Ok(self.none());
        }
        self.discovery_enum("RowErrorPolicy", &project_policy)
    }

    /// Inline SQL, else the SQL file relative to the pipeline directory, stripped.
    fn model_query(
        &self,
        model: &Bound<'py, PyAny>,
        pipeline_dir: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let query: Bound<'py, PyAny> = model.getattr("query")?;
        if !query.is_none() {
            return query.call_method0("strip");
        }
        let sql_file: Bound<'py, PyAny> = model.getattr("sql_file")?;
        if sql_file.is_none() {
            return Err(self.compile_error(format!(
                "Model '{}' must define exactly one of 'query' or 'sql_file'",
                model.getattr("name")?.str()?
            ))?);
        }
        let keywords: Bound<'py, PyDict> = PyDict::new(self.py);
        keywords.set_item("encoding", "utf-8")?;
        pipeline_dir
            .div(sql_file)?
            .call_method0("resolve")?
            .call_method("read_text", (), Some(&keywords))?
            .call_method0("strip")
    }

    /// Analyze one model, translating SQL-analysis failures to compile contract errors.
    fn analyze(
        &self,
        name: &Bound<'py, PyAny>,
        query: &Bound<'py, PyAny>,
        engine: Bound<'py, PyAny>,
        order_by: Bound<'py, PyAny>,
        partition_by: Bound<'py, PyAny>,
        ttl: Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let analyze: Bound<'py, PyAny> = self.analyzer.getattr("analyze")?;
        match self.call(
            &analyze,
            &[
                ("sql", query.clone()),
                ("engine", engine),
                ("order_by", order_by),
                ("partition_by", partition_by),
                ("ttl", ttl),
            ],
        ) {
            Ok(analysis) => Ok(analysis),
            Err(error) => {
                let failure: Bound<'py, PyAny> = error.value(self.py).clone().into_any();
                if !failure.is_instance(&self.sql_errors.getattr("SqlAnalysisError")?)? {
                    return Err(error);
                }
                Err(self.contract_error(name, query, &failure)?)
            }
        }
    }

    /// The compile contract error for one SQL-analysis failure, raised from it.
    fn contract_error(
        &self,
        name: &Bound<'py, PyAny>,
        query: &Bound<'py, PyAny>,
        failure: &Bound<'py, PyAny>,
    ) -> PyResult<PyErr> {
        let is = |class: &str| -> PyResult<bool> {
            failure.is_instance(&self.sql_errors.getattr(class)?)
        };
        let build =
            |class: &str, arguments: Vec<Bound<'py, PyAny>>| -> PyResult<Bound<'py, PyAny>> {
                let mut all: Vec<Bound<'py, PyAny>> = vec![name.clone()];
                all.extend(arguments);
                self.exceptions
                    .getattr(class)?
                    .call1(PyTuple::new(self.py, all)?)
            };
        let attribute = |field: &str| failure.getattr(field);
        let error: Bound<'py, PyAny> = if is("SqlStatementCountError")? {
            build(
                "TransformSqlMultipleStatementsError",
                vec![attribute("statement_count")?],
            )?
        } else if is("SqlQueryShapeError")? && attribute("is_set_operation")?.is_truthy()? {
            build(
                "TransformSqlTopLevelSetOperationError",
                vec![attribute("statement_sql")?],
            )?
        } else if is("SqlQueryShapeError")? {
            build(
                "TransformSqlFinalQueryShapeError",
                vec![attribute("statement_type")?],
            )?
        } else if is("SqlStarProjectionError")? {
            build(
                "TransformSqlStarProjectionError",
                vec![attribute("column_index")?],
            )?
        } else if is("SqlUntypedProjectionError")? {
            build(
                "TransformSqlUntypedProjectionError",
                vec![attribute("column_index")?, attribute("projection_sql")?],
            )?
        } else if is("SqlDuplicateAliasError")? {
            build("TransformSqlDuplicateAliasError", vec![attribute("alias")?])?
        } else if is("SqlStorageReferenceError")? {
            let kind: Bound<'py, PyAny> = attribute("kind")?;
            let kinds: Bound<'py, PyAny> = self
                .py
                .import(SQL_ANALYSIS_TYPES)?
                .getattr("SqlStorageExpressionKind")?;
            let class: &str = if kind.eq(kinds.getattr("ORDER_BY")?)? {
                "TransformOrderByUnknownColumnError"
            } else if kind.eq(kinds.getattr("PARTITION_BY")?)? {
                "TransformPartitionByUnknownColumnError"
            } else {
                "TransformTtlUnknownColumnError"
            };
            build(
                class,
                vec![
                    attribute("expression")?,
                    attribute("unknown_column_names")?,
                    attribute("available_column_names")?,
                ],
            )?
        } else {
            build(
                "TransformSqlParseError",
                vec![query.clone(), failure.str()?.into_any()],
            )?
        };
        error.setattr("span", attribute("span")?)?;
        error.setattr("__context__", failure)?;
        error.setattr("__cause__", failure)?;
        Ok(PyErr::from_value(error))
    }

    /// `(name, relation type, ref type)` of each analyzed reference, coerced as `ParsedRef` does.
    fn references(&self, analysis: &Bound<'py, PyAny>) -> PyResult<References> {
        let relation_types: Bound<'py, PyAny> = self.discovery_types.getattr("SqlRelationType")?;
        let mut references: References = Vec::new();
        for reference in Self::items(&analysis.getattr("references")?)? {
            let ref_type: Bound<'py, PyAny> = reference.getattr("ref_type")?;
            let ref_type: Option<String> = if ref_type.is_none() {
                None
            } else {
                Some(
                    self.discovery_enum("RefType", &ref_type)?
                        .getattr("value")?
                        .extract()?,
                )
            };
            let relation_type: String = relation_types
                .call1((reference.getattr("relation_type")?,))?
                .getattr("value")?
                .extract()?;
            references.push((
                reference.getattr("name")?.extract()?,
                relation_type,
                ref_type,
            ));
        }
        Ok(references)
    }

    fn compile_view(
        &self,
        view: &Bound<'py, PyAny>,
        pipeline: &Bound<'py, PyAny>,
        pipeline_dir: &Bound<'py, PyAny>,
        relation_name: Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let name: Bound<'py, PyAny> = view.getattr("name")?;
        let query: Bound<'py, PyAny> = self.model_query(view, pipeline_dir)?;
        let analysis: Bound<'py, PyAny> = self.analyze(
            &name,
            &query,
            DEFAULT_SQL_MODEL_ENGINE.into_pyobject(self.py)?.into_any(),
            self.tuple(Vec::new())?,
            self.none(),
            self.none(),
        )?;
        let references: References = self.references(&analysis)?;
        if let Some(message) =
            transforms::view_ref_type_error(&name.extract::<String>()?, &borrowed(&references))
        {
            return Err(self.compile_error(message)?);
        }
        self.build(
            &self.models,
            "CompiledViewModel",
            &[
                ("key", self.logical_key("MODEL", name)?),
                ("pipeline_name", pipeline.getattr("name")?),
                ("relation_name", relation_name),
                (
                    "kind",
                    self.member(&self.discovery_types, "ModelKind", "VIEW")?,
                ),
                ("sql_analysis", analysis),
                ("view", view.clone()),
            ],
        )
    }

    fn analyze_transform(
        &self,
        transform: &Bound<'py, PyAny>,
        query: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.analyze(
            &transform.getattr("name")?,
            query,
            transform.getattr("engine")?,
            self.tuple(Self::items(&transform.getattr("order_by")?)?)?,
            transform.getattr("partition_by")?,
            transform.getattr("ttl")?,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn compile_table(
        &self,
        mut transform: Bound<'py, PyAny>,
        pipeline: &Bound<'py, PyAny>,
        pipeline_dir: &Bound<'py, PyAny>,
        lineage_mode: &Bound<'py, PyAny>,
        relation_name: Bound<'py, PyAny>,
        replay_on_change: Bound<'py, PyAny>,
        bounded_replay_fallback: Bound<'py, PyAny>,
        model_retention: Bound<'py, PyAny>,
        inherited_policy: &Bound<'py, PyAny>,
        refresh_source: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let name: Bound<'py, PyAny> = transform.getattr("name")?;
        let model_name: String = name.extract()?;
        let query: Bound<'py, PyAny> = self.model_query(&transform, pipeline_dir)?;
        let mut analysis: Bound<'py, PyAny> = self.analyze_transform(&transform, &query)?;
        if self
            .call(
                &self.analyzer.getattr("uses_prewhere")?,
                &[("analysis", analysis.clone())],
            )?
            .is_truthy()?
        {
            return Err(self.compile_error(format!(
                "Transform '{model_name}' uses PREWHERE, which ClickHouse does not allow in \
                 materialized views; use WHERE instead"
            ))?);
        }
        let mut columns: Vec<(String, String)> = Vec::new();
        for column in Self::items(&analysis.getattr("output_columns")?)? {
            columns.push((
                column.getattr("name")?.extract()?,
                column.getattr("type")?.extract()?,
            ));
        }
        let mut retention_applied: bool = false;
        let policy: Bound<'py, PyAny> = model_retention.getattr("value")?;
        if policy.is_instance(&self.discovery.getattr("ModelRetentionPolicy")?)? {
            let available: HashMap<String, String> = columns.iter().cloned().collect();
            let timestamp_column: String = policy.getattr("timestamp_column")?.extract()?;
            let cap_at_column: Option<String> = policy.getattr("cap_at_column")?.extract()?;
            let skip: bool = policy.getattr("when_missing")?.eq(self.member(
                &self.discovery_types,
                "RetentionMissingBehavior",
                "SKIP",
            )?)?;
            let rendered: Result<Option<String>, String> = retention::render_model_retention(
                &ModelRetention {
                    duration_seconds: policy.getattr("duration_seconds")?.extract()?,
                    timestamp_column: &timestamp_column,
                    cap_at_column: cap_at_column.as_deref(),
                    skip_when_missing: skip,
                },
                &available,
                &model_name,
            );
            let rendered: Option<String> = match rendered {
                Ok(rendered) => rendered,
                Err(message) => return Err(self.compile_error_from_none(message)?),
            };
            if let Some(ttl) = rendered {
                retention_applied = true;
                transform = self.replace(
                    &transform,
                    &[("ttl", ttl.into_pyobject(self.py)?.into_any())],
                )?;
                analysis = self.analyze_transform(&transform, &query)?;
            }
        }
        let references: References = self.references(&analysis)?;
        let column_names: Vec<&str> = columns.iter().map(|(name, _)| name.as_str()).collect();
        let source: String = transform.getattr("source")?.extract()?;
        let mode: String = lineage_mode.extract()?;
        let replay_anchor: String = transform.getattr("replay_anchor")?.extract()?;
        let has_aggregate_semantics: bool = analysis
            .getattr("aggregate_facts")?
            .getattr("has_semantics")?
            .is_truthy()?;
        let contract: Result<transforms::TransformContract, String> =
            transforms::transform_contract(
                &model_name,
                &source,
                &borrowed(&references),
                &column_names,
                &mode,
                &replay_anchor,
                has_aggregate_semantics,
                pipeline.getattr("mode")?.eq("virtual")?,
            );
        let contract: transforms::TransformContract = match contract {
            Ok(contract) => contract,
            Err(message) => return Err(self.compile_error_from_none(message)?),
        };
        let key: Bound<'py, PyAny> = self.logical_key("MODEL", name)?;
        let on_row_error: Bound<'py, PyAny> =
            self.on_row_error(&transform, &analysis, inherited_policy, refresh_source)?;
        self.build(
            &self.models,
            "CompiledTableModel",
            &[
                ("key", key),
                ("pipeline_name", pipeline.getattr("name")?),
                ("relation_name", relation_name),
                (
                    "kind",
                    self.member(&self.discovery_types, "ModelKind", "TABLE")?,
                ),
                ("sql_analysis", analysis),
                ("transform", transform),
                (
                    "preserves_required_lineage",
                    pyo3::types::PyBool::new(self.py, contract.preserves_required_lineage)
                        .to_owned()
                        .into_any(),
                ),
                (
                    "replay_anchor_eligible",
                    pyo3::types::PyBool::new(self.py, contract.replay_anchor_eligible)
                        .to_owned()
                        .into_any(),
                ),
                ("effective_bounded_replay_fallback", bounded_replay_fallback),
                ("retention", model_retention),
                (
                    "retention_applied",
                    pyo3::types::PyBool::new(self.py, retention_applied)
                        .to_owned()
                        .into_any(),
                ),
                ("replay_on_change", replay_on_change),
                ("on_row_error", on_row_error),
            ],
        )
    }

    /// The model, pipeline, then project row-error policy; only a model value may raise.
    fn on_row_error(
        &self,
        transform: &Bound<'py, PyAny>,
        analysis: &Bound<'py, PyAny>,
        inherited_policy: &Bound<'py, PyAny>,
        refresh_source: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let fail: Bound<'py, PyAny> =
            self.member(&self.discovery_types, "RowErrorPolicy", "FAIL")?;
        let quarantine: Bound<'py, PyAny> =
            self.member(&self.discovery_types, "RowErrorPolicy", "QUARANTINE")?;
        let declared: Bound<'py, PyAny> = transform.getattr("on_row_error")?;
        if declared.is(&fail) {
            return Ok(fail);
        }
        if declared.is_none() && !inherited_policy.is(&quarantine) {
            return Ok(fail);
        }
        let mut reason: Option<String> = None;
        if refresh_source {
            reason = Some("it reads a scheduled refresh source".to_owned());
        } else if analysis
            .getattr("aggregate_facts")?
            .getattr("engine_has_aggregate_semantics")?
            .is_truthy()?
            || !self
                .call(
                    &self.analyzer.getattr("is_row_level")?,
                    &[("analysis", analysis.clone())],
                )?
                .is_truthy()?
        {
            reason = Some(
                "its outer query uses GROUP BY, an aggregate-state engine, or a window function"
                    .to_owned(),
            );
        }
        let reason: String = match reason {
            Some(reason) => reason,
            None => {
                let blocked: Bound<'py, PyAny> = self.call(
                    &self.analyzer.getattr("quarantine_ineligibility")?,
                    &[("analysis", analysis.clone())],
                )?;
                if blocked.is_none() {
                    return Ok(quarantine);
                }
                let column: Bound<'py, PyAny> = blocked.get_item(0)?;
                let detail: Bound<'py, PyAny> = blocked.get_item(1)?;
                if column.is_truthy()? {
                    format!(
                        "column '{}' is ineligible: {}",
                        column.str()?,
                        detail.str()?
                    )
                } else {
                    detail.str()?.to_string()
                }
            }
        };
        if declared.is(&quarantine) {
            return Err(self.compile_error(format!(
                "Transform '{}' sets on_row_error = quarantine, but {reason}",
                transform.getattr("name")?.str()?
            ))?);
        }
        Ok(fail)
    }

    /// Attach the model's compilation diagnostic to a contract or compile failure.
    fn with_diagnostic(
        &self,
        error: PyErr,
        model: &Bound<'py, PyAny>,
        loaded_pipeline: &Bound<'py, PyAny>,
    ) -> PyResult<PyErr> {
        let failure: Bound<'py, PyAny> = error.value(self.py).clone().into_any();
        if !(failure.is_instance(&self.exceptions.getattr("TransformSqlContractError")?)?
            || failure.is_instance(&self.exceptions.getattr("PipelineCompileError")?)?)
        {
            return Ok(error);
        }
        if failure.getattr("diagnostic")?.is_none() {
            let diagnostics: Bound<'py, PyModule> = self.py.import(DIAGNOSTICS_MODELS)?;
            let types: Bound<'py, PyModule> = self.py.import(DIAGNOSTICS_TYPES)?;
            let location: Bound<'py, PyAny> =
                self.error_location(&failure, model, &loaded_pipeline.getattr("file_path")?)?;
            let diagnostic: Bound<'py, PyAny> = self.build(
                &diagnostics,
                "CompilerDiagnostic",
                &[
                    (
                        "phase",
                        self.member(&types, "DiagnosticPhase", "COMPILATION")?,
                    ),
                    (
                        "severity",
                        self.member(&types, "DiagnosticSeverity", "ERROR")?,
                    ),
                    ("code", "SQT-COMPILE-001".into_pyobject(self.py)?.into_any()),
                    ("message", failure.str()?.into_any()),
                    ("resource_name", model.getattr("name")?),
                    ("location", location),
                ],
            )?;
            failure.setattr("diagnostic", diagnostic)?;
        }
        Ok(error)
    }

    /// The model's source location, narrowed to the failure's SQL span when it has one.
    fn error_location(
        &self,
        failure: &Bound<'py, PyAny>,
        model: &Bound<'py, PyAny>,
        pipeline_file_path: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let diagnostics: Bound<'py, PyModule> = self.py.import(DIAGNOSTICS_MODELS)?;
        let model_path: Bound<'py, PyAny> = model.getattr("source_file_path")?;
        let path: Bound<'py, PyAny> = if model_path.is_truthy()? {
            model_path
        } else {
            pipeline_file_path.clone()
        };
        let source_line: Bound<'py, PyAny> = model.getattr("source_line")?;
        let source_column: Bound<'py, PyAny> = model.getattr("source_column")?;
        let span: Bound<'py, PyAny> = failure.getattr("span")?;
        if span.is_none() {
            return self.build(
                &diagnostics,
                "SourceLocation",
                &[
                    ("path", path),
                    ("line", source_line),
                    ("column", source_column),
                ],
            );
        }
        let line: i64 = source_line.extract()?;
        let column: i64 = source_column.extract()?;
        let span_line: i64 = span.getattr("line")?.extract()?;
        let span_column: i64 = span.getattr("column")?.extract()?;
        let span_end_line: i64 = span.getattr("end_line")?.extract()?;
        let span_end_column: i64 = span.getattr("end_column")?.extract()?;
        let integer = |value: i64| -> PyResult<Bound<'py, PyAny>> {
            Ok(value.into_pyobject(self.py)?.into_any())
        };
        self.build(
            &diagnostics,
            "SourceLocation",
            &[
                ("path", path),
                ("line", integer(line + span_line - 1)?),
                (
                    "column",
                    integer(if span_line == 1 {
                        column + span_column - 1
                    } else {
                        span_column
                    })?,
                ),
                ("end_line", integer(line + span_end_line - 1)?),
                (
                    "end_column",
                    integer(if span_end_line == 1 {
                        column + span_end_column - 1
                    } else {
                        span_end_column
                    })?,
                ),
            ],
        )
    }
}

impl<'py> Compiler<'py> {
    /// Compile every attached pipeline and assemble one validated compiled project.
    fn assemble_project(
        &self,
        inputs: &Bound<'py, PyAny>,
        reference_rewriter: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let mut pipelines: Vec<Bound<'py, PyAny>> = Vec::new();
        for loaded_pipeline in Self::items(&inputs.getattr("pipelines")?)? {
            pipelines.push(self.compile_pipeline(&loaded_pipeline)?);
        }
        let pipelines: Bound<'py, PyAny> = self.tuple(pipelines)?;
        let sources_by_name: Bound<'py, PyDict> = PyDict::new(self.py);
        for source in Self::items(&inputs.getattr("sources")?)? {
            let lineage_mode: Bound<'py, PyAny> = self.source_lineage_mode(&source)?;
            sources_by_name.set_item(
                source.getattr("name")?,
                self.compiled_source(&source, lineage_mode)?,
            )?;
        }
        let mut models: Vec<Bound<'py, PyAny>> = Vec::new();
        for pipeline in Self::items(&pipelines)? {
            let source: Bound<'py, PyAny> = pipeline.getattr("source")?;
            if !source.is_none() {
                sources_by_name.call_method1(
                    "setdefault",
                    (source.getattr("key")?.getattr("name")?, &source),
                )?;
            }
            models.extend(Self::items(&pipeline.getattr("models")?)?);
        }
        let adapter_profile: Bound<'py, PyAny> = inputs.getattr("adapter_profile")?;
        let dialect: Bound<'py, PyAny> = adapter_profile.getattr("sql_analysis_dialect")?;
        let test_cases: Bound<'py, PyAny> = build_sql_test_cases(
            self.py,
            &inputs.getattr("tests")?,
            &pipelines,
            reference_rewriter,
            &adapter_profile.getattr("render_set_difference_comparison")?,
            &dialect.extract::<String>()?,
        )?
        .into_any();
        let audits: Vec<Bound<'py, PyAny>> = self.resolve_audit_policies(inputs, &pipelines)?;
        let target: Bound<'py, PyAny> = inputs.getattr("effective_target")?;
        let audits: Bound<'py, PyAny> = self.compiled_audits(
            audits,
            &models,
            &target.getattr("default_database")?,
            reference_rewriter,
            &dialect,
        )?;
        let models: Bound<'py, PyAny> = self.tuple(models)?;
        let sources: Bound<'py, PyAny> =
            self.tuple(Self::items(&sources_by_name.values().into_any())?)?;
        let project: Bound<'py, PyAny> = self.build(
            &self.models,
            "CompiledProject",
            &[
                ("sources", sources),
                ("models", models),
                ("pipelines", pipelines),
                ("tests", inputs.getattr("tests")?),
                ("test_cases", test_cases),
                ("audits", audits),
                ("macro_registry", inputs.getattr("macro_registry")?),
                ("macro_context", inputs.getattr("macro_context")?),
                ("project_name", inputs.getattr("project_name")?),
                ("target_name", inputs.getattr("target_name")?),
                ("production_target", target.getattr("production_target")?),
                (
                    "destruction_relation_drop_size_limit",
                    target.getattr("destruction_relation_drop_size_limit")?,
                ),
                (
                    "model_reference_scope",
                    inputs.getattr("model_reference_scope")?,
                ),
                (
                    "allowed_cross_pipeline_references",
                    inputs.getattr("allowed_cross_pipeline_references")?,
                ),
                (
                    "consumer_group_identity",
                    inputs.getattr("consumer_group_identity")?,
                ),
            ],
        )?;
        self.validate_relation_names(&project)?;
        Ok(project)
    }

    /// `(severity, cadence seconds, warmup seconds)` of one audit defaults value.
    fn audit_defaults(defaults: &Bound<'py, PyAny>) -> PyResult<AuditDefaults> {
        Ok(AuditDefaults {
            severity: defaults.getattr("severity")?.extract()?,
            cadence_seconds: defaults.getattr("cadence_seconds")?.extract()?,
            warmup_seconds: defaults.getattr("warmup_seconds")?.extract()?,
        })
    }

    /// Validate every audit's targets, then apply deterministic policy precedence.
    fn resolve_audit_policies(
        &self,
        inputs: &Bound<'py, PyAny>,
        pipelines: &Bound<'py, PyAny>,
    ) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let audits: Vec<Bound<'py, PyAny>> = Self::items(&inputs.getattr("audits")?)?;
        let standalone: Bound<'py, PyAny> = self
            .py
            .import(AUDIT_TYPES)?
            .getattr("AuditAttachmentKind")?
            .getattr("STANDALONE")?;
        let mut policy_inputs: Vec<AuditPolicyInput> = Vec::new();
        for audit in &audits {
            let file_path: Bound<'py, PyAny> = audit.getattr("file_path")?;
            let name: Bound<'py, PyAny> = audit.getattr("name")?;
            let label: Bound<'py, PyAny> = if name.is_truthy()? {
                name.clone()
            } else {
                file_path.getattr("stem")?
            };
            policy_inputs.push(AuditPolicyInput {
                file_path: file_path.str()?.extract()?,
                label: label.extract()?,
                has_name: !name.is_none(),
                is_standalone: audit.getattr("attachment_kind")?.eq(&standalone)?,
                referenced_model_names: Self::items(&audit.getattr("referenced_model_names")?)?
                    .iter()
                    .map(|name| name.extract())
                    .collect::<PyResult<_>>()?,
                severity: audit.getattr("severity")?.extract()?,
                severity_is_explicit: audit.getattr("severity_is_explicit")?.extract()?,
                cadence_seconds_override: audit.getattr("cadence_seconds_override")?.extract()?,
                warmup_seconds_override: audit.getattr("warmup_seconds_override")?.extract()?,
                scheduled_override: audit.getattr("scheduled_override")?.extract()?,
            });
        }
        let mut scopes: Vec<PipelineAuditScope> = Vec::new();
        for pipeline in Self::items(pipelines)? {
            let model_names: Vec<String> = Self::items(&pipeline.getattr("models")?)?
                .iter()
                .map(|model| model.getattr("key")?.getattr("name")?.extract())
                .collect::<PyResult<_>>()?;
            scopes.push(PipelineAuditScope {
                defaults: Self::audit_defaults(
                    &pipeline.getattr("pipeline")?.getattr("audit_defaults")?,
                )?,
                model_names,
            });
        }
        let loaded_project: Bound<'py, PyAny> = inputs
            .getattr("discovered_inputs")?
            .getattr("loaded_project")?;
        let project_defaults: Bound<'py, PyAny> = if loaded_project.is_none() {
            self.build(&self.discovery, "AuditDefaults", &[])?
        } else {
            loaded_project
                .getattr("project")?
                .getattr("audit_defaults")?
        };
        let policies = match audits::resolve_audit_policies(
            &policy_inputs,
            &scopes,
            &Self::audit_defaults(&project_defaults)?,
        ) {
            Ok(policies) => policies,
            Err(message) => return Err(self.error_from_none("AuditCompileError", message)?),
        };
        audits
            .iter()
            .zip(policies)
            .map(|(audit, policy)| {
                self.replace(
                    audit,
                    &[
                        (
                            "severity",
                            policy.severity.into_pyobject(self.py)?.into_any(),
                        ),
                        (
                            "cadence_seconds",
                            policy.cadence_seconds.into_pyobject(self.py)?.into_any(),
                        ),
                        (
                            "warmup_seconds",
                            policy.warmup_seconds.into_pyobject(self.py)?.into_any(),
                        ),
                        (
                            "scheduled",
                            pyo3::types::PyBool::new(self.py, policy.scheduled)
                                .to_owned()
                                .into_any(),
                        ),
                    ],
                )
            })
            .collect()
    }

    /// Each audit with the quality identity of its query resolved against the models.
    fn compiled_audits(
        &self,
        audits: Vec<Bound<'py, PyAny>>,
        models: &[Bound<'py, PyAny>],
        database: &Bound<'py, PyAny>,
        reference_rewriter: &Bound<'py, PyAny>,
        dialect: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let resolver: Bound<'py, PyDict> = PyDict::new(self.py);
        for model in models {
            let relation_name: Bound<'py, PyAny> = model.getattr("relation_name")?;
            let resolved: Bound<'py, PyAny> = if database.is_none() {
                relation_name
            } else {
                format!("{}.{}", database.str()?, relation_name.str()?)
                    .into_pyobject(self.py)?
                    .into_any()
            };
            resolver.set_item(model.getattr("key")?.getattr("name")?, resolved)?;
        }
        let dialect: String = dialect.extract()?;
        let mut compiled: Vec<Bound<'py, PyAny>> = Vec::new();
        for audit in audits {
            let resolved_query: String = replace_refs(
                self.py,
                &audit.getattr("query")?,
                &resolver,
                reference_rewriter,
            )?
            .extract()?;
            let quality_identity: Bound<'py, PyAny> =
                build_audit_quality_identity(self.py, &audit, &resolved_query, &dialect)?;
            compiled.push(self.replace(&audit, &[("quality_identity", quality_identity)])?);
        }
        self.tuple(compiled)
    }

    /// Reject project-wide physical relation collisions before graph or realization work.
    fn validate_relation_names(&self, project: &Bound<'py, PyAny>) -> PyResult<()> {
        let mut sources: Vec<(String, Option<String>)> = Vec::new();
        for source in Self::items(&project.getattr("sources")?)? {
            let authored: Bound<'py, PyAny> = source.getattr("source")?;
            let table_name: Option<String> = if self.is_a(&authored, "ExternalTableSourceStep")? {
                Some(authored.getattr("table_name")?.extract()?)
            } else {
                None
            };
            sources.push((
                source.getattr("key")?.getattr("name")?.extract()?,
                table_name,
            ));
        }
        let table: Bound<'py, PyAny> = self.member(&self.discovery_types, "ModelKind", "TABLE")?;
        let mut models: Vec<(String, String, bool)> = Vec::new();
        for model in Self::items(&project.getattr("models")?)? {
            models.push((
                model.getattr("key")?.getattr("name")?.extract()?,
                model.getattr("relation_name")?.extract()?,
                model.getattr("kind")?.eq(&table)?,
            ));
        }
        let source_claims: Vec<SourceClaim<'_>> = sources
            .iter()
            .map(|(name, table_name)| match table_name {
                Some(table_name) => SourceClaim::Adopted { name, table_name },
                None => SourceClaim::Managed { name },
            })
            .collect();
        let model_claims: Vec<ModelClaim<'_>> = models
            .iter()
            .map(|(name, relation_name, is_table)| ModelClaim {
                name,
                relation_name,
                kind: if *is_table {
                    ModelRelationKind::Table
                } else {
                    ModelRelationKind::View
                },
            })
            .collect();
        relations::validate_project_relation_names(&source_claims, &model_claims)
            .or_else(|message| Err(self.compile_error_from_none(message)?))
    }
}

fn borrowed(references: &References) -> Vec<Reference<'_>> {
    references
        .iter()
        .map(|(name, relation_type, ref_type)| {
            (name.as_str(), relation_type.as_str(), ref_type.as_deref())
        })
        .collect()
}

/// Compile one loaded pipeline into its compiled source and models.
#[pyfunction]
#[pyo3(signature = (*, loaded_pipeline, sql_analyzer))]
fn compile_pipeline<'py>(
    py: Python<'py>,
    loaded_pipeline: &Bound<'py, PyAny>,
    sql_analyzer: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    Compiler::import(py, sql_analyzer)?.compile_pipeline(loaded_pipeline)
}

/// Compile and validate every attached project resource exactly once.
#[pyfunction]
#[pyo3(signature = (*, inputs, reference_rewriter, sql_analyzer))]
fn assemble_project<'py>(
    py: Python<'py>,
    inputs: &Bound<'py, PyAny>,
    reference_rewriter: &Bound<'py, PyAny>,
    sql_analyzer: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    Compiler::import(py, sql_analyzer)?.assemble_project(inputs, reference_rewriter)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(compile_pipeline, module)?)?;
    module.add_function(wrap_pyfunction!(assemble_project, module)?)
}
