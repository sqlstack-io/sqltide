//! SQL-native test execution: run each assembled comparison query and decode its bag-difference
//! rows into per-target missing and unexpected rows.

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyInt, PyList, PyString, PyTuple};

use crate::dev_server::support::attr;
use crate::py_kwargs;

const MODELS: &str = "sqltide.executor.testing.models";
const MISSING_DIFF_TYPE: &str = "missing";
const UNEXPECTED_DIFF_TYPE: &str = "unexpected";
const ASSERTION_RESULT_LABEL_PREFIX: &str = "assert ";
const COMPARISON_ROW_LENGTH: usize = 4;

type Any<'py> = Bound<'py, PyAny>;
type Rows<'py> = Vec<Vec<Any<'py>>>;

fn items<'py>(value: &Any<'py>, field: &str) -> PyResult<Vec<Any<'py>>> {
    value.getattr(field)?.try_iter()?.collect()
}

fn execution_error(py: Python<'_>, message: String) -> PyResult<PyErr> {
    Ok(PyErr::from_value(
        attr(
            py,
            "sqltide.executor.testing.exceptions",
            "SqlTestExecutionError",
        )?
        .call1((message,))?,
    ))
}

fn invalid_row(py: Python<'_>, file_path: &Any<'_>) -> PyResult<PyErr> {
    execution_error(
        py,
        format!(
            "SQL test query for '{}' returned an invalid comparison row",
            file_path.str()?
        ),
    )
}

/// An `int` that is not a `bool`.
fn is_integer(value: &Any<'_>) -> bool {
    value.is_instance_of::<PyInt>() && !value.is_instance_of::<PyBool>()
}

/// Expand grouped multiplicities into per-case `(missing, unexpected)` rows.
fn decode_comparison_rows<'py>(
    rows: &Any<'py>,
    case_count: usize,
    file_path: &Any<'py>,
) -> PyResult<(Rows<'py>, Rows<'py>)> {
    let py: Python<'py> = rows.py();
    let mut missing: Rows<'py> = vec![Vec::new(); case_count];
    let mut unexpected: Rows<'py> = vec![Vec::new(); case_count];
    for raw_row in rows.try_iter()? {
        let raw_row: Any<'py> = raw_row?;
        if raw_row.len()? != COMPARISON_ROW_LENGTH {
            return Err(invalid_row(py, file_path)?);
        }
        let case_index: Any<'py> = raw_row.get_item(0)?;
        let diff_type: Any<'py> = raw_row.get_item(1)?;
        let row_values: Any<'py> = raw_row.get_item(2)?;
        let multiplicity: Any<'py> = raw_row.get_item(3)?;
        if !is_integer(&case_index)
            || !is_integer(&multiplicity)
            || !diff_type.is_instance_of::<PyString>()
            || !(row_values.is_instance_of::<PyList>() || row_values.is_instance_of::<PyTuple>())
            || !(case_index.ge(0)? && case_index.lt(case_count)?)
            || !multiplicity.gt(0)?
        {
            return Err(invalid_row(py, file_path)?);
        }
        let destination: &mut Rows<'py> = if diff_type.eq(MISSING_DIFF_TYPE)? {
            &mut missing
        } else if diff_type.eq(UNEXPECTED_DIFF_TYPE)? {
            &mut unexpected
        } else {
            return Err(execution_error(
                py,
                format!(
                    "SQL test query for '{}' returned unsupported diff type '{}'",
                    file_path.str()?,
                    diff_type.str()?
                ),
            )?);
        };
        let values: Any<'py> =
            PyTuple::new(py, row_values.try_iter()?.collect::<PyResult<Vec<_>>>()?)?.into_any();
        let case_index: usize = case_index.extract()?;
        let occurrences: u64 = multiplicity.extract()?;
        for _ in 0..occurrences {
            destination[case_index].push(values.clone());
        }
    }
    Ok((missing, unexpected))
}

