//! Discovery of authored SQL audits: `audits/**.sql` files with one or more `AUDIT(...)` blocks,
//! reusable generic definitions under `audits/generic/`, and the generic audit instances that
//! `MODEL(...)` headers and their columns declare.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList, PyString, PyTuple};
use sqltide_sqltext::python_text::{python_isspace, python_strip};
use sqltide_sqltext::sql_scan::split_header_blocks;

use crate::analysis::{sql_render_string_literal, sql_validate_query};
use crate::config::{
    audit_header_fields, generic_audit_instances, generic_audit_parameter_names,
    render_generic_audit, unsupported_audit_header_keys,
};
use crate::macros::expand_sql_macros_with;
use crate::model_sql::{call, chained, extract_refs, object};
use crate::project_discovery::parse_sql_header;
use crate::sql_scan::scan_failure;

const AUDIT_MODELS: &str = "sqltide.compiler.audit_discovery.models";
const AUDIT_TYPES: &str = "sqltide.compiler.audit_discovery.types";
const AUDIT_EXCEPTIONS: &str = "sqltide.compiler.audit_discovery.exceptions";
const DISCOVERY_EXCEPTIONS: &str = "sqltide.compiler.discovery.exceptions";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const SQL_ANALYSIS_EXCEPTIONS: &str = "sqltide.compiler.sql_analysis.exceptions";
const AUDIT_KEYWORD: &str = "AUDIT";
const SQL_FILE_PATTERN: &str = "*.sql";
const GENERIC_DIRECTORY: &str = "generic";
const SQL_ANALYSIS_KEY: &str = "sql_analysis";
const LITERAL_FAILURE: &str = "literal";
const DIALECT: &str = "clickhouse";
const MODEL_ARGUMENT: &str = "model";
const COLUMN_ARGUMENT: &str = "column";

/// `SqlAuditParseError(message)`.
fn parse_error(py: Python<'_>, message: String) -> PyErr {
    py.import(AUDIT_EXCEPTIONS)
        .and_then(|module| module.getattr("SqlAuditParseError"))
        .and_then(|class| class.call1((message,)))
        .map_or_else(|error| error, PyErr::from_value)
}

/// `SqlAuditParseError(message)` raised while handling `handled`: `from None` without a cause,
/// or `from handled` with one.
fn parse_error_from(py: Python<'_>, message: String, handled: &PyErr, caused: bool) -> PyErr {
    let error: PyErr = parse_error(py, message);
    let value: Bound<'_, PyAny> = error.value(py).clone().into_any();
    let handled: Bound<'_, PyAny> = handled.value(py).clone().into_any();
    match chained(&value, &handled, caused.then_some(&handled)) {
        Ok(()) => error,
        Err(failure) => failure,
    }
}

fn is_instance(py: Python<'_>, error: &PyErr, module: &str, name: &str) -> PyResult<bool> {
    error
        .value(py)
        .is_instance(&py.import(module)?.getattr(name)?)
}

/// The authored contents of one audit file: the retained snapshot, or the file read as UTF-8.
fn file_contents(
    file_path: &Bound<'_, PyAny>,
    contents_by_path: Option<&Bound<'_, PyAny>>,
) -> PyResult<String> {
    match contents_by_path {
        Some(contents) => contents.get_item(file_path)?.extract(),
        None => {
            let keywords: Bound<'_, PyDict> = PyDict::new(file_path.py());
            keywords.set_item("encoding", "utf-8")?;
            file_path
                .call_method("read_text", (), Some(&keywords))?
                .extract()
        }
    }
}

/// `sorted(root.rglob("*.sql"))`.
fn sorted_sql_files<'py>(root: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    root.py()
        .import("builtins")?
        .getattr("sorted")?
        .call1((root.call_method1("rglob", (SQL_FILE_PATTERN,))?,))?
        .try_iter()?
        .collect()
}

fn exists(path: &Bound<'_, PyAny>) -> PyResult<bool> {
    path.call_method0("exists")?.is_truthy()
}

