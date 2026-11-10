//! View-only row-error policy changes for live direct and virtual models.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use sqltide_sqltext::python_text::python_isalnum;

const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const PLANNER_MODELS: &str = "sqltide.compiler.planner.models";
/// Suffix of the dead-letter view beside each quarantining model view.
const DEAD_LETTERS_VIEW_SUFFIX: &str = "__dead_letters";
/// The projection alias only a quarantining live view defines.
const QUARANTINE_ALIAS: &str = "AS __sqt_value_0";

fn items<'py>(value: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    value.try_iter()?.collect()
}

fn is_word(character: Option<char>) -> bool {
    character.is_some_and(|character| character == '_' || python_isalnum(character))
}

/// Whether `sql` matches Python's `re.search(r"\bAS __sqt_value_0\b", sql)`.
fn quarantines_live(sql: &str) -> bool {
    sql.match_indices(QUARANTINE_ALIAS).any(|(start, matched)| {
        !is_word(sql[..start].chars().next_back())
            && !is_word(sql[start + matched.len()..].chars().next())
    })
}

/// The desired views, Kafka tables and non-dead-letter view names of one realized project.
struct DesiredViews<'py> {
    views: Vec<Bound<'py, PyAny>>,
    kafka_tables: Bound<'py, PyAny>,
    model_view_names: Bound<'py, PyAny>,
}

impl<'py> DesiredViews<'py> {
    fn new(analysis: &Bound<'py, PyAny>) -> PyResult<Self> {
        let py: Python<'py> = analysis.py();
        let models: Bound<'py, PyModule> = py.import(COMPILE_MODELS)?;
        let view_class: Bound<'py, PyAny> = models.getattr("DesiredMaterializedView")?;
        let kafka_class: Bound<'py, PyAny> = models.getattr("DesiredKafkaTable")?;
        let objects: Vec<Bound<'py, PyAny>> = items(
            &analysis
                .getattr("realized_project")?
                .getattr("desired_state")?
                .getattr("objects")?,
        )?;
        let mut views: Vec<Bound<'py, PyAny>> = Vec::new();
        let mut kafka_tables: Vec<Bound<'py, PyAny>> = Vec::new();
        for desired in objects {
            if desired.is_instance(&view_class)? {
                views.push(desired);
            } else if desired.is_instance(&kafka_class)? {
                kafka_tables.push(desired.getattr("name")?);
            }
        }
        let mut model_view_names: Vec<Bound<'py, PyAny>> = Vec::new();
        for view in &views {
            if !view.getattr("dead_letters")?.is_truthy()? {
                model_view_names.push(view.getattr("name")?);
            }
        }
        let frozenset: Bound<'py, PyAny> = py.import("builtins")?.getattr("frozenset")?;
        Ok(Self {
            kafka_tables: frozenset.call1((PyTuple::new(py, kafka_tables)?,))?,
            model_view_names: frozenset.call1((PyTuple::new(py, model_view_names)?,))?,
            views,
        })
    }

    /// The first non-dead-letter view of one model.
    fn model_view(&self, model_name: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
        for candidate in &self.views {
            if candidate.getattr("logical_model_name")?.eq(model_name)?
                && !candidate.getattr("dead_letters")?.is_truthy()?
            {
                return Ok(Some(candidate.clone()));
            }
        }
        Ok(None)
    }

    /// The Kafka landing view feeding `view`, or nothing for any other source.
    fn landing_view_names(&self, view: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let mut current: Bound<'py, PyAny> = view.clone();
        let mut visited: Vec<Bound<'py, PyAny>> = vec![current.getattr("name")?];
        loop {
            let source: Bound<'py, PyAny> = current.getattr("source_table_name")?;
            let mut parent: Option<Bound<'py, PyAny>> = None;
            for candidate in &self.views {
                if !candidate.getattr("target_table_name")?.eq(&source)?
                    || candidate.getattr("dead_letters")?.is_truthy()?
                {
                    continue;
                }
                let name: Bound<'py, PyAny> = candidate.getattr("name")?;
                let mut seen: bool = false;
                for visited_name in &visited {
                    if visited_name.eq(&name)? {
                        seen = true;
                        break;
                    }
                }
                if !seen {
                    parent = Some(candidate.clone());
                    break;
                }
            }
            let Some(parent) = parent else {
                let is_landing_view: bool =
                    !current.is(view) && self.kafka_tables.contains(&source)?;
                return Ok(if is_landing_view {
                    vec![current.getattr("name")?]
                } else {
                    Vec::new()
                });
            };
            visited.push(parent.getattr("name")?);
            current = parent;
        }
    }

    /// Whether a live model's landing view is missing while its Kafka table is live.
    fn landing_repair_pending(
        &self,
        target_name: &Bound<'py, PyAny>,
        landing_views: &[Bound<'py, PyAny>],
        relation_by_name: &Bound<'py, PyDict>,
    ) -> PyResult<bool> {
        if !relation_by_name.contains(target_name)? {
            return Ok(false);
        }
        let source_by_view: Bound<'py, PyDict> = PyDict::new(target_name.py());
        for view in &self.views {
            source_by_view.set_item(view.getattr("name")?, view.getattr("source_table_name")?)?;
        }
        for name in landing_views {
            if !relation_by_name.contains(name)?
                && relation_by_name.contains(source_by_view.as_any().get_item(name)?)?
            {
                return Ok(true);
            }
        }
        Ok(false)
    }
}

