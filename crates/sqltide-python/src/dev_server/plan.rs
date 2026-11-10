//! The `/api/plan` payload: a mode-aware build preparation serialized for the UI, and the
//! replay row counts it shows per replay root.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use super::support::attr;
use crate::py_kwargs;

const SOURCE_RECREATION_WARNING_CODE: &str = "managed_source_recreated";
const BUILD_MODELS: &str = "sqltide.cli.build.models";

type Any<'py> = Bound<'py, PyAny>;

fn none(py: Python<'_>) -> Any<'_> {
    py.None().into_bound(py)
}

fn get<'py>(value: &Any<'py>, name: &str) -> PyResult<Any<'py>> {
    value.getattr(name)
}

fn name_of<'py>(value: &Any<'py>, key_attr: &str) -> PyResult<Any<'py>> {
    get(&get(value, key_attr)?, "name")
}

fn str_of<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    Ok(value.str()?.into_any())
}

fn list<'py>(py: Python<'py>, items: Vec<Any<'py>>) -> PyResult<Any<'py>> {
    Ok(PyList::new(py, items)?.into_any())
}

fn as_list<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    Ok(PyList::new(value.py(), value.try_iter()?.collect::<PyResult<Vec<_>>>()?)?.into_any())
}

fn map_items<'py>(
    value: &Any<'py>,
    mut each: impl FnMut(&Any<'py>) -> PyResult<Any<'py>>,
) -> PyResult<Vec<Any<'py>>> {
    value.try_iter()?.map(|item| each(&item?)).collect()
}

fn is<'py>(value: &Any<'py>, name: &str) -> PyResult<bool> {
    value.is_instance(&attr(value.py(), BUILD_MODELS, name)?)
}

fn replay_window<'py>(py: Python<'py>, start_time: &Any<'py>) -> PyResult<Any<'py>> {
    if start_time.is_none() {
        return Ok(py_kwargs!(py; mode = "full")?.into_any());
    }
    Ok(py_kwargs!(py; mode = "from", startTime = start_time)?.into_any())
}

fn entry_payload<'py>(entry: &Any<'py>, pipeline_by_model: &Bound<'py, PyDict>) -> PyResult<Any<'py>> {
    let py: Python<'py> = entry.py();
    let model_name = name_of(entry, "model_key")?;
    let driving = get(entry, "driving_input_key")?;
    let sql_change = get(entry, "sql_change")?;
    Ok(py_kwargs!(py;
        modelName = &model_name,
        pipeline = pipeline_by_model.get_item(&model_name)?.unwrap_or_else(|| none(py)),
        reason = str_of(&get(entry, "reason")?)?,
        relationNames = as_list(&get(entry, "relation_names")?)?,
        resourceKinds = list(py, map_items(&get(entry, "resource_kinds")?, str_of)?)?,
        drivingInput = if driving.is_none() { driving } else { get(&driving, "name")? },
        isReplayRoot = get(entry, "is_replay_root")?,
        sqlChange = if sql_change.is_none() {
            none(py)
        } else {
            py_kwargs!(py;
                status = str_of(&get(&sql_change, "status")?)?,
                unifiedDiff = get(&sql_change, "unified_diff")?,
                warning = get(&sql_change, "warning")?,
            )?
            .into_any()
        },
    )?
    .into_any())
}

fn prerequisite_payload<'py>(item: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = item.py();
    let key = get(item, "key")?;
    Ok(py_kwargs!(py;
        name = get(&key, "name")?,
        type = str_of(&get(&key, "resource_type")?)?,
        relationNames = as_list(&get(item, "relation_names")?)?,
        present = get(item, "present")?,
        frameworkManaged = get(item, "framework_managed")?,
    )?
    .into_any())
}

fn operation_payload<'py>(item: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = item.py();
    Ok(py_kwargs!(py;
        relationName = get(item, "relation_name")?,
        action = str_of(&get(item, "action")?)?,
        modelName = name_of(item, "model_key")?,
        resourceKind = str_of(&get(item, "resource_kind")?)?,
    )?
    .into_any())
}

