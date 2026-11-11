//! Shapes of the compiler's SQL analysis, dependency graph, Rules, quality identity, audit and
//! test discovery and SQL test assembly, plus the diagnostic value enums.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyString, PyTuple};

use super::{Coerce, Given, all_members, base_init, exception, member_set, shapes, str_enum};
use crate::cli::render::render_diagnostic_text;

const ANALYSIS_TYPES: &str = "sqltide.compiler.sql_analysis.types";
const ANALYSIS_EXCEPTIONS: &str = "sqltide.compiler.sql_analysis.exceptions";
const GRAPH_TYPES: &str = "sqltide.compiler.graph.types";
const RULES_EXCEPTIONS: &str = "sqltide.compiler.rules.exceptions";

shapes! {
    module = "sqltide.compiler.sql_analysis.models";

    /// One half-open source span with one-based display coordinates.
    SqlSourceSpan { start, end, line, column, end_line, end_column }

    /// One authored logical relation reference.
    SqlReference {
        name,
        relation_type(coerce = Coerce::Enum("SqlRelationType")),
        span,
        ref_type(default = Given::None),
    }

    /// One exact typed output from the outer model projection list.
    SqlOutputColumn { name, r#type }

    /// One strict outer projection and its authored source span.
    SqlProjection { index, output, span }

    /// One canonical storage expression and its output-column references.
    SqlStorageExpression {
        kind(coerce = Coerce::Enum("SqlStorageExpressionKind")),
        sql,
        referenced_column_names,
    }

    /// ClickHouse query and engine aggregation facts for one model.
    SqlAggregateFacts { has_group_by, function_names, engine_name, engine_has_aggregate_semantics }
    @impl {
        /// `has_group_by or bool(function_names) or engine_has_aggregate_semantics`.
        #[getter]
        fn has_semantics(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            if self.has_group_by.bind(py).is_truthy()? {
                return Ok(self.has_group_by.clone_ref(py));
            }
            if self.function_names.bind(py).is_truthy()? {
                return Ok(true.into_pyobject(py)?.to_owned().into_any().unbind());
            }
            Ok(self.engine_has_aggregate_semantics.clone_ref(py))
        }
    }

    /// Complete immutable SQL facts produced once for one authored model.
    SqlModelAnalysis {
        authored_sql,
        shape(coerce = Coerce::Enum("SqlQueryShape")),
        projections,
        references,
        storage_expressions,
        aggregate_facts,
    }
    @impl {
        #[getter]
        fn output_columns<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
            let mut outputs: Vec<Bound<'py, PyAny>> = Vec::new();
            for projection in self.projections.bind(py).try_iter()? {
                outputs.push(projection?.getattr("output")?);
            }
            PyTuple::new(py, outputs)
        }
    }

    /// Canonical database-neutral SQL and its adapter database template.
    SqlResolvedQuery { canonical_sql, database_template }

    /// One eligible relation identity and its replacement relation SQL.
    SqlRelationRewrite {
        source_name,
        target_relation,
        source_databases(default = Given::None),
        preserve_source_database(default = Given::Bool(false)),
    }

    /// One named query to prepend to a rewritten SELECT.
    SqlNamedQuery { name, query }

    /// Canonical rewritten SELECT SQL and its aggregate classification.
    SqlQueryRewriteResult { query, has_aggregate_semantics }

    /// One physical SQL relation identity.
    SqlRelationIdentity { database, name }

    /// Canonical query and ClickHouse catalog facts from one SQL statement.
    SqlCatalogAnalysis {
        canonical_sql,
        query_sql,
        source_relations,
        direct_source,
        target_relation,
        engine,
        ttl,
        settings,
    }
    @impl {
        /// Return the first physical source when one exists.
        #[getter]
        fn first_source(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            let sources: &Bound<'_, PyAny> = self.source_relations.bind(py);
            if sources.is_truthy()? {
                return Ok(sources.get_item(0)?.unbind());
            }
            Ok(py.None())
        }
    }
}