/// The line-leading `AUDIT(...)` blocks of `contents`.
fn audit_blocks(
    py: Python<'_>,
    file_path: &Bound<'_, PyAny>,
    contents: &[char],
) -> PyResult<Vec<sqltide_sqltext::sql_scan::SqlHeaderBlock>> {
    match split_header_blocks(contents, AUDIT_KEYWORD).map_err(scan_failure) {
        Ok(blocks) => Ok(blocks),
        Err(error) if is_instance(py, &error, SQL_ANALYSIS_EXCEPTIONS, "SqlAnalysisError")? => {
            let message: String = format!(
                "SQL audit '{}' header could not be parsed: {}",
                file_path.str()?,
                error.value(py).str()?
            );
            Err(parse_error_from(py, message, &error, false))
        }
        Err(error) => Err(error),
    }
}

/// The 1-based `(line, column)` of character `index` of `contents`.
fn source_position(contents: &[char], index: usize) -> (usize, usize) {
    let before: &[char] = &contents[..index.min(contents.len())];
    let line: usize = before
        .iter()
        .filter(|character| **character == '\n')
        .count()
        + 1;
    let column: usize = before
        .iter()
        .rposition(|character| *character == '\n')
        .map_or(index + 1, |newline| index - newline);
    (line, column)
}

fn leading_whitespace(text: &str) -> usize {
    text.chars()
        .take_while(|character| python_isspace(*character))
        .count()
}

fn stripped(text: &str) -> String {
    python_strip(&text.chars().collect::<Vec<char>>())
        .iter()
        .collect()
}

/// The parsed settings of an `AUDIT(...)` header, validated against the supported keys.
fn audit_header<'py>(
    py: Python<'py>,
    header: &str,
    file_path: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let header: String = stripped(header);
    if header.is_empty() {
        return Ok(PyDict::new(py));
    }
    let parsed: Bound<'py, PyDict> = match parse_sql_header(py, &header) {
        Ok(parsed) => parsed.cast_into::<PyDict>()?,
        Err(error) if is_instance(py, &error, DISCOVERY_EXCEPTIONS, "ModelHeaderSyntaxError")? => {
            let message: String = format!(
                "AUDIT() header in '{}' uses invalid SQLBuild syntax: {}",
                file_path.str()?,
                error.value(py).str()?
            );
            return Err(parse_error_from(py, message, &error, true));
        }
        Err(error) => return Err(error),
    };
    if let Some(message) =
        unsupported_audit_header_keys(py, &file_path.str()?.to_cow()?, header_items(&parsed)?)?
    {
        return Err(parse_error(py, message));
    }
    Ok(parsed)
}

fn header_items<'py>(header: &Bound<'py, PyDict>) -> PyResult<Vec<(String, Bound<'py, PyAny>)>> {
    header
        .iter()
        .map(|(key, value)| Ok((key.extract()?, value)))
        .collect()
}

/// The audit query with authored macros expanded when a registry and context are available.
fn expanded_query<'py>(
    sql: &str,
    file_path: &Bound<'py, PyAny>,
    macros: Macros<'_, 'py>,
    (line, column): (usize, usize),
) -> PyResult<String> {
    let py: Python<'py> = file_path.py();
    let sql: String = stripped(sql);
    match macros {
        (Some(registry), Some(context)) => expand_sql_macros_with(
            &PyString::new(py, &sql),
            file_path,
            registry,
            context,
            &object(py, line)?,
            &object(py, column)?,
        )?
        .extract(),
        _ => Ok(sql),
    }
}

fn validate_single_query(py: Python<'_>, file_path: &Bound<'_, PyAny>, sql: &str) -> PyResult<()> {
    match sql_validate_query(sql, DIALECT) {
        Ok(()) => Ok(()),
        Err(error) if is_instance(py, &error, SQL_ANALYSIS_EXCEPTIONS, "SqlAnalysisError")? => {
            let message: String = format!(
                "SQL audit '{}' must contain exactly one valid top-level query after AUDIT(): {}",
                file_path.str()?,
                error.value(py).str()?
            );
            Err(parse_error_from(py, message, &error, false))
        }
        Err(error) => Err(error),
    }
}

