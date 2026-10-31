//! Python bindings for deployment planning over desired state.

use std::collections::HashSet;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_planner::desired::{CycleError, DesiredGraph, DesiredObject, KeyInfo, ObjectType};

fn object_type(text: &str) -> PyResult<ObjectType> {
    match text {
        "kafka_table" => Ok(ObjectType::KafkaTable),
        "materialized_view" => Ok(ObjectType::MaterializedView),
        "table" => Ok(ObjectType::Table),
        "view" => Ok(ObjectType::View),
        other => Err(PyValueError::new_err(format!(
            "Unknown desired object type '{other}'"
        ))),
    }
}

fn cycle(error: CycleError) -> PyErr {
    PyValueError::new_err(error.message)
}

/// The desired-state graph over caller-interned key indices.
#[pyclass(name = "DesiredGraph", module = "sqltide._native", frozen)]
struct PyDesiredGraph {
    graph: DesiredGraph,
}

#[pymethods]
impl PyDesiredGraph {
    /// `keys` holds `(object type, name)` per index; `objects` holds
    /// `(key, deps, materialized view source key | None)`.
    #[new]
    fn new(
        keys: Vec<(String, String)>,
        objects: Vec<(usize, Vec<usize>, Option<usize>)>,
        replay_anchors: Vec<usize>,
    ) -> PyResult<Self> {
        let keys: Vec<KeyInfo> = keys
            .into_iter()
            .map(|(kind, name)| {
                Ok(KeyInfo {
                    object_type: object_type(&kind)?,
                    name,
                })
            })
            .collect::<PyResult<_>>()?;
        let count: usize = keys.len();
        let in_range = |index: &usize| *index < count;
        if !objects.iter().all(|(key, deps, source)| {
            in_range(key) && deps.iter().all(in_range) && source.as_ref().is_none_or(in_range)
        }) || !replay_anchors.iter().all(in_range)
        {
            return Err(PyValueError::new_err(
                "Desired graph key index out of range",
            ));
        }
        let objects: Vec<DesiredObject> = objects
            .into_iter()
            .map(|(key, deps, materialized_view_source)| DesiredObject {
                key,
                deps,
                materialized_view_source,
            })
            .collect();
        Ok(Self {
            graph: DesiredGraph::new(keys, objects, &replay_anchors),
        })
    }

    /// `(upstream key, downstream keys)` for every key with a downstream object.
    fn reverse_deps(&self) -> Vec<(usize, Vec<usize>)> {
        self.graph
            .reverse_dep_keys()
            .into_iter()
            .map(|key| (key, self.graph.reverse_deps(key).to_vec()))
            .collect()
    }

    /// Included keys in dependency order. A cycle raises `ValueError`.
    fn order(&self, py: Python<'_>, included: HashSet<usize>) -> PyResult<Vec<usize>> {
        py.detach(|| self.graph.order(&included)).map_err(cycle)
    }

    /// `root` and its downstream objects in dependency order. A cycle raises `ValueError`.
    fn descendants(&self, py: Python<'_>, root: usize) -> PyResult<Vec<usize>> {
        py.detach(|| self.graph.descendants(root)).map_err(cycle)
    }

    /// The nearest upstream replay anchor for `root`. A cycle raises `ValueError`.
    fn nearest_replay_anchor(
        &self,
        root: usize,
        allow_root: bool,
        root_name: &str,
    ) -> PyResult<usize> {
        self.graph
            .nearest_replay_anchor(root, allow_root, root_name)
            .map_err(cycle)
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PyDesiredGraph>()
}
