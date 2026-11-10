//! `sqt admin`: operator-only account bootstrap and recovery against the control store.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

use crate::cli::python::{getattr_none, print, raised, raised_during, raised_from, user_error};
use crate::dev_server::support::attr;

const CONTROL_STORE_ENV_VAR: &str = "SQLTIDE_CONTROL_STORE_URL";
const VIEWER_ROLE: &str = "viewer";
const AUTH_TYPES: &str = "sqltide.auth.types";

/// Run one account operation without opening a warehouse connection; exit status 0.
pub(crate) fn run_admin_command<'py>(
    py: Python<'py>,
    args: &Bound<'py, PyAny>,
    environment: &Bound<'py, PyAny>,
    working_directory: &Bound<'py, PyAny>,
) -> PyResult<i64> {
    let project_dir_value: Bound<'py, PyAny> = getattr_none(args, "project_dir")?;
    let project_dir: Bound<'py, PyAny> = if project_dir_value.is_none() {
        working_directory.clone()
    } else if project_dir_value.call_method0("is_absolute")?.is_truthy()? {
        project_dir_value
    } else {
        working_directory.call_method1("__truediv__", (project_dir_value,))?
    };
    let mut url: Bound<'py, PyAny> = getattr_none(args, "control_store_url")?;
    if !url.is_truthy()? {
        url = environment.call_method1("get", (CONTROL_STORE_ENV_VAR,))?;
    }
    if !url.is_truthy()? {
        let kwargs: Bound<'py, PyDict> = PyDict::new(py);
        kwargs.set_item("project_dir", &project_dir)?;
        url = attr(
            py,
            "sqltide.auth.main.default_control_store_url",
            "default_control_store_url",
        )?
        .call((), Some(&kwargs))?;
    }
    let outcome: PyResult<i64> = (|| {
        let kwargs: Bound<'py, PyDict> = PyDict::new(py);
        kwargs.set_item("url", &url)?;
        let store: Bound<'py, PyAny> =
            attr(py, "sqltide.auth.classes.control_store", "ControlStore")?
                .call((), Some(&kwargs))?;
        let dispatched: PyResult<i64> = dispatch_admin_command(py, args, &store);
        match store.call_method0("close") {
            Ok(_) => dispatched,
            Err(error) => Err(match dispatched {
                Err(original) => raised_during(py, error, original),
                Ok(_) => error,
            }),
        }
    })();
    match outcome {
        Err(error)
            if raised(py, &error, "sqltide.auth.exceptions", "ControlStoreError")?
                || error.is_instance_of::<PyValueError>(py) =>
        {
            let message: String = error.value(py).str()?.to_string();
            Err(raised_from(py, user_error(py, &message, None), error))
        }
        outcome => outcome,
    }
}

/// `store.method(**kwargs)`.
fn call<'py>(
    store: &Bound<'py, PyAny>,
    method: &str,
    entries: &[(&str, Bound<'py, PyAny>)],
) -> PyResult<Bound<'py, PyAny>> {
    let kwargs: Bound<'py, PyDict> = PyDict::new(store.py());
    for (name, value) in entries {
        kwargs.set_item(*name, value)?;
    }
    store.call_method(method, (), Some(&kwargs))
}