shapes! {
    module = "sqltide.compiler.graph.models";

    /// One typed dependency from an upstream resource to a dependent resource.
    DependencyEdge { upstream_key, downstream_key, edge_type }

    /// One immutable typed graph over a compiled logical project.
    ProjectGraph {
        project,
        upstream_edges_by_key(coerce = Coerce::ReadOnlyMapping),
        downstream_edges_by_key(coerce = Coerce::ReadOnlyMapping),
        ordered_keys,
    }
}

shapes! {
    module = "sqltide.compiler.rules.models";

    /// A source candidate and remaining diagnostics, or a refusal that preserves its original.
    FormattedSource {
        path,
        original,
        formatted,
        diagnostics(default = Given::EmptyTuple),
        error(default = Given::None),
    }
}

shapes! {
    module = "sqltide.compiler.quality.models";

    /// Stable logical, definition, and executable identity for one quality node.
    QualityNodeIdentity {
        node_kind,
        node_name,
        binding_key,
        definition_fingerprint,
        execution_fingerprint,
    }
}

shapes! {
    module = "sqltide.compiler.audit_discovery.models";

    /// One discovered SQL audit file with validated metadata and refs.
    LoadedSqlAudit {
        file_path,
        query,
        referenced_model_names,
        severity(default = Given::Str("error")),
        description(default = Given::None),
        name(default = Given::None),
        audit_index(default = Given::Int(1)),
        generic_definition_name(default = Given::None),
        attachment_kind(
            default = Given::Member("AuditAttachmentKind", "STANDALONE"),
            coerce = Coerce::Enum("AuditAttachmentKind"),
        ),
        attached_model(default = Given::None),
        attached_column(default = Given::None),
        quality_identity(default = Given::None),
        severity_is_explicit(default = Given::Bool(false)),
        cadence_seconds(default = Given::None),
        warmup_seconds(default = Given::Int(0)),
        scheduled(default = Given::Bool(false)),
        cadence_seconds_override(default = Given::None),
        warmup_seconds_override(default = Given::None),
        scheduled_override(default = Given::None),
    }

    /// One reusable generic SQL audit definition discovered from `audits/generic/`.
    LoadedGenericSqlAuditDefinition {
        file_path,
        query,
        raw_parameter_names,
        quoted_parameter_names,
        name,
    }

    /// One schema-attached generic SQL audit before template rendering.
    LoadedGenericSqlAuditInstance {
        file_path,
        definition_name,
        arguments,
        name,
        severity(default = Given::Str("error")),
        description(default = Given::None),
        severity_is_explicit(default = Given::Bool(false)),
        cadence_seconds_override(default = Given::None),
        warmup_seconds_override(default = Given::None),
        scheduled_override(default = Given::None),
    }
}

shapes! {
    module = "sqltide.compiler.test_discovery.models";

    /// One discovered direct mock boundary in a SQL test file.
    SqlTestMock {
        cte_name,
        name,
        relation_type(coerce = Coerce::Class("sqltide.compiler.discovery.types", "SqlRelationType")),
        query,
    }

    /// One authored CTE preserved from a SQL-native test file.
    SqlTestCte { name, query }

    /// Mocks, expectations, and assertions authored by one model-mode test.
    SqlTestModelPayload { mocks, expected_targets, assertions, assertion_reference_names }

    /// The actual and expected comparison authored by one macro-mode test.
    SqlTestMacroPayload { actual, expected }

    /// One discovered SQL test block with its classified authored CTEs.
    LoadedSqlTest {
        file_path,
        mode,
        authored_ctes,
        payload,
        name(default = Given::None),
        test_index(default = Given::Int(1)),
    }
}

shapes! {
    module = "sqltide.compiler.testing.models";

    /// One recursively assembled actual/expected comparison.
    SqlTestChainStep {
        target_model_name,
        expected_column_names,
        ctes,
        actual_query,
        expected_query,
    }

    /// One zero-row assertion assembled against the resolved test chain.
    SqlTestAssertionStep { name, column_names, ctes, query }

    /// One fully assembled SQL-native test and its canonical adapter statement.
    SqlTestCase {
        file_path,
        query,
        target_cases,
        assertion_cases(default = Given::EmptyTuple),
        warnings(default = Given::EmptyTuple),
        name(default = Given::None),
        test_index(default = Given::Int(1)),
        quality_identity(default = Given::None),
    }
}

