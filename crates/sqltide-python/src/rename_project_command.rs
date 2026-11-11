//! `sqt rename-project`: rename a project while its Kafka consumer groups, manifest history and
//! project role assignments carry over to the new name.
//!
//! Every step is idempotent and the project config is edited last, so a failure leaves the old
//! name fully working and running the command again completes the rename.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};
use sqltide_clickhouse::metadata::manifest_identity_copy;
use sqltide_clickhouse::resources::database_scoped_consumer_group;
use sqltide_config::project::PROJECT_CONFIG_FILE_NAME;
use sqltide_config::project_rename::{
    ProjectIdentityKeys, project_identity_keys, rename_project_text,
};

use crate::cli::python::{print, user_error};
use crate::control_store::{ControlStore, ProjectRenamePreview, default_control_store_url};
use crate::dev_server::support::attr;
use crate::handling::within;
use crate::py_kwargs;
use crate::realization::managed_source_consumer_groups;
use crate::workflow_statements::numbered;

const NATIVE: &str = "sqltide._native";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const MUTATION_LOCK: &str = "sqltide.executor.workflow.main.target_mutation_lock";
const WAREHOUSE_WORKFLOW: &str = "sqltide.executor.workflow.main._execute_warehouse_workflow";
const CONTROL_STORE_ENV_VAR: &str = "SQLTIDE_CONTROL_STORE_URL";
const SQLITE_FILE_PREFIX: &str = "sqlite:///";
const INTERPOLATION_TOKEN_START: &str = "${";

type Any<'py> = Bound<'py, PyAny>;

/// The manifest history of one project target under the old and new identities.
struct ManifestPlan {
    from_identity: String,
    to_identity: String,
    target_name: String,
    database: String,
    /// Manifests recorded under the old identity and not yet under the new one.
    to_copy: usize,
    /// Manifests recorded under the old identity.
    recorded: usize,
}

/// The role assignments the rename moves, and where they live.
struct RolePlan<'py> {
    store: Bound<'py, ControlStore>,
    url: String,
    preview: ProjectRenamePreview,
}

/// Everything the rename would change, computed without writing anything.
struct RenamePlan<'py> {
    config_path: PathBuf,
    /// The name the warehouse and control store still record.
    current_name: String,
    new_name: String,
    /// Whether this run completes an earlier rename (`--from`) on another target.
    completing: bool,
    /// The identity to add to the project config, when existing groups need it.
    added_identity: Option<String>,
    /// `(source, database-scoped group)` for every managed source, unchanged by the rename.
    groups: Vec<(String, String)>,
    manifests: ManifestPlan,
    roles: Option<RolePlan<'py>>,
    other_targets: Vec<String>,
    /// The edited project config, or `None` when the config already carries the new name.
    edited_config: Option<String>,
}

fn fail(py: Python<'_>, message: &str) -> PyErr {
    user_error(py, message, None)
}

/// The new name, held to the rules the project config applies to `name`.
fn validated_name(py: Python<'_>, new_name: &str, current_name: &str) -> PyResult<String> {
    if new_name.trim().is_empty() {
        return Err(fail(py, "The new project name must be a non-empty string"));
    }
    if new_name.trim() != new_name {
        return Err(fail(
            py,
            "The new project name must not start or end with whitespace",
        ));
    }
    if new_name.contains(INTERPOLATION_TOKEN_START) {
        return Err(fail(
            py,
            "The new project name must be a committed literal and cannot be interpolated",
        ));
    }
    if new_name == current_name {
        return Err(fail(
            py,
            &format!("The project is already named '{current_name}'"),
        ));
    }
    Ok(new_name.to_owned())
}

