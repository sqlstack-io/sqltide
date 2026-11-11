//! Discover the project's provider classes under `providers/` and validate their settings.

use pyo3::exceptions::{PyException, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple, PyType};

use super::support::{chained_to_handled, text};
use crate::dev_server::support::attr;
use crate::py_kwargs;

const PROVIDERS_DIRECTORY_NAME: &str = "providers";
const PYTHON_INIT_MODULE_STEM: &str = "__init__";
const PROVIDER_EXCEPTIONS: &str = "sqltide.provider.exceptions";
const PROVIDER_NAME_PATTERN: &str = "[a-z_][a-z0-9_]*";

type Any<'py> = Bound<'py, PyAny>;

/// `ProviderDiscoveryError(message)`, raised `from cause` when one is given.
fn discovery_error(py: Python<'_>, message: String, cause: Option<PyErr>) -> PyErr {
    let error: PyErr = match attr(py, PROVIDER_EXCEPTIONS, "ProviderDiscoveryError")
        .and_then(|class| class.call1((message,)))
    {
        Ok(error) => PyErr::from_value(error),
        Err(error) => return error,
    };
    let Some(cause) = cause else {
        return chained_to_handled(py, error);
    };
    let context: Any<'_> = cause.value(py).clone().into_any();
    error.set_cause(py, Some(cause));
    if let Err(failed) = error.value(py).setattr("__context__", context) {
        return failed;
    }
    error
}

/// `path.relative_to(project_dir)`.
fn relative<'py>(path: &Any<'py>, project_dir: &Any<'py>) -> PyResult<Any<'py>> {
    path.call_method1("relative_to", (project_dir,))
}

/// `Path(value).resolve()`.
fn resolved<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    attr(value.py(), "pathlib", "Path")?
        .call1((value,))?
        .call_method0("resolve")
}

/// Removes the modules of the project's root package that another project directory left in
/// `sys.modules`, so a second project imports its own package.
fn evict_stale_project_package_modules(root_module: &str, project_dir: &Any<'_>) -> PyResult<()> {
    let py: Python<'_> = project_dir.py();
    let modules: Any<'_> = py.import("sys")?.getattr("modules")?;
    let root_path: Any<'_> = project_dir.div(root_module)?.call_method0("resolve")?;
    let prefix: String = format!("{root_module}.");
    let items: Bound<'_, PyTuple> = PyTuple::new(
        py,
        modules
            .call_method0("items")?
            .try_iter()?
            .collect::<PyResult<Vec<Any<'_>>>>()?,
    )?;
    for item in items.iter() {
        let module_name: Any<'_> = item.get_item(0)?;
        let module: Any<'_> = item.get_item(1)?;
        if module_name.ne(root_module)?
            && !module_name
                .call_method1("startswith", (prefix.as_str(),))?
                .is_truthy()?
        {
            continue;
        }
        let evict = || modules.call_method1("pop", (&module_name, py.None()));
        if let Some(module_file) = module.getattr_opt("__file__")?
            && module_file.is_instance_of::<pyo3::types::PyString>()
        {
            let inside = || -> PyResult<Any<'_>> {
                resolved(&module_file)?
                    .call_method1("relative_to", (project_dir.call_method0("resolve")?,))
            };
            match inside() {
                Ok(_) => {}
                Err(error) if error.is_instance_of::<PyValueError>(py) => {
                    evict()?;
                }
                Err(error) => return Err(error),
            }
            continue;
        }
        let Some(module_paths) = module
            .getattr_opt("__path__")?
            .filter(|paths| !paths.is_none())
        else {
            evict()?;
            continue;
        };
        let mut in_root: bool = false;
        for path in module_paths.try_iter()? {
            if resolved(&path?)?.eq(&root_path)? {
                in_root = true;
                break;
            }
        }
        if !in_root {
            evict()?;
        }
    }
    Ok(())
}