/// `SqlAnalysisError.__init__`: the message as `.args`, and the optional source span.
fn analysis_init<'py>(
    instance: &Bound<'py, PyAny>,
    message: Bound<'py, PyAny>,
    span: Option<Bound<'py, PyAny>>,
) -> PyResult<()> {
    base_init(instance, [message])?;
    instance.setattr("span", span)
}

fn text<'py>(py: Python<'py>, message: String) -> Bound<'py, PyAny> {
    PyString::new(py, &message).into_any()
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, message, *, span = None))]
fn sql_analysis_error_init<'py>(
    instance: &Bound<'py, PyAny>,
    message: Bound<'py, PyAny>,
    span: Option<Bound<'py, PyAny>>,
) -> PyResult<()> {
    analysis_init(instance, message, span)
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, *, context, span))]
fn missing_with_clause_init<'py>(
    instance: &Bound<'py, PyAny>,
    context: Bound<'py, PyAny>,
    span: Bound<'py, PyAny>,
) -> PyResult<()> {
    let py: Python<'py> = instance.py();
    let message: String = format!("{} must declare CTEs in a WITH clause", context.str()?);
    analysis_init(instance, text(py, message), Some(span))?;
    instance.setattr("context", context)
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, statement_count))]
fn statement_count_init<'py>(
    instance: &Bound<'py, PyAny>,
    statement_count: Bound<'py, PyAny>,
) -> PyResult<()> {
    let py: Python<'py> = instance.py();
    let message: String = format!(
        "Expected exactly one SQL statement; found {}",
        statement_count.str()?
    );
    analysis_init(instance, text(py, message), None)?;
    instance.setattr("statement_count", statement_count)
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, *, statement_type, statement_sql, is_set_operation))]
fn query_shape_init<'py>(
    instance: &Bound<'py, PyAny>,
    statement_type: Bound<'py, PyAny>,
    statement_sql: Bound<'py, PyAny>,
    is_set_operation: Bound<'py, PyAny>,
) -> PyResult<()> {
    let py: Python<'py> = instance.py();
    let message: String = format!(
        "Unsupported outer SQL statement shape: {}",
        statement_type.str()?
    );
    analysis_init(instance, text(py, message), None)?;
    instance.setattr("statement_type", statement_type)?;
    instance.setattr("statement_sql", statement_sql)?;
    instance.setattr("is_set_operation", is_set_operation)
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, *, column_index, span))]
fn star_projection_init<'py>(
    instance: &Bound<'py, PyAny>,
    column_index: Bound<'py, PyAny>,
    span: Bound<'py, PyAny>,
) -> PyResult<()> {
    let py: Python<'py> = instance.py();
    let message: String = format!(
        "Wildcard output projection at column {}",
        column_index.str()?
    );
    analysis_init(instance, text(py, message), Some(span))?;
    instance.setattr("column_index", column_index)
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, *, column_index, projection_sql, span))]
fn untyped_projection_init<'py>(
    instance: &Bound<'py, PyAny>,
    column_index: Bound<'py, PyAny>,
    projection_sql: Bound<'py, PyAny>,
    span: Bound<'py, PyAny>,
) -> PyResult<()> {
    let py: Python<'py> = instance.py();
    let message: String = format!(
        "Untyped output projection at column {}",
        column_index.str()?
    );
    analysis_init(instance, text(py, message), Some(span))?;
    instance.setattr("column_index", column_index)?;
    instance.setattr("projection_sql", projection_sql)
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, *, alias, span))]
fn duplicate_alias_init<'py>(
    instance: &Bound<'py, PyAny>,
    alias: Bound<'py, PyAny>,
    span: Bound<'py, PyAny>,
) -> PyResult<()> {
    let py: Python<'py> = instance.py();
    let message: String = format!("Duplicate outer projection alias: {}", alias.str()?);
    analysis_init(instance, text(py, message), Some(span))?;
    instance.setattr("alias", alias)
}