/// The manifest ids one identity holds for the target, or the reason they cannot be read.
fn manifest_ids(
    client: &Any<'_>,
    database: &str,
    identity: &str,
    target_name: &str,
) -> PyResult<BTreeSet<String>> {
    let py: Python<'_> = client.py();
    let snapshot: Any<'_> = client.call_method(
        "load_manifests",
        (),
        Some(&py_kwargs!(py;
            database = database,
            project_identity = identity,
            target_name = target_name,
            target_database = database,
        )?),
    )?;
    let unavailable: Any<'_> =
        attr(py, "sqltide.adapter.types", "AdapterOptionalStateStatus")?.getattr("UNAVAILABLE")?;
    if snapshot.getattr("status")?.eq(unavailable)? {
        let warning: Any<'_> = snapshot.getattr("warning")?;
        return Err(fail(
            py,
            &format!(
                "Manifest history is unavailable, so the rename cannot carry it over: {}",
                if warning.is_none() {
                    "no detail".to_owned()
                } else {
                    warning.str()?.to_string()
                }
            ),
        ));
    }
    snapshot
        .getattr("manifests")?
        .try_iter()?
        .map(|manifest| manifest?.getattr("manifest_id")?.extract())
        .collect()
}

fn manifest_plan(
    client: &Any<'_>,
    from_identity: &str,
    to_identity: &str,
    target_name: &str,
    database: &str,
) -> PyResult<ManifestPlan> {
    let py: Python<'_> = client.py();
    let recorded: BTreeSet<String> = manifest_ids(client, database, from_identity, target_name)?;
    let copied: BTreeSet<String> = manifest_ids(client, database, to_identity, target_name)?;
    let foreign: Vec<&String> = copied.difference(&recorded).collect();
    if !foreign.is_empty() {
        return Err(fail(
            py,
            &format!(
                "Target '{target_name}' (database '{database}') already records {} manifest(s) \
                 for project '{to_identity}' that did not come from '{from_identity}'; another \
                 project already uses that name here. Choose a different name.",
                foreign.len()
            ),
        ));
    }
    Ok(ManifestPlan {
        from_identity: from_identity.to_owned(),
        to_identity: to_identity.to_owned(),
        target_name: target_name.to_owned(),
        database: database.to_owned(),
        to_copy: recorded.difference(&copied).count(),
        recorded: recorded.len(),
    })
}

/// The control store the project uses: `--control-store-url`, then the environment, then the
/// project-local store when it exists.
fn control_store_url(
    py: Python<'_>,
    project_dir: &Any<'_>,
    option: &Any<'_>,
) -> PyResult<Option<String>> {
    if option.is_truthy()? {
        return Ok(Some(option.extract()?));
    }
    let environment: Any<'_> = attr(py, "os", "environ")?;
    let configured: Any<'_> = environment.call_method1("get", (CONTROL_STORE_ENV_VAR,))?;
    if configured.is_truthy()? {
        return Ok(Some(configured.extract()?));
    }
    let local: String = default_control_store_url(project_dir)?;
    let path: &str = local.strip_prefix(SQLITE_FILE_PREFIX).unwrap_or(&local);
    Ok(Path::new(path).exists().then_some(local))
}

fn role_plan<'py>(
    py: Python<'py>,
    url: Option<String>,
    from_project: &str,
    to_project: &str,
) -> PyResult<Option<RolePlan<'py>>> {
    let Some(url) = url else {
        return Ok(None);
    };
    let store: Bound<'py, ControlStore> = attr(py, NATIVE, "ControlStore")?
        .call((), Some(&py_kwargs!(py; url = &url)?))?
        .cast_into()?;
    let preview: ProjectRenamePreview =
        store
            .get()
            .preview_project_rename(py, from_project, to_project)?;
    if !preview.conflicts.is_empty() {
        return Err(fail(
            py,
            &format!(
                "Project '{to_project}' already holds role assignments that '{from_project}' \
                 also holds: {}. Revoke one of each pair, then run the rename again.",
                preview.conflicts.join(", ")
            ),
        ));
    }
    Ok(Some(RolePlan {
        store,
        url,
        preview,
    }))
}