/// Imports one provider file as `<package>.<module>` with the project directory on `sys.path`,
/// reusing the module already loaded from the same file.
fn load_provider_module<'py>(file_path: &Any<'py>, project_dir: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = file_path.py();
    let parts: Any<'py> = relative(file_path, project_dir)?
        .call_method1("with_suffix", ("",))?
        .getattr("parts")?;
    let module_name: String = "."
        .into_pyobject(py)?
        .call_method1("join", (parts,))?
        .extract()?;
    let root_module: &str = module_name.split('.').next().unwrap_or_default();
    evict_stale_project_package_modules(root_module, project_dir)?;
    let sys: Bound<'py, PyModule> = py.import("sys")?;
    let modules: Any<'py> = sys.getattr("modules")?;
    let existing: Any<'py> = modules.call_method1("get", (&module_name,))?;
    if !existing.is_none() {
        if let Some(existing_file) = existing.getattr_opt("__file__")?
            && existing_file.is_instance_of::<pyo3::types::PyString>()
            && resolved(&existing_file)?.eq(file_path.call_method0("resolve")?)?
        {
            return Ok(existing);
        }
        modules.call_method1("pop", (&module_name, py.None()))?;
    }
    let util: Bound<'py, PyModule> = py.import("importlib.util")?;
    let spec: Any<'py> = util.call_method1("spec_from_file_location", (&module_name, file_path))?;
    if spec.is_none() || spec.getattr("loader")?.is_none() {
        return Err(discovery_error(
            py,
            format!("Could not load provider file {}", text(file_path)?),
            None,
        ));
    }
    let module: Any<'py> = util.call_method1("module_from_spec", (&spec,))?;
    let old_path: Bound<'py, PyList> = PyList::new(
        py,
        sys.getattr("path")?
            .try_iter()?
            .collect::<PyResult<Vec<Any<'py>>>>()?,
    )?;
    sys.getattr("path")?
        .call_method1("insert", (0, project_dir.str()?))?;
    let loaded: PyResult<()> = (|| {
        modules.set_item(&module_name, &module)?;
        spec.getattr("loader")?
            .call_method1("exec_module", (&module,))?;
        Ok(())
    })();
    let restored: PyResult<()> = sys.setattr("path", old_path);
    let loaded: PyResult<()> = match loaded {
        Err(error) if error.is_instance_of::<PyException>(py) => {
            let message: PyResult<String> = (|| {
                Ok(format!(
                    "Failed to import provider file {}: {}",
                    text(&relative(file_path, project_dir)?)?,
                    text(error.value(py).as_any())?
                ))
            })();
            match message {
                Ok(message) => Err(discovery_error(py, message, Some(error))),
                Err(failed) => {
                    // As in an `except` block, the import error is the context.
                    failed.value(py).setattr("__context__", error.value(py))?;
                    Err(failed)
                }
            }
        }
        outcome => outcome,
    };
    loaded?;
    restored?;
    Ok(module)
}

/// `provider_class.name()`, reporting an invalid name with the class and its file.
fn discovered_name<'py>(
    provider_class: &Any<'py>,
    file_path: &Any<'py>,
    project_dir: &Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = provider_class.py();
    match provider_class.call_method0("name") {
        Err(error)
            if error.is_instance(py, &attr(py, PROVIDER_EXCEPTIONS, "ProviderInputError")?) =>
        {
            let message: String = format!(
                "Provider class {} in {} has an invalid provider name: {}",
                text(&provider_class.getattr("__name__")?)?,
                text(&relative(file_path, project_dir)?)?,
                text(error.value(py).as_any())?
            );
            Err(discovery_error(py, message, Some(error)))
        }
        outcome => outcome,
    }
}

/// Each pydantic validation error as `location: message [type]`, one per line.
fn format_provider_validation_error(error: &Any<'_>) -> PyResult<String> {
    let py: Python<'_> = error.py();
    let mut details: Vec<String> = Vec::new();
    for item in error
        .call_method("errors", (), Some(&py_kwargs!(py; include_input = false)?))?
        .try_iter()?
    {
        let item: Any<'_> = item?;
        let location: Any<'_> = item.call_method1("get", ("loc", PyTuple::empty(py)))?;
        let location_text: String = if location.is_truthy()? {
            location
                .try_iter()?
                .map(|part| text(&part?))
                .collect::<PyResult<Vec<String>>>()?
                .join(".")
        } else {
            "<root>".to_owned()
        };
        let message: Any<'_> = item.call_method1("get", ("msg", "invalid value"))?;
        let error_type: Any<'_> = item.call_method1("get", ("type", "validation_error"))?;
        details.push(format!(
            "{location_text}: {} [{}]",
            text(&message)?,
            text(&error_type)?
        ));
    }
    if details.is_empty() {
        return Ok("invalid provider settings".to_owned());
    }
    Ok(details.join("\n"))
}

/// `provider_class()`, reporting invalid settings with the provider name and file.
fn provider_instance<'py>(
    provider_class: &Any<'py>,
    provider_name: &Any<'py>,
    file_path: &Any<'py>,
    project_dir: &Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = provider_class.py();
    match provider_class.call0() {
        Err(error) if error.is_instance(py, &attr(py, "pydantic", "ValidationError")?) => {
            let message: String = format!(
                "Provider '{}' in {} has invalid settings:\n{}",
                text(provider_name)?,
                text(&relative(file_path, project_dir)?)?,
                format_provider_validation_error(error.value(py).as_any())?
            );
            Err(discovery_error(py, message, Some(error)))
        }
        outcome => outcome,
    }
}