#[pyfunction]
#[pyo3(
    name = "__init__",
    signature = (instance, *, kind, expression, unknown_column_names, available_column_names)
)]
fn storage_reference_init<'py>(
    instance: &Bound<'py, PyAny>,
    kind: Bound<'py, PyAny>,
    expression: Bound<'py, PyAny>,
    unknown_column_names: Bound<'py, PyAny>,
    available_column_names: Bound<'py, PyAny>,
) -> PyResult<()> {
    let py: Python<'py> = instance.py();
    let unknown: Bound<'py, PyAny> =
        PyString::new(py, ", ").call_method1("join", (&unknown_column_names,))?;
    let message: String = format!(
        "Unknown model output columns in {}: {}",
        kind.str()?,
        unknown.str()?
    );
    analysis_init(instance, text(py, message), None)?;
    instance.setattr("kind", kind)?;
    instance.setattr("expression", expression)?;
    instance.setattr("unknown_column_names", unknown_column_names)?;
    instance.setattr("available_column_names", available_column_names)
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, message, *, location = None))]
fn graph_input_init<'py>(
    instance: &Bound<'py, PyAny>,
    message: Bound<'py, PyAny>,
    location: Option<Bound<'py, PyAny>>,
) -> PyResult<()> {
    base_init(instance, [message])?;
    instance.setattr("location", location)?;
    instance.setattr("diagnostic", instance.py().None())
}

const DEFAULT_RULES_NOTICE: &str = "\nSQLClean default SQL Rules are enabled. Configure [rules] \
    select = [] to disable configurable Rules, or ignore selected codes in sqltide_project.toml. \
    Mandatory compiler validation still applies.";

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, *, findings, defaults = None))]
fn rules_init<'py>(
    instance: &Bound<'py, PyAny>,
    findings: Bound<'py, PyAny>,
    defaults: Option<Bound<'py, PyAny>>,
) -> PyResult<()> {
    let py: Python<'py> = instance.py();
    instance.setattr("findings", &findings)?;
    let mut rendered: Vec<String> = Vec::new();
    for finding in findings.try_iter()? {
        rendered.push(render_diagnostic_text(
            &finding?.getattr("diagnostic")?,
            None,
        )?);
    }
    let mut message: String = rendered.join("\n\n");
    if let Some(defaults) = defaults
        && defaults.is_truthy()?
    {
        message.push_str(DEFAULT_RULES_NOTICE);
    }
    base_init(instance, [text(py, message)])
}