fn other_targets(loaded_project: &Any<'_>, target_name: &str) -> PyResult<Vec<String>> {
    let configuration: Any<'_> = loaded_project.getattr("configuration")?;
    if configuration.is_none() {
        return Ok(Vec::new());
    }
    let mut names: Vec<String> = Vec::new();
    for entry in configuration
        .getattr("project")?
        .getattr("targets")?
        .try_iter()?
    {
        let name: String = entry?.get_item(0)?.extract()?;
        if name != target_name {
            names.push(name);
        }
    }
    Ok(names)
}

/// The old and new names when completing an earlier rename on another target: the config
/// must already carry `new_name`, and `from_name` must be the name it replaced.
fn completed_names(
    py: Python<'_>,
    from_name: &str,
    new_name: &str,
    config_name: &str,
) -> PyResult<(String, String)> {
    if from_name.trim().is_empty() || from_name.trim() != from_name {
        return Err(fail(
            py,
            "--from must be the previous project name, a non-empty string without surrounding \
             whitespace",
        ));
    }
    if from_name == new_name {
        return Err(fail(
            py,
            &format!(
                "--from names the previous project name and cannot be the new name '{new_name}'"
            ),
        ));
    }
    if from_name == config_name {
        return Err(fail(
            py,
            &format!(
                "sqltide_project.toml is still named '{config_name}'. Run `sqt rename-project \
                 {new_name}` without --from first; use --from only on the remaining targets \
                 after the config is renamed"
            ),
        ));
    }
    if new_name != config_name {
        return Err(fail(
            py,
            &format!(
                "With --from, the new name must be the name sqltide_project.toml already \
                 carries ('{config_name}'), not '{new_name}'"
            ),
        ));
    }
    Ok((from_name.to_owned(), new_name.to_owned()))
}