/// Execute one parsed command against an open control store.
fn dispatch_admin_command<'py>(
    py: Python<'py>,
    args: &Bound<'py, PyAny>,
    store: &Bound<'py, PyAny>,
) -> PyResult<i64> {
    let command: String = attr(py, "sqltide.cli.admin.types", "AdminCommand")?
        .call1((args.getattr("admin_command")?,))?
        .str()?
        .to_string();
    if command == "migrate" {
        store.call_method0("bootstrap")?;
        print(py, "SQLTide account schema is ready")?;
        return Ok(0);
    }
    if command == "create-user" {
        return create_user(py, args, store);
    }
    let username: Bound<'py, PyAny> = args.getattr("username")?;
    let account: Bound<'py, PyAny> = call(
        store,
        "get_user_by_username",
        &[("username", username.clone())],
    )?;
    if account.is_none() {
        return Err(user_error(
            py,
            &format!("User '{}' was not found", username.str()?),
            None,
        ));
    }
    let user_id: Bound<'py, PyAny> = account.getattr("user_id")?;
    let none: Bound<'py, PyAny> = py.None().into_bound(py);
    match command.as_str() {
        "grant-role" | "revoke-role" => {
            let role: Bound<'py, PyAny> = args.getattr("role")?;
            let granting: bool = command == "grant-role";
            let updated: Bound<'py, PyAny> = call(
                store,
                if granting {
                    "grant_role"
                } else {
                    "revoke_role"
                },
                &[
                    ("user_id", user_id),
                    ("role_name", role.clone()),
                    ("actor_user_id", none),
                ],
            )?;
            let (verb, preposition) = if granting {
                ("Granted", "to")
            } else {
                ("Revoked", "from")
            };
            print(
                py,
                &format!(
                    "{verb} {} {preposition} {}",
                    role.str()?,
                    updated.getattr("username")?.str()?
                ),
            )?;
        }
        "enable-user" | "disable-user" => {
            let enabled: bool = command == "enable-user";
            let updated: Bound<'py, PyAny> = call(
                store,
                "set_user_active",
                &[
                    ("user_id", user_id),
                    (
                        "is_active",
                        pyo3::types::PyBool::new(py, enabled).to_owned().into_any(),
                    ),
                    ("actor_user_id", none),
                ],
            )?;
            print(
                py,
                &format!(
                    "{} {}",
                    if enabled { "Enabled" } else { "Disabled" },
                    updated.getattr("username")?.str()?
                ),
            )?;
        }
        "reset-password" => {
            let password: Bound<'py, PyAny> = read_password(py)?;
            call(
                store,
                "reset_password",
                &[
                    ("user_id", user_id),
                    ("password", password),
                    ("actor_user_id", none),
                ],
            )?;
            print(
                py,
                &format!("Reset password for {}", account.getattr("username")?.str()?),
            )?;
        }
        _ => {
            return Err(user_error(
                py,
                &format!("Unknown admin command '{command}'"),
                None,
            ));
        }
    }
    Ok(0)
}

fn create_user<'py>(
    py: Python<'py>,
    args: &Bound<'py, PyAny>,
    store: &Bound<'py, PyAny>,
) -> PyResult<i64> {
    let source: Bound<'py, PyAny> = attr(py, AUTH_TYPES, "AuthenticationSource")?
        .call1((args.getattr("authentication_source")?,))?;
    let source_name: String = source.str()?.to_string();
    let none: Bound<'py, PyAny> = py.None().into_bound(py);
    let password: Bound<'py, PyAny> = if source_name == "password" {
        read_password(py)?
    } else {
        none.clone()
    };
    let trusted: bool = source_name == "trusted_proxy";
    let username: Bound<'py, PyAny> = args.getattr("username")?;
    let roles_arg: Bound<'py, PyAny> = args.getattr("role")?;
    let roles: Bound<'py, PyAny> = if roles_arg.is_truthy()? {
        PyTuple::new(py, roles_arg.try_iter()?.collect::<PyResult<Vec<_>>>()?)?.into_any()
    } else {
        PyTuple::new(py, [VIEWER_ROLE])?.into_any()
    };
    let account: Bound<'py, PyAny> = call(
        store,
        "create_user",
        &[
            ("username", username.clone()),
            ("display_name", args.getattr("display_name")?),
            ("email", args.getattr("email")?),
            ("password", password),
            (
                "authentication_source",
                if trusted {
                    source.clone()
                } else {
                    none.clone()
                },
            ),
            (
                "external_subject",
                if trusted {
                    username.call_method0("strip")?.call_method0("casefold")?
                } else {
                    none
                },
            ),
            ("roles", roles),
        ],
    )?;
    print(
        py,
        &format!(
            "Created user {} ({})",
            account.getattr("username")?.str()?,
            account.getattr("user_id")?.str()?
        ),
    )?;
    Ok(0)
}

/// One password from a pipe, or entered twice at an interactive terminal.
fn read_password(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    let stdin: Bound<'_, PyAny> = py.import("sys")?.getattr("stdin")?;
    if !stdin.call_method0("isatty")?.is_truthy()? {
        let password: Bound<'_, PyAny> = stdin
            .call_method0("readline")?
            .call_method1("rstrip", ("\n",))?;
        if !password.is_truthy()? {
            return Err(user_error(py, "Password input was empty", None));
        }
        return Ok(password);
    }
    let getpass: Bound<'_, PyAny> = attr(py, "getpass", "getpass")?;
    let password: Bound<'_, PyAny> = getpass.call1(("Password: ",))?;
    if !password.eq(getpass.call1(("Confirm password: ",))?)? {
        return Err(user_error(py, "Passwords do not match", None));
    }
    Ok(password)
}