fn enums(module: &Bound<'_, PyModule>) -> PyResult<()> {
    str_enum(
        module,
        "RefType",
        ANALYSIS_TYPES,
        "RefType",
        None,
        &[("REFERENCE", "reference"), ("MUTABLE", "mutable")],
    )?;
    str_enum(
        module,
        "SqlRelationType",
        ANALYSIS_TYPES,
        "SqlRelationType",
        None,
        &[("SOURCE", "source"), ("REF", "ref")],
    )?;
    str_enum(
        module,
        "SqlQueryShape",
        ANALYSIS_TYPES,
        "SqlQueryShape",
        Some("The supported outer shape of an authored model query."),
        &[("SELECT", "select")],
    )?;
    str_enum(
        module,
        "SqlStorageExpressionKind",
        ANALYSIS_TYPES,
        "SqlStorageExpressionKind",
        Some("One model storage clause analyzed against its output schema."),
        &[
            ("ORDER_BY", "order_by"),
            ("PARTITION_BY", "partition_by"),
            ("TTL", "ttl"),
        ],
    )?;
    str_enum(
        module,
        "DependencyEdgeType",
        GRAPH_TYPES,
        "DependencyEdgeType",
        Some("Semantic relationship between two logical resources."),
        &[
            ("DRIVING_INPUT", "driving_input"),
            ("REFERENCE", "reference"),
            ("MUTABLE_REFERENCE", "mutable_reference"),
        ],
    )?;
    str_enum(
        module,
        "GraphTraversalDirection",
        GRAPH_TYPES,
        "GraphTraversalDirection",
        Some("Direction followed while collecting graph closure."),
        &[("UPSTREAM", "upstream"), ("DOWNSTREAM", "downstream")],
    )?;
    str_enum(
        module,
        "QualityNodeKind",
        "sqltide.compiler.quality.types",
        "QualityNodeKind",
        Some("Kinds of quality definitions persisted by SQLTide."),
        &[("AUDIT", "audit"), ("TEST", "test")],
    )?;
    str_enum(
        module,
        "AuditAttachmentKind",
        "sqltide.compiler.audit_discovery.types",
        "AuditAttachmentKind",
        Some("How an audit is attached to the compiled project."),
        &[
            ("STANDALONE", "standalone"),
            ("MODEL", "model"),
            ("COLUMN", "column"),
        ],
    )?;
    str_enum(
        module,
        "SqlTestMode",
        "sqltide.compiler.test_discovery.types",
        "SqlTestMode",
        Some("Supported authored SQL-native test modes."),
        &[("MODEL", "model"), ("MACRO", "macro")],
    )?;
    str_enum(
        module,
        "DiagnosticPhase",
        "sqltide.diagnostics.types",
        "DiagnosticPhase",
        Some("Compiler or runtime phase that produced a diagnostic."),
        &[
            ("DISCOVERY", "discovery"),
            ("COMPILATION", "compilation"),
            ("GRAPH", "graph"),
            ("REALIZATION", "realization"),
            ("RUNTIME", "runtime"),
        ],
    )?;
    str_enum(
        module,
        "DiagnosticSeverity",
        "sqltide.diagnostics.types",
        "DiagnosticSeverity",
        Some("Severity of a structured diagnostic."),
        &[("ERROR", "error"), ("WARNING", "warning")],
    )
}

fn exceptions(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py: Python<'_> = module.py();
    let value_error: Bound<'_, PyAny> = py.get_type::<PyValueError>().into_any();
    let analysis: Bound<'_, PyAny> = exception(
        module,
        ANALYSIS_EXCEPTIONS,
        "SqlAnalysisError",
        "Raised when SQL cannot be scanned, parsed, rewritten, or generated safely.",
        std::slice::from_ref(&value_error),
        Some(wrap_pyfunction!(sql_analysis_error_init, module)?.into_any()),
    )?;
    let subclasses: [(&str, &str, Bound<'_, PyAny>); 7] = [
        (
            "SqlMissingWithClauseError",
            "Raised when a statement expected to open with a WITH clause does not.",
            wrap_pyfunction!(missing_with_clause_init, module)?.into_any(),
        ),
        (
            "SqlStatementCountError",
            "Raised when authored model SQL does not contain exactly one statement.",
            wrap_pyfunction!(statement_count_init, module)?.into_any(),
        ),
        (
            "SqlQueryShapeError",
            "Raised when the outer statement is not a supported model SELECT.",
            wrap_pyfunction!(query_shape_init, module)?.into_any(),
        ),
        (
            "SqlStarProjectionError",
            "Raised when an outer model projection contains a wildcard.",
            wrap_pyfunction!(star_projection_init, module)?.into_any(),
        ),
        (
            "SqlUntypedProjectionError",
            "Raised when an outer projection lacks an explicit alias and cast.",
            wrap_pyfunction!(untyped_projection_init, module)?.into_any(),
        ),
        (
            "SqlDuplicateAliasError",
            "Raised when outer model projections repeat one output name.",
            wrap_pyfunction!(duplicate_alias_init, module)?.into_any(),
        ),
        (
            "SqlStorageReferenceError",
            "Raised when a storage expression references an unknown output column.",
            wrap_pyfunction!(storage_reference_init, module)?.into_any(),
        ),
    ];
    for (name, doc, init) in subclasses {
        exception(
            module,
            ANALYSIS_EXCEPTIONS,
            name,
            doc,
            std::slice::from_ref(&analysis),
            Some(init),
        )?;
    }
    exception(
        module,
        "sqltide.compiler.graph.exceptions",
        "GraphInputError",
        "Raised when compiled dependencies do not form a valid project graph.",
        std::slice::from_ref(&value_error),
        Some(wrap_pyfunction!(graph_input_init, module)?.into_any()),
    )?;
    exception(
        module,
        RULES_EXCEPTIONS,
        "SqlRulesError",
        "Invalid Rules configuration, SQL adaptation or unsafe formatting candidate.",
        std::slice::from_ref(&value_error),
        None,
    )?;
    exception(
        module,
        RULES_EXCEPTIONS,
        "RulesError",
        "All findings from one compile, with the default-selection migration notice.",
        std::slice::from_ref(&value_error),
        Some(wrap_pyfunction!(rules_init, module)?.into_any()),
    )?;
    let plain: [(&str, &str, &str); 4] = [
        (
            "sqltide.compiler.quality.exceptions",
            "QualityIdentityError",
            "Raised when a quality node lacks required compiled identity.",
        ),
        (
            "sqltide.compiler.audit_discovery.exceptions",
            "SqlAuditParseError",
            "Raised when an authored SQL audit file has an invalid shape.",
        ),
        (
            "sqltide.compiler.test_discovery.exceptions",
            "SqlTestParseError",
            "Raised when an authored SQL test file has an invalid shape.",
        ),
        (
            "sqltide.compiler.testing.exceptions",
            "SqlTestAssemblyError",
            "Raised when a SQL test cannot be assembled against the compiled graph.",
        ),
    ];
    for (public_module, name, doc) in plain {
        exception(
            module,
            public_module,
            name,
            doc,
            std::slice::from_ref(&value_error),
            None,
        )?;
    }
    Ok(())
}