/// The `__ref(...)` names of `query`, in first-use order; `missing` or `not_ref` explains a
/// query without references or one that uses `__source(...)`.
fn referenced_models<'py>(
    query: &str,
    file_path: &Bound<'py, PyAny>,
    missing: impl FnOnce() -> PyResult<String>,
    not_ref: impl Fn() -> PyResult<String>,
) -> PyResult<Bound<'py, PyTuple>> {
    let py: Python<'py> = file_path.py();
    let refs: Bound<'py, PyList> = extract_refs(py, query, Some(file_path.clone()), 1, 1)?;
    if refs.is_empty() {
        return Err(parse_error(py, missing()?));
    }
    let ref_relation: Bound<'py, PyAny> = py
        .import(DISCOVERY_TYPES)?
        .getattr("SqlRelationType")?
        .getattr("REF")?;
    for parsed in refs.iter() {
        if parsed.getattr("relation_type")?.ne(&ref_relation)? {
            return Err(parse_error(py, not_ref()?));
        }
    }
    let names: Bound<'py, PyAny> = py.import("builtins")?.getattr("dict")?.call_method1(
        "fromkeys",
        (refs
            .iter()
            .map(|parsed| parsed.getattr("name"))
            .collect::<PyResult<Vec<_>>>()?,),
    )?;
    PyTuple::new(py, names.try_iter()?.collect::<PyResult<Vec<_>>>()?)
}

type Macros<'a, 'py> = (Option<&'a Bound<'py, PyAny>>, Option<&'a Bound<'py, PyAny>>);

/// One concrete `AUDIT(...)` block as a `LoadedSqlAudit`.
fn concrete_audit<'py>(
    file_path: &Bound<'py, PyAny>,
    block: &sqltide_sqltext::sql_scan::SqlHeaderBlock,
    audit_index: usize,
    macros: Macros<'_, 'py>,
    position: (usize, usize),
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = file_path.py();
    let path: String = file_path.str()?.to_cow()?.into_owned();
    let header: Bound<'py, PyDict> = audit_header(py, &block.header, file_path)?;
    let query: String = expanded_query(&block.body, file_path, macros, position)?;
    if query.is_empty() {
        return Err(parse_error(
            py,
            format!("SQL audit '{path}' must define a query after AUDIT(...)"),
        ));
    }
    validate_single_query(py, file_path, &query)?;
    let referenced: Bound<'py, PyTuple> = referenced_models(
        &query,
        file_path,
        || {
            Ok(format!(
                "SQL audit '{path}' must reference at least one model with __ref(...)"
            ))
        },
        || {
            Ok(format!(
                "SQL audit '{path}' may only use __ref(...); __source(...) is not allowed"
            ))
        },
    )?;
    let (severity, description, name, severity_is_explicit, cadence, warmup, scheduled) =
        match audit_header_fields(py, &path, header_items(&header)?) {
            Ok(fields) => fields,
            Err(error) if error.is_instance_of::<PyValueError>(py) => {
                let message: String = error.value(py).str()?.to_cow()?.into_owned();
                return Err(parse_error_from(py, message, &error, false));
            }
            Err(error) => return Err(error),
        };
    call(
        &py.import(AUDIT_MODELS)?.getattr("LoadedSqlAudit")?,
        &[
            ("file_path", file_path.clone()),
            ("query", object(py, query)?),
            ("referenced_model_names", referenced.into_any()),
            ("severity", object(py, severity)?),
            ("description", object(py, description)?),
            ("name", object(py, name)?),
            ("audit_index", object(py, audit_index)?),
            ("severity_is_explicit", object(py, severity_is_explicit)?),
            ("cadence_seconds_override", object(py, cadence)?),
            ("warmup_seconds_override", object(py, warmup)?),
            ("scheduled_override", object(py, scheduled)?),
        ],
    )
}