/// The concrete `Provider` subclasses each public `providers/` module defines, in file then class
/// name order, as `DiscoveredProvider` records with validated settings.
pub(super) fn discover_provider_classes<'py>(project_dir: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = project_dir.py();
    let providers_root: Any<'py> = project_dir.div(PROVIDERS_DIRECTORY_NAME)?;
    if !providers_root.call_method0("is_dir")?.is_truthy()? {
        return Ok(PyTuple::empty(py).into_any());
    }
    let provider: Any<'py> = attr(py, "sqltide.providers", "Provider")?;
    let provider_type: &Bound<'py, PyType> = provider.cast()?;
    let discovered_class: Any<'py> = attr(py, "sqltide.provider.models", "DiscoveredProvider")?;
    let is_abstract: Any<'py> = attr(py, "inspect", "isabstract")?;
    let seen_names: Bound<'py, PyDict> = PyDict::new(py);
    let mut discovered: Vec<Any<'py>> = Vec::new();
    let sorted: Any<'py> = py
        .import("builtins")?
        .getattr("sorted")?
        .call1((providers_root.call_method1("rglob", ("*.py",))?,))?;
    for file_path in sorted.try_iter()? {
        let file_path: Any<'py> = file_path?;
        if file_path.getattr("stem")?.eq(PYTHON_INIT_MODULE_STEM)?
            || file_path
                .getattr("name")?
                .call_method1("startswith", ("_",))?
                .is_truthy()?
        {
            continue;
        }
        let module: Any<'py> = load_provider_module(&file_path, project_dir)?;
        let module_name: Any<'py> = module.getattr("__name__")?;
        let members: Any<'py> =
            attr(py, "inspect", "getmembers")?.call1((&module, attr(py, "inspect", "isclass")?))?;
        for member in members.try_iter()? {
            let value: Any<'py> = member?.get_item(1)?;
            if value.getattr("__module__")?.ne(&module_name)? {
                continue;
            }
            if value.is(&provider)
                || !value.cast::<PyType>()?.is_subclass(provider_type)?
                || is_abstract.call1((&value,))?.is_truthy()?
            {
                continue;
            }
            let name: Any<'py> = discovered_name(&value, &file_path, project_dir)?;
            if let Some(existing_path) = seen_names.get_item(&name)? {
                return Err(discovery_error(
                    py,
                    format!(
                        "Duplicate provider name '{}' found in {} and {}",
                        text(&name)?,
                        text(&relative(&existing_path, project_dir)?)?,
                        text(&relative(&file_path, project_dir)?)?
                    ),
                    None,
                ));
            }
            seen_names.set_item(&name, &file_path)?;
            let relative_path: Any<'py> = relative(&file_path, project_dir)?;
            let settings: Any<'py> = provider_instance(&value, &name, &file_path, project_dir)?;
            discovered.push(discovered_class.call(
                (),
                Some(&py_kwargs!(py;
                    file_path = &file_path,
                    relative_path = relative_path,
                    name = name,
                    provider_class = &value,
                    settings = settings,
                )?),
            )?);
        }
    }
    Ok(PyTuple::new(py, discovered)?.into_any())
}

/// `ProviderInputError(message)`.
fn input_error(py: Python<'_>, message: String) -> PyResult<PyErr> {
    Ok(PyErr::from_value(
        attr(py, PROVIDER_EXCEPTIONS, "ProviderInputError")?.call1((message,))?,
    ))
}

/// The class name in snake case: an underscore before every ASCII capital after the first
/// character, then lowercased.
fn snake_case(class_name: &str) -> String {
    let mut snake: String = String::with_capacity(class_name.len() + 4);
    for (index, character) in class_name.chars().enumerate() {
        if index > 0 && character.is_ascii_uppercase() {
            snake.push('_');
        }
        snake.push(character);
    }
    snake.to_lowercase()
}

/// Whether `name` is a lower snake_case Python identifier.
fn is_provider_name(name: &str) -> bool {
    let mut characters = name.chars();
    characters
        .next()
        .is_some_and(|first| first.is_ascii_lowercase() || first == '_')
        && characters.all(|rest| rest.is_ascii_lowercase() || rest.is_ascii_digit() || rest == '_')
}

/// The runtime name of a provider class: its `provider_name`, or its class name in snake case.
#[pyfunction]
pub(super) fn provider_name<'py>(provider_class: Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = provider_class.py();
    let class_name: Any<'py> = provider_class.getattr("__name__")?;
    let mut raw_name: Any<'py> = provider_class.getattr("provider_name")?;
    if raw_name.is_none() {
        raw_name =
            PyString::new(py, &snake_case(class_name.cast::<PyString>()?.to_str()?)).into_any();
    }
    if !raw_name.is_truthy()? {
        return Err(input_error(
            py,
            format!("provider {} resolved to an empty name", text(&class_name)?),
        )?);
    }
    let Ok(name) = raw_name.cast::<PyString>() else {
        attr(py, "re", "fullmatch")?.call1((PROVIDER_NAME_PATTERN, &raw_name))?;
        return Ok(raw_name);
    };
    if !is_provider_name(name.to_str()?) {
        return Err(input_error(
            py,
            format!(
                "provider {} has invalid provider name '{}'; provider names must be lower \
                 snake_case Python identifiers",
                text(&class_name)?,
                text(&raw_name)?
            ),
        )?);
    }
    Ok(raw_name)
}