fn constants(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py: Python<'_> = module.py();
    module.add("UPSTREAM_SELECTOR_PREFIX", "+")?;
    module.add("SELECTOR_NAMESPACE_SEPARATOR", ":")?;
    module.add("PIPELINE_SELECTOR_NAMESPACE", "pipeline")?;
    module.add("MODEL_SELECTOR_NAMESPACE", "model")?;
    module.add(
        "ALL_DEPENDENCY_EDGE_TYPES",
        all_members(py, "DependencyEdgeType")?,
    )?;
    module.add(
        "DRIVING_DEPENDENCY_EDGE_TYPES",
        member_set(py, "DependencyEdgeType", &["DRIVING_INPUT"])?,
    )
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    enums(module)?;
    exceptions(module)?;
    constants(module)?;
    module.add_class::<SqlSourceSpan>()?;
    module.add_class::<SqlReference>()?;
    module.add_class::<SqlOutputColumn>()?;
    module.add_class::<SqlProjection>()?;
    module.add_class::<SqlStorageExpression>()?;
    module.add_class::<SqlAggregateFacts>()?;
    module.add_class::<SqlModelAnalysis>()?;
    module.add_class::<SqlResolvedQuery>()?;
    module.add_class::<SqlRelationRewrite>()?;
    module.add_class::<SqlNamedQuery>()?;
    module.add_class::<SqlQueryRewriteResult>()?;
    module.add_class::<SqlRelationIdentity>()?;
    module.add_class::<SqlCatalogAnalysis>()?;
    module.add_class::<DependencyEdge>()?;
    module.add_class::<ProjectGraph>()?;
    module.add_class::<FormattedSource>()?;
    module.add_class::<QualityNodeIdentity>()?;
    module.add_class::<LoadedSqlAudit>()?;
    module.add_class::<LoadedGenericSqlAuditDefinition>()?;
    module.add_class::<LoadedGenericSqlAuditInstance>()?;
    module.add_class::<SqlTestMock>()?;
    module.add_class::<SqlTestCte>()?;
    module.add_class::<SqlTestModelPayload>()?;
    module.add_class::<SqlTestMacroPayload>()?;
    module.add_class::<LoadedSqlTest>()?;
    module.add_class::<SqlTestChainStep>()?;
    module.add_class::<SqlTestAssertionStep>()?;
    module.add_class::<SqlTestCase>()
}