#[allow(clippy::too_many_arguments)]
fn plan_rename<'py>(
    py: Python<'py>,
    project_dir: &Any<'py>,
    new_name: &str,
    from_name: Option<&str>,
    database: &str,
    control_store_option: &Any<'py>,
    client: &Any<'py>,
    loaded_project: &Any<'py>,
    analysis: &Any<'py>,
    adapter_profile: &Any<'py>,
) -> PyResult<RenamePlan<'py>> {
    let config_path: PathBuf = project_dir
        .call_method1("__truediv__", (PROJECT_CONFIG_FILE_NAME,))?
        .extract()?;
    let config_text: String = std::fs::read_to_string(&config_path).map_err(|error| {
        fail(
            py,
            &format!("Could not read {}: {error}", config_path.display()),
        )
    })?;
    let keys: ProjectIdentityKeys =
        project_identity_keys(&config_text).map_err(|message| fail(py, &message))?;
    let compiled: Any<'py> = analysis.getattr("compiled_project")?;
    let config_name: String = compiled.getattr("project_name")?.extract()?;
    let (current_name, new_name): (String, String) = match from_name {
        Some(from_name) => completed_names(py, from_name, new_name, &config_name)?,
        None => (
            config_name.clone(),
            validated_name(py, new_name, &config_name)?,
        ),
    };
    let completing: bool = from_name.is_some();
    let identity: Option<String> = compiled.getattr("consumer_group_identity")?.extract()?;
    if identity
        != keys
            .consumer_group_identity
            .map(|value| value.trim().to_owned())
    {
        return Err(fail(
            py,
            &format!(
                "The consumer group identity in {} does not match the loaded project; check \
                 the file and run the rename again",
                config_path.display()
            ),
        ));
    }
    let before: Vec<(String, String)> = managed_source_consumer_groups(
        &compiled,
        adapter_profile,
        &current_name,
        identity.as_deref(),
    )?;
    let renamed = |identity: Option<&str>| {
        managed_source_consumer_groups(&compiled, adapter_profile, &new_name, identity)
    };
    // Keep the old name in the groups only when some group would otherwise change. A completing
    // run never edits the config, so its identity must already keep the groups.
    let added_identity: Option<String> =
        if !completing && identity.is_none() && renamed(None)? != before {
            Some(current_name.clone())
        } else {
            None
        };
    let after: Vec<(String, String)> = renamed(identity.as_deref().or(added_identity.as_deref()))?;
    let changed: Vec<String> = before
        .iter()
        .zip(&after)
        .filter(|(old, new)| old != new)
        .map(|((source, old), (_, new))| format!("{source}: '{old}' -> '{new}'"))
        .collect();
    if !changed.is_empty() || before.len() != after.len() {
        return Err(fail(
            py,
            &format!(
                "Renaming would change Kafka consumer groups, so sources would not resume from \
                 their committed offsets: {}{}",
                changed.join("; "),
                if completing {
                    format!(
                        ". Set consumer_group_identity = {} in sqltide_project.toml first",
                        quoted(&current_name)
                    )
                } else {
                    String::new()
                }
            ),
        ));
    }
    let groups: Vec<(String, String)> = before
        .into_iter()
        .map(|(source, group)| (source, database_scoped_consumer_group(&group, database)))
        .collect();
    let target_name: String = compiled.getattr("target_name")?.extract()?;
    let from_identity: String = if completing {
        current_name.clone()
    } else {
        attr(py, NATIVE, "resolve_manifest_project_identity")?
            .call((), Some(&py_kwargs!(py; analysis = analysis)?))?
            .extract()?
    };
    let manifests: ManifestPlan =
        manifest_plan(client, &from_identity, &new_name, &target_name, database)?;
    let roles: Option<RolePlan<'py>> = role_plan(
        py,
        control_store_url(py, project_dir, control_store_option)?,
        &current_name,
        &new_name,
    )?;
    let edited_config: Option<String> = if completing {
        None
    } else {
        Some(
            rename_project_text(&config_text, &new_name, added_identity.as_deref())
                .map_err(|message| fail(py, &message))?,
        )
    };
    Ok(RenamePlan {
        config_path,
        current_name,
        new_name,
        completing,
        added_identity,
        groups,
        manifests,
        roles,
        other_targets: other_targets(loaded_project, &target_name)?,
        edited_config,
    })
}

fn quoted(value: &str) -> String {
    sqltide_config::project_rename::toml_basic_string(value)
}

fn render_plan(plan: &RenamePlan<'_>, applied: bool) -> String {
    let manifests: &ManifestPlan = &plan.manifests;
    let mut lines: Vec<String> = vec![
        if applied {
            "Project Renamed".to_owned()
        } else {
            "Rename Project Preview".to_owned()
        },
        format!("Project: {} -> {}", plan.current_name, plan.new_name),
        format!(
            "Target: {} (database {})",
            manifests.target_name, manifests.database
        ),
    ];
    if plan.completing {
        lines.push(format!(
            "Config: {} already named {}; unchanged",
            plan.config_path.display(),
            quoted(&plan.new_name)
        ));
    } else {
        lines.push(format!("Config: {}", plan.config_path.display()));
        lines.push(format!("  name = {}", quoted(&plan.new_name)));
    }
    if let Some(identity) = &plan.added_identity {
        lines.push(format!(
            "  consumer_group_identity = {} (added so existing Kafka consumer groups stay the same)",
            quoted(identity)
        ));
    }
    lines.push(format!(
        "Kafka consumer groups: {} unchanged",
        plan.groups.len()
    ));
    lines.extend(
        plan.groups
            .iter()
            .map(|(source, group)| format!("  - {source}: {group}")),
    );
    lines.push(format!(
        "Manifest history: {} of {} manifest(s) {} from '{}' to '{}'",
        manifests.to_copy,
        manifests.recorded,
        if applied { "copied" } else { "to copy" },
        manifests.from_identity,
        manifests.to_identity
    ));
    match &plan.roles {
        None => lines.push("Role assignments: no control store in use".to_owned()),
        Some(roles) => lines.push(format!(
            "Role assignments: {} {} from '{}' to '{}'{} (control store {})",
            roles.preview.moving,
            if applied { "moved" } else { "to move" },
            plan.current_name,
            plan.new_name,
            if roles.preview.existing > 0 {
                format!(
                    "; {} already recorded under '{}'",
                    roles.preview.existing, plan.new_name
                )
            } else {
                String::new()
            },
            redacted_url(&roles.url)
        )),
    }
    if !plan.other_targets.is_empty() && !plan.completing {
        lines.push(format!(
            "Other targets keep manifest history and role grants under '{}' until you run \
             `sqt rename-project {} --from {} --target <target>` there: {}",
            plan.current_name,
            plan.new_name,
            plan.current_name,
            plan.other_targets.join(", ")
        ));
    }
    lines.push(
        "Unchanged: run history and audit and check status follow the project directory name, \
         not the project name."
            .to_owned(),
    );
    lines.push(if applied {
        "Restart any running `sqt dev` server to pick up the new name.".to_owned()
    } else {
        "Dry run: nothing was changed. Run again with --apply to rename.".to_owned()
    });
    lines.join("\n")
}