fn replay_root_payload<'py>(item: &Any<'py>, rows_to_replay: Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = item.py();
    let columns = get(item, "driving_input_replay_columns")?;
    Ok(py_kwargs!(py;
        modelName = name_of(item, "model_key")?,
        drivingInputName = name_of(item, "driving_input_key")?,
        drivingInputRelationName = get(item, "driving_input_relation_name")?,
        boundaryMode = str_of(&get(item, "replay_boundary_mode")?)?,
        replayColumns = py_kwargs!(py;
            partition = get(&columns, "partition")?,
            offset = get(&columns, "offset")?,
            timestamp = get(&columns, "timestamp")?,
            landed_at = get(&columns, "landed_at")?,
            cursor = get(&columns, "cursor")?,
        )?,
        propagatedModelNames = list(py, map_items(&get(item, "propagated_model_keys")?, |key| get(key, "name"))?)?,
        hasAggregateSemantics = get(item, "has_aggregate_semantics")?,
        settings = attr(py, "builtins", "dict")?.call1((get(item, "replay_settings")?,))?,
        rowsToReplay = rows_to_replay,
    )?
    .into_any())
}

fn protection_payloads<'py>(plan: &Any<'py>, analysis: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = plan.py();
    let execution_keys = attr(py, "builtins", "frozenset")?.call1((get(plan, "execution_scope")?,))?;
    let mut payloads: Vec<Any<'py>> = Vec::new();
    for pipeline in get(&get(analysis, "compiled_project")?, "pipelines")?.try_iter()? {
        let pipeline = pipeline?;
        let declared = get(&pipeline, "pipeline")?;
        let protection = get(&declared, "protection")?;
        if protection.is_none() {
            continue;
        }
        let mut executes: bool = false;
        for model in get(&pipeline, "models")?.try_iter()? {
            if execution_keys.contains(get(&model?, "key")?)? {
                executes = true;
                break;
            }
        }
        if !executes {
            continue;
        }
        payloads.push(
            py_kwargs!(py;
                pipelineName = get(&declared, "name")?,
                warning = get(&protection, "warning")?,
                confirmation = get(&protection, "confirmation")?,
            )?
            .into_any(),
        );
    }
    list(py, payloads)
}

/// Serialize one `DirectPlan` into the UI plan shape.
fn plan_payload<'py>(
    plan: &Any<'py>,
    analysis: &Any<'py>,
    selectors: &Any<'py>,
    start_time: &Any<'py>,
    planned_at: &Any<'py>,
    replay_row_counts: &Any<'py>,
    command: &Any<'py>,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = plan.py();
    let pipeline_by_model: Bound<'py, PyDict> = PyDict::new(py);
    for model in get(&get(analysis, "compiled_project")?, "models")?.try_iter()? {
        let model = model?;
        pipeline_by_model.set_item(name_of(&model, "key")?, get(&model, "pipeline_name")?)?;
    }
    let roots: Vec<Any<'py>> = map_items(&get(plan, "replay_roots")?, |item| {
        let rows = replay_row_counts.call_method1("get", (name_of(item, "model_key")?,))?;
        replay_root_payload(item, rows)
    })?;
    let warnings: Vec<Any<'py>> = map_items(&get(plan, "warnings")?, |item| {
        Ok(py_kwargs!(py; code = get(item, "warning_code")?, message = get(item, "message")?)?.into_any())
    })?;
    py_kwargs!(py;
        database = get(plan, "database")?,
        userScope = as_list(selectors)?,
        entries = list(py, map_items(&get(plan, "entries")?, |entry| entry_payload(entry, &pipeline_by_model))?)?,
        prerequisites = list(py, map_items(&get(plan, "prerequisite_scope")?, prerequisite_payload)?)?,
        teardown = list(py, map_items(&get(plan, "teardown_operations")?, operation_payload)?)?,
        creation = list(py, map_items(&get(plan, "creation_operations")?, operation_payload)?)?,
        replayRoots = list(py, roots)?,
        warnings = list(py, warnings)?,
        protections = protection_payloads(plan, analysis)?,
        replayWindow = replay_window(py, start_time)?,
        plannedAt = planned_at,
        command = command,
    )
}

fn empty_plan_payload<'py>(
    database: &Any<'py>,
    selectors: &Any<'py>,
    start_time: &Any<'py>,
    planned_at: &Any<'py>,
    command: &Any<'py>,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = database.py();
    py_kwargs!(py;
        database = database,
        userScope = as_list(selectors)?,
        entries = PyList::empty(py),
        prerequisites = PyList::empty(py),
        teardown = PyList::empty(py),
        creation = PyList::empty(py),
        replayRoots = PyList::empty(py),
        warnings = PyList::empty(py),
        sourceRecreations = PyList::empty(py),
        protections = PyList::empty(py),
        replayWindow = replay_window(py, start_time)?,
        plannedAt = planned_at,
        command = command,
    )
}