/// Every `AUDIT(...)` block of one authored SQL audit file.
fn audit_file<'py>(
    file_path: &Bound<'py, PyAny>,
    contents_by_path: Option<&Bound<'py, PyAny>>,
    macros: Macros<'_, 'py>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let py: Python<'py> = file_path.py();
    let path: String = file_path.str()?.to_cow()?.into_owned();
    let contents: Vec<char> = file_contents(file_path, contents_by_path)?
        .chars()
        .collect();
    let blocks = audit_blocks(py, file_path, &contents)?;
    let Some(first) = blocks.first() else {
        return Err(parse_error(
            py,
            format!(
                "SQL audit '{path}' must start with an AUDIT(...) header as the first \
                 non-whitespace content"
            ),
        ));
    };
    if !python_strip(&contents[..first.start]).is_empty() {
        return Err(parse_error(
            py,
            format!(
                "SQL audit '{path}' must not contain content before the first AUDIT(...) block"
            ),
        ));
    }
    let mut audits: Vec<Bound<'py, PyAny>> = Vec::new();
    for (offset, block) in blocks.iter().enumerate() {
        let position: (usize, usize) = source_position(
            &contents,
            block.body_start + leading_whitespace(&block.body),
        );
        audits.push(concrete_audit(
            file_path,
            block,
            offset + 1,
            macros,
            position,
        )?);
    }
    if audits.len() > 1 {
        let mut names: Vec<String> = Vec::new();
        for audit in &audits {
            let name: Bound<'py, PyAny> = audit.getattr("name")?;
            if name.is_none() {
                return Err(parse_error(
                    py,
                    format!(
                        "SQL audit '{path}' contains multiple AUDIT(...) blocks; each must define \
                         name"
                    ),
                ));
            }
            names.push(name.extract()?);
        }
        let mut seen: Vec<&str> = Vec::new();
        for name in &names {
            if seen.contains(&name.as_str()) {
                return Err(parse_error(
                    py,
                    format!("SQL audit '{path}' contains duplicate audit name '{name}'"),
                ));
            }
            seen.push(name);
        }
    }
    Ok(audits)
}

/// One generic audit definition from `audits/generic/`.
fn generic_definition<'py>(
    file_path: &Bound<'py, PyAny>,
    contents_by_path: Option<&Bound<'py, PyAny>>,
    macros: Macros<'_, 'py>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = file_path.py();
    let path: String = file_path.str()?.to_cow()?.into_owned();
    let contents: Vec<char> = file_contents(file_path, contents_by_path)?
        .chars()
        .collect();
    let blocks = audit_blocks(py, file_path, &contents)?;
    let [block] = blocks.as_slice() else {
        return Err(parse_error(
            py,
            format!(
                "Generic SQL audit definition '{path}' must contain exactly one AUDIT(...) block"
            ),
        ));
    };
    if !python_strip(&contents[..block.start]).is_empty() {
        return Err(parse_error(
            py,
            format!(
                "Generic SQL audit definition '{path}' must not contain content before the \
                 AUDIT(...) block"
            ),
        ));
    }
    let header: Bound<'py, PyDict> = audit_header(py, &block.header, file_path)?;
    for key in header.keys() {
        if key.extract::<String>()? != SQL_ANALYSIS_KEY {
            return Err(parse_error(
                py,
                format!(
                    "Generic SQL audit definition '{path}' must not define AUDIT() header fields"
                ),
            ));
        }
    }
    if let Some(sql_analysis) = header.get_item(SQL_ANALYSIS_KEY)?
        && !sql_analysis.is_exact_instance_of::<PyBool>()
    {
        return Err(parse_error(
            py,
            format!("AUDIT() in '{path}' sql_analysis must be a boolean"),
        ));
    }
    let position: (usize, usize) = source_position(
        &contents,
        block.body_start + leading_whitespace(&block.body),
    );
    let query: String = expanded_query(&block.body, file_path, macros, position)?;
    if query.is_empty() {
        return Err(parse_error(
            py,
            format!("Generic SQL audit definition '{path}' must define a query after AUDIT(...)"),
        ));
    }
    validate_single_query(py, file_path, &query)?;
    let (raw, quoted) = generic_audit_parameter_names(&query);
    call(
        &py.import(AUDIT_MODELS)?
            .getattr("LoadedGenericSqlAuditDefinition")?,
        &[
            ("file_path", file_path.clone()),
            ("query", object(py, query)?),
            ("raw_parameter_names", PyTuple::new(py, raw)?.into_any()),
            (
                "quoted_parameter_names",
                PyTuple::new(py, quoted)?.into_any(),
            ),
            ("name", file_path.getattr("stem")?),
        ],
    )
}