/// Ordered chain-target and assertion results for one comparison query result.
fn target_results<'py>(test_case: &Any<'py>, result: &Any<'py>) -> PyResult<Vec<Any<'py>>> {
    let py: Python<'py> = test_case.py();
    let targets: Vec<Any<'py>> = items(test_case, "target_cases")?;
    let assertions: Vec<Any<'py>> = items(test_case, "assertion_cases")?;
    let mut labels: Vec<Any<'py>> = Vec::new();
    for target in &targets {
        labels.push(target.getattr("target_model_name")?);
    }
    for assertion in &assertions {
        labels.push(
            PyString::new(
                py,
                &format!(
                    "{ASSERTION_RESULT_LABEL_PREFIX}{}",
                    assertion.getattr("name")?.str()?
                ),
            )
            .into_any(),
        );
    }
    let mut column_names: Vec<Any<'py>> = Vec::new();
    for target in &targets {
        column_names.push(target.getattr("expected_column_names")?);
    }
    for assertion in &assertions {
        column_names.push(assertion.getattr("column_names")?);
    }
    let (missing, unexpected) = decode_comparison_rows(
        &result.getattr("rows")?,
        labels.len(),
        &test_case.getattr("file_path")?,
    )?;
    let class: Any<'py> = attr(py, MODELS, "SqlTestTargetExecutionResult")?;
    labels
        .into_iter()
        .enumerate()
        .map(|(index, label)| {
            class.call(
                (),
                Some(&py_kwargs!(py;
                    target_model_name = label,
                    passed = missing[index].is_empty() && unexpected[index].is_empty(),
                    column_names = &column_names[index],
                    missing_rows = PyTuple::new(py, &missing[index])?,
                    unexpected_rows = PyTuple::new(py, &unexpected[index])?,
                )?),
            )
        })
        .collect()
}

/// Execute assembled SQL-native tests against the warehouse.
#[pyfunction]
#[pyo3(signature = (*, test_cases, client))]
fn execute_sql_tests<'py>(
    test_cases: &Any<'py>,
    client: &Any<'py>,
) -> PyResult<Bound<'py, PyTuple>> {
    let py: Python<'py> = client.py();
    if !client
        .getattr("capabilities")?
        .getattr("set_difference_comparison")?
        .is_truthy()?
    {
        return Err(execution_error(
            py,
            "The selected adapter does not support SQL-test set-difference comparison".to_owned(),
        )?);
    }
    let warehouse_error: Any<'py> =
        attr(py, "sqltide.adapter.exceptions", "AdapterWarehouseError")?;
    let result_class: Any<'py> = attr(py, MODELS, "SqlTestExecutionResult")?;
    let mut results: Vec<Any<'py>> = Vec::new();
    for test_case in test_cases.try_iter()? {
        let test_case: Any<'py> = test_case?;
        let query: Any<'py> = test_case.getattr("query")?;
        let common = py_kwargs!(py;
            file_path = test_case.getattr("file_path")?,
            test_index = test_case.getattr("test_index")?,
            executed_sql = &query,
            warnings = test_case.getattr("warnings")?,
            name = test_case.getattr("name")?,
        )?;
        match client.call_method1("query", (&query,)) {
            Err(error) if error.value(py).is_instance(&warehouse_error)? => {
                common.set_item("passed", false)?;
                common.set_item("target_results", PyTuple::empty(py))?;
                common.set_item("error_message", error.value(py).str()?)?;
            }
            Err(error) => return Err(error),
            Ok(result) => {
                let targets: Vec<Any<'py>> = target_results(&test_case, &result)?;
                let mut passed: bool = true;
                for target in &targets {
                    if !target.getattr("passed")?.is_truthy()? {
                        passed = false;
                        break;
                    }
                }
                common.set_item("passed", passed)?;
                common.set_item("target_results", PyTuple::new(py, targets)?)?;
            }
        }
        results.push(result_class.call((), Some(&common))?);
    }
    PyTuple::new(py, results)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(execute_sql_tests, module)?)
}