fn recreation_notice<'py>(recreation: &Any<'py>) -> PyResult<Any<'py>> {
    attr(recreation.py(), "sqltide._native", "render_source_recreation_notice")?.call1((recreation,))
}

fn source_name<'py>(names: &Bound<'py, PyDict>, relation_name: &Any<'py>) -> PyResult<Any<'py>> {
    Ok(names.get_item(relation_name)?.unwrap_or_else(|| relation_name.clone()))
}

fn source_recreation_payload<'py>(
    recreation: &Any<'py>,
    names: &Bound<'py, PyDict>,
    drift_selection_by_source: &Bound<'py, PyDict>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = recreation.py();
    let relation_name = get(&get(&get(recreation, "managed_source")?, "resource")?, "name")?;
    let source = source_name(names, &relation_name)?;
    let selection = drift_selection_by_source.get_item(&source)?;
    let (selected, others): (Any<'py>, Any<'py>) = match selection {
        None => (none(py), PyList::empty(py).into_any()),
        Some(selection) => {
            let pipeline = get(&selection, "pipeline_name")?;
            let mut others: Vec<Any<'py>> = Vec::new();
            for name in get(&selection, "reader_pipeline_names")?.try_iter()? {
                let name = name?;
                if name.ne(&pipeline)? {
                    others.push(name);
                }
            }
            (pipeline, list(py, others)?)
        }
    };
    Ok(py_kwargs!(py;
        sourceName = source,
        relationName = relation_name,
        landingViewName = get(recreation, "landing_view_name")?,
        landingRelationName = get(recreation, "landing_relation_name")?,
        stagedRelationName = get(recreation, "staged_relation_name")?,
        consumerGroup = get(recreation, "consumer_group")?,
        driftedSettings = as_list(&get(recreation, "drifted_setting_names")?)?,
        message = recreation_notice(recreation)?,
        selectedPipeline = selected,
        otherReaderPipelines = others,
    )?
    .into_any())
}

fn direct_phase_payload<'py>(preparation: &Any<'py>, names: &Bound<'py, PyDict>) -> PyResult<Any<'py>> {
    let py: Python<'py> = preparation.py();
    let preview = get(preparation, "preview")?;
    let plan = get(&preview, "plan")?;
    let mut relation_names: Vec<Any<'py>> = Vec::new();
    for entry in get(&plan, "entries")?.try_iter()? {
        relation_names.extend(get(&entry?, "relation_names")?.try_iter()?.collect::<PyResult<Vec<_>>>()?);
    }
    let mut actions: Vec<Any<'py>> = Vec::new();
    for recreation in get(&get(preparation, "workflow")?, "recreated_sources")?.try_iter()? {
        let recreation = recreation?;
        let relation_name = get(&get(&get(&recreation, "managed_source")?, "resource")?, "name")?;
        let source = source_name(names, &relation_name)?;
        let staged = get(&recreation, "staged_relation_name")?;
        for (action, physical) in [("stage", &staged), ("swap", &relation_name), ("retire", &staged)] {
            actions.push(
                py_kwargs!(py;
                    phase = "preparation",
                    action = action,
                    logicalName = &source,
                    physicalName = physical,
                )?
                .into_any(),
            );
        }
    }
    for (phase, operations) in [("teardown", "teardown_operations"), ("creation", "creation_operations")] {
        for operation in get(&plan, operations)?.try_iter()? {
            let operation = operation?;
            actions.push(
                py_kwargs!(py;
                    phase = phase,
                    action = str_of(&get(&operation, "action")?)?,
                    logicalName = name_of(&operation, "model_key")?,
                    physicalName = get(&operation, "relation_name")?,
                )?
                .into_any(),
            );
        }
    }
    Ok(py_kwargs!(py;
        mode = "direct",
        effect = "applied_immediately",
        deploymentId = none(py),
        modelNames = list(py, map_items(&get(&plan, "entries")?, |entry| name_of(entry, "model_key"))?)?,
        contextModelNames = list(py, map_items(&get(&plan, "prerequisite_scope")?, |item| name_of(item, "key"))?)?,
        relationNames = list(py, relation_names)?,
        actions = list(py, actions)?,
        startTime = get(&preview, "effective_start_time")?,
    )?
    .into_any())
}