/// Whether the live views differ from the policy, repairing a half-applied switch.
fn change_pending(
    target_name: &Bound<'_, PyAny>,
    view_name: &str,
    dead_letter_view_name: &str,
    relation_by_name: &Bound<'_, PyDict>,
    dead_letter_name_owned_by_model: bool,
    quarantines: bool,
) -> PyResult<bool> {
    if !relation_by_name.contains(target_name)? {
        return Ok(false);
    }
    let live_view: Option<Bound<'_, PyAny>> = relation_by_name.get_item(view_name)?;
    let has_dead_letter_view: bool =
        relation_by_name.contains(dead_letter_view_name)? && !dead_letter_name_owned_by_model;
    let live_quarantines: bool = match &live_view {
        None => false,
        Some(view) => {
            let query: Bound<'_, PyAny> = view.getattr("query_sql")?;
            let query: String = if query.is_truthy()? {
                query.extract()?
            } else {
                String::new()
            };
            quarantines_live(&query)
        }
    };
    Ok(if quarantines {
        live_view.is_none() || !live_quarantines || !has_dead_letter_view
    } else {
        has_dead_letter_view || live_quarantines
    })
}

fn relations_by_name<'py>(relations: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let by_name: Bound<'py, PyDict> = PyDict::new(relations.py());
    for relation in items(relations)? {
        by_name.set_item(relation.getattr("name")?, relation)?;
    }
    Ok(by_name)
}

/// One model's live view and dead-letter names: the logical names plus a deployment suffix.
struct Targets<'py> {
    target_name: Bound<'py, PyAny>,
    suffix: String,
}

/// The view-only change for one table model's policy, when one is pending.
fn model_change<'py>(
    model: &Bound<'py, PyAny>,
    view: &Bound<'py, PyAny>,
    targets: Targets<'py>,
    desired: &DesiredViews<'py>,
    relation_by_name: &Bound<'py, PyDict>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py: Python<'py> = model.py();
    let logical_view: String = view.getattr("name")?.extract()?;
    let logical_dead_letters: String = format!("{logical_view}{DEAD_LETTERS_VIEW_SUFFIX}");
    let view_name: String = format!("{logical_view}{}", targets.suffix);
    let dead_letter_view_name: String = format!("{logical_dead_letters}{}", targets.suffix);
    let landing_views: Vec<Bound<'py, PyAny>> = desired.landing_view_names(view)?;
    let desired_policy: Bound<'py, PyAny> = model.getattr("on_row_error")?;
    let quarantine: Bound<'py, PyAny> = py
        .import(DISCOVERY_TYPES)?
        .getattr("RowErrorPolicy")?
        .getattr("QUARANTINE")?;
    let policy_pending: bool = change_pending(
        &targets.target_name,
        &view_name,
        &dead_letter_view_name,
        relation_by_name,
        desired.model_view_names.contains(&logical_dead_letters)?,
        desired_policy.is(&quarantine),
    )?;
    let landing_repair: bool =
        desired.landing_repair_pending(&targets.target_name, &landing_views, relation_by_name)?;
    if !policy_pending && !landing_repair {
        return Ok(None);
    }
    let previous_view_definition: Bound<'py, PyAny> = match relation_by_name.get_item(&view_name)? {
        Some(live_view) => live_view.getattr("definition_sql")?,
        None => py.None().into_bound(py),
    };
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    keywords.set_item("model_key", model.getattr("key")?)?;
    keywords.set_item("view_name", view_name)?;
    keywords.set_item("dead_letter_view_name", dead_letter_view_name)?;
    keywords.set_item("desired_policy", desired_policy)?;
    keywords.set_item("landing_view_names", PyTuple::new(py, landing_views)?)?;
    keywords.set_item("logical_view_name", logical_view)?;
    keywords.set_item("logical_dead_letter_view_name", logical_dead_letters)?;
    keywords.set_item("landing_repair_only", !policy_pending)?;
    keywords.set_item("previous_view_definition", previous_view_definition)?;
    py.import(PLANNER_MODELS)?
        .getattr("DirectRowErrorPolicyChange")?
        .call((), Some(&keywords))
        .map(Some)
}