/// A control store URL without its password.
fn redacted_url(url: &str) -> String {
    let Some((scheme, rest)) = url.split_once("://") else {
        return url.to_owned();
    };
    let Some((credentials, host)) = rest.rsplit_once('@') else {
        return url.to_owned();
    };
    match credentials.split_once(':') {
        Some((user, _)) => format!("{scheme}://{user}:***@{host}"),
        None => url.to_owned(),
    }
}

/// Copy the manifest history under the target mutation lock; refuse while a run holds it.
fn copy_manifests(client: &Any<'_>, plan: &ManifestPlan) -> PyResult<usize> {
    let py: Python<'_> = client.py();
    if !client
        .call_method1("database_exists", (&plan.database,))?
        .is_truthy()?
    {
        // Never built: no manifests to copy and no run that could hold the lock.
        return Ok(0);
    }
    let lock: Any<'_> = attr(py, MUTATION_LOCK, "target_mutation_lock")?.call(
        (),
        Some(&py_kwargs!(py; connection = client, database = &plan.database)?),
    )?;
    let copied = within(&lock, || {
        let fresh: ManifestPlan = manifest_plan(
            client,
            &plan.from_identity,
            &plan.to_identity,
            &plan.target_name,
            &plan.database,
        )?;
        if fresh.to_copy == 0 {
            return Ok(0);
        }
        let statement: String = manifest_identity_copy(
            &plan.database,
            &plan.from_identity,
            &plan.to_identity,
            &plan.target_name,
            &plan.database,
        );
        let rendered: Any<'_> = PyTuple::new(py, [statement])?.into_any();
        let statements: Vec<Any<'_>> =
            numbered(&rendered, 1, "copy_manifest_history", "FINALIZATION")?;
        attr(py, WAREHOUSE_WORKFLOW, "execute_warehouse_workflow")?.call(
            (),
            Some(&py_kwargs!(py; statements = PyTuple::new(py, statements)?, connection = client)?),
        )?;
        let after: ManifestPlan = manifest_plan(
            client,
            &plan.from_identity,
            &plan.to_identity,
            &plan.target_name,
            &plan.database,
        )?;
        if after.to_copy != 0 {
            return Err(fail(
                py,
                &format!(
                    "{} manifest(s) were not copied to '{}'; run the rename again",
                    after.to_copy, plan.to_identity
                ),
            ));
        }
        Ok(fresh.to_copy)
    });
    copied.map_err(|error| {
        let locked: bool = attr(py, ADAPTER_EXCEPTIONS, "AdapterTargetMutationLockError")
            .and_then(|class| error.value(py).is_instance(&class))
            .unwrap_or(false);
        if !locked {
            return error;
        }
        let refused: PyErr = fail(
            py,
            &format!(
                "A run is active on target database '{}' ({}). Wait for it to finish, then \
                 run the rename again.",
                plan.database,
                error.value(py)
            ),
        );
        refused.set_cause(py, Some(error));
        refused
    })
}