fn model_name_by_object<'py>(plan: &Any<'py>) -> PyResult<Bound<'py, PyDict>> {
    let names: Bound<'py, PyDict> = PyDict::new(plan.py());
    for prepared in get(plan, "prepared_shadow_objects")?.try_iter()? {
        let prepared = prepared?;
        names.set_item(get(&prepared, "logical_key")?, get(&prepared, "logical_model_name")?)?;
    }
    Ok(names)
}

/// `step.target_key or step.root_key`, and the model name it maps to or the key's own name.
fn affected_name<'py>(item: &Any<'py>, names: &Bound<'py, PyDict>) -> PyResult<Any<'py>> {
    let target = get(item, "target_key")?;
    let affected = if target.is_truthy()? { target } else { get(item, "root_key")? };
    match names.get_item(&affected)? {
        Some(name) => Ok(name),
        None => get(&affected, "name"),
    }
}

fn virtual_phase_payload<'py>(preparation: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = preparation.py();
    let preview = get(preparation, "preview")?;
    let plan = get(&preview, "plan")?;
    let names = model_name_by_object(&plan)?;
    let mut model_names: Vec<Any<'py>> = Vec::new();
    for key in get(&preview, "run_execution_scope")?.try_iter()? {
        let name = get(&key?, "name")?;
        let mut seen: bool = false;
        for existing in &model_names {
            if existing.eq(&name)? {
                seen = true;
                break;
            }
        }
        if !seen {
            model_names.push(name);
        }
    }
    let actions: Vec<Any<'py>> = map_items(&get(&plan, "steps")?, |step| {
        Ok(py_kwargs!(py;
            phase = str_of(&get(step, "phase")?)?,
            action = str_of(&get(step, "action")?)?,
            logicalName = affected_name(step, &names)?,
            physicalName = get(step, "physical_name")?,
        )?
        .into_any())
    })?;
    Ok(py_kwargs!(py;
        mode = "virtual",
        effect = "staged",
        deploymentId = get(&preview, "deployment_id")?,
        modelNames = list(py, model_names)?,
        contextModelNames = list(py, map_items(&get(&preview, "run_context_scope")?, |key| get(key, "name"))?)?,
        relationNames = list(py, map_items(&get(&plan, "prepared_shadow_objects")?, |prepared| get(prepared, "physical_name"))?)?,
        actions = list(py, actions)?,
        startTime = get(&preview, "start_time")?,
    )?
    .into_any())
}

/// The warnings already in `payload`, kept only when they are a list.
fn existing_warnings<'py>(payload: &Bound<'py, PyDict>) -> PyResult<Vec<Any<'py>>> {
    match payload.get_item("warnings")? {
        Some(warnings) if warnings.is_instance_of::<PyList>() => warnings.try_iter()?.collect(),
        _ => Ok(Vec::new()),
    }
}