/// The generic audit definitions under `root`, with unique names.
fn generic_definitions<'py>(
    root: &Bound<'py, PyAny>,
    contents_by_path: Option<&Bound<'py, PyAny>>,
    macros: Macros<'_, 'py>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    if !exists(root)? {
        return Ok(Vec::new());
    }
    let definitions: Vec<Bound<'py, PyAny>> = sorted_sql_files(root)?
        .iter()
        .map(|file_path| generic_definition(file_path, contents_by_path, macros))
        .collect::<PyResult<_>>()?;
    let mut seen: Vec<String> = Vec::new();
    let mut duplicates: Vec<String> = Vec::new();
    for definition in &definitions {
        let name: String = definition.getattr("name")?.extract()?;
        if seen.contains(&name) && !duplicates.contains(&name) {
            duplicates.push(name.clone());
        }
        seen.push(name);
    }
    if !duplicates.is_empty() {
        return Err(parse_error(
            root.py(),
            format!(
                "Duplicate generic SQL audit definitions found: {}",
                duplicates.join(", ")
            ),
        ));
    }
    Ok(definitions)
}

/// The concrete query of one generic audit instance.
fn rendered_generic_query(
    py: Python<'_>,
    definition: &Bound<'_, PyAny>,
    instance: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let arguments: Vec<(String, Bound<'_, PyAny>)> = instance
        .getattr("arguments")?
        .call_method0("items")?
        .try_iter()?
        .map(|item| item?.extract())
        .collect::<PyResult<_>>()?;
    let error: PyErr = match render_generic_audit(
        py,
        &instance.getattr("file_path")?.str()?.to_cow()?,
        &definition.getattr("name")?.extract::<String>()?,
        &definition.getattr("query")?.extract::<String>()?,
        definition.getattr("raw_parameter_names")?.extract()?,
        definition.getattr("quoted_parameter_names")?.extract()?,
        arguments,
    ) {
        Ok(query) => return Ok(query),
        Err(error) if error.is_instance_of::<PyValueError>(py) => error,
        Err(error) => return Err(error),
    };
    let (reason, detail): (String, String) = error.value(py).getattr("args")?.extract()?;
    if reason == LITERAL_FAILURE
        && let Err(failure) = sql_render_string_literal(&detail, DIALECT)
    {
        failure.value(py).setattr("__context__", error.value(py))?;
        return Err(failure);
    }
    Err(parse_error_from(py, detail, &error, false))
}

/// The concrete audits of generic audit instances, numbered per file for distinct identities.
fn render_generic_audits<'py>(
    py: Python<'py>,
    definitions: &[Bound<'py, PyAny>],
    instances: &Bound<'py, PyAny>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let by_name: Bound<'py, PyDict> = PyDict::new(py);
    for definition in definitions {
        by_name.set_item(definition.getattr("name")?, definition)?;
    }
    let audit_class: Bound<'py, PyAny> = py.import(AUDIT_MODELS)?.getattr("LoadedSqlAudit")?;
    let attachment: Bound<'py, PyAny> = py.import(AUDIT_TYPES)?.getattr("AuditAttachmentKind")?;
    let index_by_file: Bound<'py, PyDict> = PyDict::new(py);
    let mut audits: Vec<Bound<'py, PyAny>> = Vec::new();
    for instance in instances.try_iter()? {
        let instance: Bound<'py, PyAny> = instance?;
        let file_path: Bound<'py, PyAny> = instance.getattr("file_path")?;
        let definition_name: Bound<'py, PyAny> = instance.getattr("definition_name")?;
        let Some(definition) = by_name.get_item(&definition_name)? else {
            return Err(parse_error(
                py,
                format!(
                    "Generic SQL audit instance '{}' references unknown definition '{}'",
                    file_path.str()?,
                    definition_name.str()?
                ),
            ));
        };
        let query: String = rendered_generic_query(py, &definition, &instance)?;
        let name: Bound<'py, PyAny> = instance.getattr("name")?;
        let referenced: Bound<'py, PyTuple> = referenced_models(
            &query,
            &file_path,
            || {
                Ok(format!(
                    "Rendered generic SQL audit '{}' must reference at least one model",
                    name.str()?
                ))
            },
            || {
                Ok(format!(
                    "Rendered generic SQL audit '{}' may only use __ref(...)",
                    name.str()?
                ))
            },
        )?;
        let audit_index: usize = index_by_file
            .get_item(&file_path)?
            .map_or(Ok(0), |index| index.extract::<usize>())?
            + 1;
        index_by_file.set_item(&file_path, audit_index)?;
        let arguments: Bound<'py, PyAny> = instance.getattr("arguments")?;
        let column: Option<Bound<'py, PyAny>> = if arguments.contains(COLUMN_ARGUMENT)? {
            Some(arguments.get_item(COLUMN_ARGUMENT)?.str()?.into_any())
        } else {
            None
        };
        let kind: &str = if column.is_some() { "COLUMN" } else { "MODEL" };
        audits.push(call(
            &audit_class,
            &[
                ("file_path", file_path.clone()),
                ("query", object(py, query)?),
                ("referenced_model_names", referenced.into_any()),
                ("severity", instance.getattr("severity")?),
                ("description", instance.getattr("description")?),
                ("name", name.clone()),
                ("audit_index", object(py, audit_index)?),
                ("generic_definition_name", definition_name),
                ("attachment_kind", attachment.getattr(kind)?),
                (
                    "attached_model",
                    arguments.get_item(MODEL_ARGUMENT)?.str()?.into_any(),
                ),
                ("attached_column", object(py, column)?),
                (
                    "severity_is_explicit",
                    instance.getattr("severity_is_explicit")?,
                ),
                (
                    "cadence_seconds_override",
                    instance.getattr("cadence_seconds_override")?,
                ),
                (
                    "warmup_seconds_override",
                    instance.getattr("warmup_seconds_override")?,
                ),
                (
                    "scheduled_override",
                    instance.getattr("scheduled_override")?,
                ),
            ],
        )?);
    }
    Ok(audits)
}