/// Write the edited config next to the original and move it into place.
fn write_config(path: &Path, text: &str) -> std::io::Result<()> {
    let staged: PathBuf = path.with_file_name(format!(
        ".{}.rename-project",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("project")
    ));
    std::fs::write(&staged, text)?;
    if let Ok(metadata) = std::fs::metadata(path) {
        std::fs::set_permissions(&staged, metadata.permissions())?;
    }
    std::fs::rename(&staged, path).inspect_err(|_| {
        let _ = std::fs::remove_file(&staged);
    })
}

fn apply_rename(py: Python<'_>, client: &Any<'_>, plan: &RenamePlan<'_>) -> PyResult<()> {
    copy_manifests(client, &plan.manifests)?;
    let moved: Vec<String> = match &plan.roles {
        Some(roles) => roles
            .store
            .get()
            .rename_project(py, &plan.current_name, &plan.new_name)?,
        None => Vec::new(),
    };
    let Some(edited_config) = &plan.edited_config else {
        return Ok(());
    };
    if let Err(error) = write_config(&plan.config_path, edited_config) {
        let mut message: String = format!(
            "Could not write {}: {error}. The project keeps its old name",
            plan.config_path.display()
        );
        if let Some(roles) = &plan.roles
            && !moved.is_empty()
            && let Err(restore) = roles.store.get().restore_project_rename(
                py,
                &moved,
                &plan.current_name,
                &plan.new_name,
            )
        {
            message.push_str(&format!(
                "; its role assignments stayed under '{}' ({restore}), so run the rename \
                 again to finish",
                plan.new_name
            ));
        }
        return Err(fail(py, &message));
    }
    Ok(())
}

/// Preview the rename of the project, or apply it with `apply`.
#[pyfunction]
#[pyo3(signature = (
    *, pipelines_root, project_dir, new_name, database, control_store_url, apply, client,
    loaded_project, adapter_profile, from_name = None,
))]
#[allow(clippy::too_many_arguments)]
fn run_rename_project<'py>(
    py: Python<'py>,
    pipelines_root: Any<'py>,
    project_dir: Any<'py>,
    new_name: String,
    database: Any<'py>,
    control_store_url: Any<'py>,
    apply: bool,
    client: Any<'py>,
    loaded_project: Any<'py>,
    adapter_profile: Any<'py>,
    from_name: Option<String>,
) -> PyResult<i64> {
    let analysis: Any<'py> = attr(py, NATIVE, "analyze_project")?.call(
        (),
        Some(&py_kwargs!(py;
            pipelines_root = pipelines_root,
            loaded_project = &loaded_project,
            adapter_profile = &adapter_profile,
        )?),
    )?;
    let pipelines: Any<'py> = PyList::new(
        py,
        analysis
            .getattr("compile_inputs")?
            .getattr("pipelines")?
            .try_iter()?
            .collect::<PyResult<Vec<_>>>()?,
    )?
    .into_any();
    let database: String =
        crate::cli::invocation::resolve_default_database(py, &pipelines, database)?.extract()?;
    let plan: RenamePlan<'py> = plan_rename(
        py,
        &project_dir,
        &new_name,
        from_name.as_deref(),
        &database,
        &control_store_url,
        &client,
        &loaded_project,
        &analysis,
        &adapter_profile,
    )?;
    if !apply {
        print(py, &render_plan(&plan, false))?;
        return Ok(0);
    }
    apply_rename(py, &client, &plan)?;
    print(py, &render_plan(&plan, true))?;
    Ok(0)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(run_rename_project, module)?)
}