/// Serialize the exact mode-aware preparation the UI can execute.
#[pyfunction]
#[pyo3(signature = (*, preparation, analysis, selectors, planned_at, replay_row_counts, command))]
pub(crate) fn build_mode_aware_plan_payload<'py>(
    py: Python<'py>,
    preparation: Any<'py>,
    analysis: Any<'py>,
    selectors: Any<'py>,
    planned_at: Any<'py>,
    replay_row_counts: Any<'py>,
    command: Any<'py>,
) -> PyResult<Any<'py>> {
    let mixed: bool = is(&preparation, "MixedWorkflowPreparation")?;
    let is_virtual: bool = is(&preparation, "VirtualWorkflowPreparation")?;
    let direct: Option<Any<'py>> = if mixed {
        Some(get(&preparation, "direct")?)
    } else if is(&preparation, "DirectWorkflowPreparation")? {
        Some(preparation.clone())
    } else {
        None
    };
    let direct = direct.filter(|phase| !phase.is_none());
    let virtual_phase: Option<Any<'py>> = if mixed {
        Some(get(&preparation, "virtual")?)
    } else if is_virtual {
        Some(preparation.clone())
    } else {
        None
    };
    let virtual_phase = virtual_phase.filter(|phase| !phase.is_none());
    let payload: Bound<'py, PyDict> = match (&direct, &virtual_phase) {
        (Some(direct), _) => {
            let preview = get(direct, "preview")?;
            plan_payload(
                &get(&preview, "plan")?,
                &analysis,
                &selectors,
                &get(&preview, "effective_start_time")?,
                &planned_at,
                &replay_row_counts,
                &command,
            )?
        }
        (None, Some(virtual_phase)) => {
            let preview = get(virtual_phase, "preview")?;
            empty_plan_payload(&get(&preview, "database")?, &selectors, &get(&preview, "start_time")?, &planned_at, &command)?
        }
        (None, None) => {
            let error = attr(py, "sqltide.dev_server.exceptions", "DevServerError")?
                .call1(("Cannot serialize a workflow preparation without a direct or virtual phase",))?;
            return Err(PyErr::from_value(error));
        }
    };
    let names: Bound<'py, PyDict> = PyDict::new(py);
    for item in get(&get(&analysis, "realized_project")?, "resources_by_logical_key")?.call_method0("items")?.try_iter()? {
        let (key, resources): (Any<'py>, Any<'py>) = item?.extract()?;
        let key_name = get(&key, "name")?;
        for resource in resources.try_iter()? {
            names.set_item(get(&resource?, "name")?, &key_name)?;
        }
    }
    let drift_selection_by_source: Bound<'py, PyDict> = PyDict::new(py);
    let mut recreations: Vec<Any<'py>> = Vec::new();
    if let Some(direct) = &direct {
        recreations = get(&get(direct, "workflow")?, "recreated_sources")?.try_iter()?.collect::<PyResult<_>>()?;
        for selection in get(&get(&get(direct, "preview")?, "plan")?, "source_drift_selections")?.try_iter()? {
            let selection = selection?;
            drift_selection_by_source.set_item(name_of(&selection, "source_key")?, &selection)?;
        }
    }
    let mut phases: Vec<Any<'py>> = Vec::new();
    if let Some(virtual_phase) = &virtual_phase {
        phases.push(virtual_phase_payload(virtual_phase)?);
    }
    if let Some(direct) = &direct {
        phases.push(direct_phase_payload(direct, &names)?);
    }
    let mode: &str = if mixed { "mixed" } else if is_virtual { "virtual" } else { "direct" };
    let order: Vec<Any<'py>> = phases
        .iter()
        .map(|phase| Ok(phase.get_item("mode")?.str()?.into_any()))
        .collect::<PyResult<_>>()?;
    let protections: Vec<Any<'py>> = map_items(&get(&preparation, "protection_requirements")?, |requirement| {
        Ok(py_kwargs!(py;
            pipelineName = get(requirement, "pipeline_name")?,
            warning = get(requirement, "warning")?,
            confirmation = get(requirement, "confirmation")?,
        )?
        .into_any())
    })?;
    let recreation_payloads: Vec<Any<'py>> = recreations
        .iter()
        .map(|recreation| source_recreation_payload(recreation, &names, &drift_selection_by_source))
        .collect::<PyResult<_>>()?;
    payload.set_item("mode", mode)?;
    payload.set_item("executionOrder", list(py, order)?)?;
    payload.set_item("phases", list(py, phases)?)?;
    payload.set_item(
        "deploymentId",
        match &virtual_phase {
            None => none(py),
            Some(virtual_phase) => get(&get(virtual_phase, "preview")?, "deployment_id")?,
        },
    )?;
    payload.set_item("protections", list(py, protections)?)?;
    payload.set_item("upperBoundary", py_kwargs!(py; mode = "captured_at_execution", continuesLive = true)?)?;
    payload.set_item("sourceRecreations", list(py, recreation_payloads)?)?;
    if direct.is_some() && !recreations.is_empty() {
        let mut warnings: Vec<Any<'py>> = existing_warnings(&payload)?;
        for recreation in &recreations {
            warnings.push(
                py_kwargs!(py;
                    code = SOURCE_RECREATION_WARNING_CODE,
                    message = recreation_notice(recreation)?,
                    relatedModel = none(py),
                )?
                .into_any(),
            );
        }
        payload.set_item("warnings", list(py, warnings)?)?;
    }
    if let Some(virtual_phase) = &virtual_phase {
        let mut warnings: Vec<Any<'py>> = existing_warnings(&payload)?;
        let plan = get(&get(virtual_phase, "preview")?, "plan")?;
        let names = model_name_by_object(&plan)?;
        for warning in get(&plan, "warnings")?.try_iter()? {
            let warning = warning?;
            warnings.push(
                py_kwargs!(py;
                    code = get(&warning, "warning_code")?,
                    message = get(&warning, "message")?,
                    relatedModel = affected_name(&warning, &names)?,
                )?
                .into_any(),
            );
        }
        payload.set_item("warnings", list(py, warnings)?)?;
    }
    Ok(payload.into_any())
}