/// Discover and parse the SQL audit files under a project audits root, then render the generic
/// audit instances against the definitions under `root/generic`.
#[pyfunction]
#[pyo3(signature = (*, root, contents_by_path=None, macro_registry=None, macro_context=None, generic_audit_instances=None))]
pub(crate) fn discover_sql_audits<'py>(
    py: Python<'py>,
    root: &Bound<'py, PyAny>,
    contents_by_path: Option<Bound<'py, PyAny>>,
    macro_registry: Option<Bound<'py, PyAny>>,
    macro_context: Option<Bound<'py, PyAny>>,
    generic_audit_instances: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyList>> {
    let instances: Bound<'py, PyAny> =
        generic_audit_instances.unwrap_or_else(|| PyTuple::empty(py).into_any());
    let contents_by_path: Option<&Bound<'py, PyAny>> = contents_by_path
        .as_ref()
        .filter(|contents| !contents.is_none());
    let macros: Macros<'_, 'py> = (
        macro_registry
            .as_ref()
            .filter(|registry| !registry.is_none()),
        macro_context.as_ref().filter(|context| !context.is_none()),
    );
    if !exists(root)? && !instances.is_truthy()? {
        return Ok(PyList::empty(py));
    }
    let generic_root: Bound<'py, PyAny> = root.div(GENERIC_DIRECTORY)?;
    let mut audits: Vec<Bound<'py, PyAny>> = Vec::new();
    if exists(root)? {
        for file_path in sorted_sql_files(root)? {
            if file_path.getattr("parents")?.contains(&generic_root)? {
                continue;
            }
            audits.extend(audit_file(&file_path, contents_by_path, macros)?);
        }
    }
    let definitions: Vec<Bound<'py, PyAny>> =
        generic_definitions(&generic_root, contents_by_path, macros)?;
    audits.extend(render_generic_audits(py, &definitions, &instances)?);
    PyList::new(py, audits)
}