/// The table models of every pipeline in one mode.
fn table_models<'py>(analysis: &Bound<'py, PyAny>, mode: &str) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let py: Python<'py> = analysis.py();
    let modes: Bound<'py, PyAny> = py.import(DISCOVERY_TYPES)?.getattr("PipelineMode")?;
    let wanted: Bound<'py, PyAny> = modes.getattr(mode)?;
    let table_class: Bound<'py, PyAny> =
        py.import(COMPILE_MODELS)?.getattr("CompiledTableModel")?;
    let mut models: Vec<Bound<'py, PyAny>> = Vec::new();
    for pipeline in items(&analysis.getattr("compiled_project")?.getattr("pipelines")?)? {
        if !modes
            .call1((pipeline.getattr("pipeline")?.getattr("mode")?,))?
            .is(&wanted)
        {
            continue;
        }
        for model in items(&pipeline.getattr("models")?)? {
            if model.is_instance(&table_class)? {
                models.push(model);
            }
        }
    }
    Ok(models)
}

/// View-only changes for live direct models outside the plan's execution scope, reading each
/// live policy from its view definition.
#[pyfunction]
#[pyo3(signature = (*, analysis, plan, relations))]
fn plan_row_error_policy_changes<'py>(
    py: Python<'py>,
    analysis: &Bound<'py, PyAny>,
    plan: &Bound<'py, PyAny>,
    relations: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let relation_by_name: Bound<'py, PyDict> = relations_by_name(relations)?;
    let desired: DesiredViews<'py> = DesiredViews::new(analysis)?;
    let scoped: Bound<'py, PyAny> = py
        .import("builtins")?
        .getattr("frozenset")?
        .call1((plan.getattr("execution_scope")?,))?;
    let mut changes: Vec<Bound<'py, PyAny>> = Vec::new();
    for model in table_models(analysis, "DIRECT")? {
        let key: Bound<'py, PyAny> = model.getattr("key")?;
        if scoped.contains(&key)? {
            continue;
        }
        let Some(view) = desired.model_view(&key.getattr("name")?)? else {
            continue;
        };
        let targets: Targets<'py> = Targets {
            target_name: view.getattr("target_table_name")?,
            suffix: String::new(),
        };
        if let Some(change) = model_change(&model, &view, targets, &desired, &relation_by_name)? {
            changes.push(change);
        }
    }
    PyTuple::new(py, changes)
}

/// View-only changes on live virtual deployments for selected models not otherwise rebuilt.
#[pyfunction]
#[pyo3(signature = (*, analysis, planned_model_names, selected_model_keys, relations))]
fn plan_virtual_row_error_policy_changes<'py>(
    py: Python<'py>,
    analysis: &Bound<'py, PyAny>,
    planned_model_names: &Bound<'py, PyAny>,
    selected_model_keys: &Bound<'py, PyAny>,
    relations: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let relation_by_name: Bound<'py, PyDict> = relations_by_name(relations)?;
    let desired: DesiredViews<'py> = DesiredViews::new(analysis)?;
    let any_selected: bool = selected_model_keys.is_truthy()?;
    let mut changes: Vec<Bound<'py, PyAny>> = Vec::new();
    for model in table_models(analysis, "VIRTUAL")? {
        let key: Bound<'py, PyAny> = model.getattr("key")?;
        let name: Bound<'py, PyAny> = key.getattr("name")?;
        if planned_model_names.contains(&name)?
            || (any_selected && !selected_model_keys.contains(&key)?)
        {
            continue;
        }
        let Some(view) = desired.model_view(&name)? else {
            continue;
        };
        let target_table: String = view.getattr("target_table_name")?.extract()?;
        let Some(binding) = relation_by_name.get_item(&target_table)? else {
            continue;
        };
        let physical_table: Bound<'py, PyAny> = binding.getattr("stable_binding_name")?;
        if physical_table.is_none() {
            continue;
        }
        let physical: String = physical_table.extract()?;
        let suffix: String = physical
            .strip_prefix(&target_table)
            .unwrap_or(&physical)
            .to_owned();
        let targets: Targets<'py> = Targets {
            target_name: physical_table,
            suffix,
        };
        if let Some(change) = model_change(&model, &view, targets, &desired, &relation_by_name)? {
            changes.push(change);
        }
    }
    PyTuple::new(py, changes)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(plan_row_error_policy_changes, module)?)?;
    module.add_function(wrap_pyfunction!(
        plan_virtual_row_error_policy_changes,
        module
    )?)
}

#[cfg(test)]
mod tests {
    use super::quarantines_live;

    #[test]
    fn quarantine_alias_matches_only_at_python_word_boundaries() {
        assert!(quarantines_live("SELECT x AS __sqt_value_0 FROM t"));
        assert!(quarantines_live("x AS __sqt_value_0"));
        assert!(quarantines_live("(x)AS __sqt_value_0,"));
        assert!(!quarantines_live("xAS __sqt_value_0"));
        assert!(!quarantines_live("x AS __sqt_value_01"));
        assert!(!quarantines_live("x AS __sqt_value_0_"));
        assert!(!quarantines_live("éAS __sqt_value_0"));
        assert!(!quarantines_live("x AS __sqt_value_0é"));
        assert!(!quarantines_live("x as __sqt_value_0"));
        assert!(quarantines_live("xAS __sqt_value_0 AS __sqt_value_0"));
    }
}