/// The column the executor compares a forced start time against for this mode.
fn time_column_for<'py>(boundary_mode: &Any<'py>, columns: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = boundary_mode.py();
    let modes = attr(py, "sqltide.compiler.discovery.types", "ReplayLineageMode")?;
    let mode = modes.call1((boundary_mode,))?;
    if mode.is(&get(&modes, "TIMESTAMP")?) || mode.is(&get(&modes, "CURSOR")?) {
        return get(columns, "timestamp");
    }
    if mode.is(&get(&modes, "OFFSETS")?) {
        let landed = get(columns, "landed_at")?;
        return if landed.is_truthy()? { Ok(landed) } else { get(columns, "timestamp") };
    }
    get(columns, "landed_at")
}

/// The column the executor compares a forced start time against for this mode.
#[pyfunction]
#[pyo3(signature = (*, boundary_mode, columns))]
pub(crate) fn replay_time_column<'py>(boundary_mode: Any<'py>, columns: Any<'py>) -> PyResult<Any<'py>> {
    time_column_for(&boundary_mode, &columns)
}

fn escape_time_literal(value: &str) -> String {
    value.replace('\\', "\\\\").replace('\'', "\\'")
}

/// Count the rows a replay of this root would read; unbounded for full replay.
#[pyfunction]
#[pyo3(signature = (*, database, relation_name, time_column, start_time))]
pub(crate) fn build_replay_count_query(
    database: &str,
    relation_name: &str,
    time_column: &str,
    start_time: Option<&str>,
) -> String {
    let base: String = format!("SELECT count() AS rows FROM `{database}`.`{relation_name}`");
    match start_time {
        None => base,
        Some(start_time) => format!(
            "{base} WHERE `{time_column}` >= toDateTime64('{}', 3, 'UTC')",
            escape_time_literal(start_time)
        ),
    }
}

/// Exact rows-to-replay per replay-root model; `None` when the anchor is unreadable, such as a
/// fresh project without the anchor table.
#[pyfunction]
#[pyo3(signature = (*, connection, database, plan, start_time))]
pub(crate) fn count_replay_rows<'py>(
    py: Python<'py>,
    connection: Any<'py>,
    database: Any<'py>,
    plan: Any<'py>,
    start_time: Option<String>,
) -> PyResult<Any<'py>> {
    let counts: Bound<'py, PyDict> = PyDict::new(py);
    let adapter_error = attr(py, "sqltide.adapter.exceptions", "AdapterError")?;
    let database: String = database.str()?.extract()?;
    for root in get(&plan, "replay_roots")?.try_iter()? {
        let root = root?;
        let time_column: String = time_column_for(
            &str_of(&get(&root, "replay_boundary_mode")?)?,
            &get(&root, "driving_input_replay_columns")?,
        )?
        .str()?
        .extract()?;
        let relation_name: String = get(&root, "driving_input_relation_name")?.str()?.extract()?;
        let query: String = build_replay_count_query(&database, &relation_name, &time_column, start_time.as_deref());
        let count: Any<'py> = match connection.call_method1("query", (query,)).and_then(|result| get(&result, "rows")) {
            Err(error) if error.is_instance(py, &adapter_error) => none(py),
            Err(error) => return Err(error),
            Ok(rows) if !rows.is_truthy()? => none(py),
            Ok(rows) => attr(py, "builtins", "int")?.call1((rows.get_item(0)?.get_item(0)?.str()?,))?,
        };
        counts.set_item(name_of(&root, "model_key")?, count)?;
    }
    Ok(counts.into_any())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(build_mode_aware_plan_payload, module)?)?;
    module.add_function(wrap_pyfunction!(count_replay_rows, module)?)?;
    module.add_function(wrap_pyfunction!(replay_time_column, module)?)?;
    module.add_function(wrap_pyfunction!(build_replay_count_query, module)?)
}