/// The generic audit instances one header entry list declares.
fn header_instances<'py>(
    py: Python<'py>,
    file_path: &Bound<'py, PyAny>,
    raw_audits: &Bound<'py, PyAny>,
    implicit: Vec<(String, Bound<'py, PyAny>)>,
    prefix: &str,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let raw: Bound<'py, PyList> =
        PyList::new(py, raw_audits.try_iter()?.collect::<PyResult<Vec<_>>>()?)?;
    let declared = match generic_audit_instances(
        py,
        &file_path.str()?.to_cow()?,
        raw.as_any(),
        implicit,
        prefix,
    ) {
        Ok(declared) => declared,
        Err(error) if error.is_instance_of::<PyValueError>(py) => {
            let message: String = error.value(py).str()?.to_cow()?.into_owned();
            return Err(parse_error_from(py, message, &error, false));
        }
        Err(error) => return Err(error),
    };
    let class: Bound<'py, PyAny> = py
        .import(AUDIT_MODELS)?
        .getattr("LoadedGenericSqlAuditInstance")?;
    declared
        .into_iter()
        .map(
            |(
                definition_name,
                arguments,
                name,
                severity,
                description,
                severity_is_explicit,
                cadence,
                warmup,
                scheduled,
            )| {
                let arguments_dict: Bound<'py, PyDict> = PyDict::new(py);
                for (key, value) in arguments {
                    arguments_dict.set_item(key, value)?;
                }
                call(
                    &class,
                    &[
                        ("file_path", file_path.clone()),
                        ("definition_name", object(py, definition_name)?),
                        ("arguments", arguments_dict.into_any()),
                        ("name", object(py, name)?),
                        ("severity", object(py, severity)?),
                        ("description", object(py, description)?),
                        ("severity_is_explicit", object(py, severity_is_explicit)?),
                        ("cadence_seconds_override", object(py, cadence)?),
                        ("warmup_seconds_override", object(py, warmup)?),
                        ("scheduled_override", object(py, scheduled)?),
                    ],
                )
            },
        )
        .collect()
}

/// The generic audit instances declared in `MODEL(...)` headers and their columns.
#[pyfunction]
#[pyo3(signature = (*, models))]
pub(crate) fn build_model_audit_instances<'py>(
    py: Python<'py>,
    models: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let mut instances: Vec<Bound<'py, PyAny>> = Vec::new();
    for model in models.try_iter()? {
        let model: Bound<'py, PyAny> = model?;
        let model_name: Bound<'py, PyAny> = model.getattr("name")?;
        let source_file_path: Bound<'py, PyAny> = model.getattr("source_file_path")?;
        let file_path: Bound<'py, PyAny> = if source_file_path.is_none() {
            py.import("pathlib")?
                .getattr("Path")?
                .call1((format!("{}.sql", model_name.str()?),))?
        } else {
            source_file_path
        };
        let name: String = model_name.str()?.to_cow()?.into_owned();
        instances.extend(header_instances(
            py,
            &file_path,
            &model.getattr("audits")?,
            vec![(MODEL_ARGUMENT.to_owned(), model_name.clone())],
            &name,
        )?);
        for column in model.getattr("columns")?.try_iter()? {
            let column: Bound<'py, PyAny> = column?;
            let column_name: Bound<'py, PyAny> = column.getattr("name")?;
            instances.extend(header_instances(
                py,
                &file_path,
                &column.getattr("audits")?,
                vec![
                    (MODEL_ARGUMENT.to_owned(), model_name.clone()),
                    (COLUMN_ARGUMENT.to_owned(), column_name.clone()),
                ],
                &format!("{name}.{}", column_name.str()?),
            )?);
        }
    }
    PyTuple::new(py, instances)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(discover_sql_audits, module)?)?;
    module.add_function(wrap_pyfunction!(build_model_audit_instances, module)?)?;
    Ok(())
}
