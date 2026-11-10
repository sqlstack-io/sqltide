//! The relational SQLite/PostgreSQL account control store. Statements, row mapping, sessions,
//! tokens, auditing and role rules run here; the Python driver glue only opens transactions and
//! executes bound statements.

mod sql;

use std::sync::LazyLock;
use std::sync::atomic::{AtomicU64, Ordering};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDateTime, PyDict, PyString, PyTuple};
use regex::Regex;
use sha2::{Digest, Sha256};

use crate::dev_server::support::attr;
use crate::py_kwargs;

const DRIVER: &str = "sqltide.auth.classes.relational_driver";
const PASSWORDS: &str = "sqltide.auth._helpers.password_hashing";
const AUTH_MODELS: &str = "sqltide.auth.models";
const AUTH_TYPES: &str = "sqltide.auth.types";
const AUTH_EXCEPTIONS: &str = "sqltide.auth.exceptions";
const SCHEMA_VERSION: i64 = 1;
const ACCOUNT_SCHEMA_COMPONENT: &str = "accounts";
const ADMIN_ROLE: &str = "admin";
const VIEWER_ROLE: &str = "viewer";
const PASSWORD_MIN_LENGTH: usize = 12;
const SQLITE_MEMORY_PATH: &str = ":memory:";
const SQLITE_URL_PREFIX: &str = "sqlite:";
const SQLITE_FILE_PREFIX: &str = "sqlite:///";
const STREAMBUILD_TABLE_PREFIX: &str = "streambuild_";
const TABLE_PREFIX: &str = "sqltide_";
const DUMMY_PASSWORD: &str = "sqltide dummy password verification";
const LAST_SEEN_WRITE_INTERVAL_SECONDS: i64 = 60;
const SESSION_TOKEN_BYTES: usize = 48;
const CSRF_TOKEN_BYTES: usize = 32;
const URLSAFE_ALPHABET: &[u8; 64] =
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

static USERNAME_PATTERN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[a-z0-9][a-z0-9._@+-]{0,127}$").expect("valid username pattern")
});
static DUMMY_PASSWORD_HASH: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

type Any<'py> = Bound<'py, PyAny>;
type Row<'py> = Bound<'py, PyDict>;

fn error(py: Python<'_>, class: &str, message: String) -> PyErr {
    match attr(py, AUTH_EXCEPTIONS, class).and_then(|class| class.call1((message,))) {
        Ok(error) => PyErr::from_value(error),
        Err(failure) => failure,
    }
}

/// `error` chained from `cause`, as `raise error from cause` leaves it.
fn chained(py: Python<'_>, error: PyErr, cause: PyErr) -> PyErr {
    error.set_cause(py, Some(cause));
    error
}

fn none(py: Python<'_>) -> Any<'_> {
    py.None().into_bound(py)
}

/// `str(value)` of a value that may be `None`.
fn text_or_none<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    if value.is_none() {
        return Ok(value.clone());
    }
    Ok(value.str()?.into_any())
}

fn uuid<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    attr(value.py(), "uuid", "UUID")?.call1((value.str()?,))
}

fn uuid_or_none<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    if value.is_none() {
        return Ok(value.clone());
    }
    uuid(value)
}

/// `value.strip() or None`, with `None` kept.
fn clean_optional<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    if value.is_none() {
        return Ok(value.clone());
    }
    let cleaned: Any<'py> = value.call_method0("strip")?;
    if cleaned.is_truthy()? {
        Ok(cleaned)
    } else {
        Ok(none(value.py()))
    }
}

/// The canonical form of a username, or `AccountValidationError`.
pub(crate) fn canonical_username<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    let canonical: Any<'py> = value.call_method0("strip")?.call_method0("casefold")?;
    let matched: bool = canonical
        .cast::<PyString>()
        .ok()
        .and_then(|text| text.to_str().ok())
        .is_some_and(|text| USERNAME_PATTERN.is_match(text));
    if !matched {
        return Err(error(
            value.py(),
            "AccountValidationError",
            "Username must start with an alphanumeric character and contain only letters, \
             numbers, '.', '_', '@', '+', or '-'"
                .to_owned(),
        ));
    }
    Ok(canonical)
}

/// The canonical form of a username, or `AccountValidationError`.
#[pyfunction]
#[pyo3(name = "canonical_username")]
fn canonical_username_py<'py>(value: Any<'py>) -> PyResult<Any<'py>> {
    canonical_username(&value)
}

fn required_assignment_name<'py>(
    value: &Any<'py>,
    label: &str,
    maximum_length: usize,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = value.py();
    let cleaned: Any<'py> = value.call_method0("strip")?;
    if !cleaned.is_truthy()? {
        return Err(error(
            py,
            "AccountValidationError",
            format!("{label} must not be empty"),
        ));
    }
    if cleaned.len()? > maximum_length {
        return Err(error(
            py,
            "AccountValidationError",
            format!("{label} must contain at most {maximum_length} characters"),
        ));
    }
    Ok(cleaned)
}

/// `secrets.token_urlsafe(length)` from the operating system's random source.
fn token_urlsafe(length: usize) -> PyResult<String> {
    let mut bytes: Vec<u8> = vec![0; length];
    getrandom::fill(&mut bytes)
        .map_err(|failure| pyo3::exceptions::PyOSError::new_err(failure.to_string()))?;
    Ok(urlsafe_base64(&bytes))
}

/// Unpadded URL-safe base64, as `secrets.token_urlsafe` encodes its bytes.
fn urlsafe_base64(bytes: &[u8]) -> String {
    let mut encoded: String = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let group: u32 = chunk.iter().enumerate().fold(0, |acc, (index, byte)| {
            acc | (u32::from(*byte) << (16 - 8 * index))
        });
        for index in 0..=chunk.len() {
            encoded.push(char::from(
                URLSAFE_ALPHABET[((group >> (18 - 6 * index)) & 63) as usize],
            ));
        }
    }
    encoded
}

fn token_hash(token: &Any<'_>) -> PyResult<String> {
    let encoded: Bound<'_, PyBytes> = token.call_method1("encode", ("utf-8",))?.cast_into()?;
    Ok(Sha256::digest(encoded.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect())
}

fn utc(py: Python<'_>) -> PyResult<Any<'_>> {
    attr(py, "datetime", "UTC")
}

fn now(py: Python<'_>) -> PyResult<Any<'_>> {
    attr(py, "datetime", "datetime")?.call_method1("now", (utc(py)?,))
}

fn seconds<'py>(py: Python<'py>, count: Any<'py>) -> PyResult<Any<'py>> {
    attr(py, "datetime", "timedelta")?.call((), Some(&py_kwargs!(py; seconds = count)?))
}

/// A database timestamp as an aware UTC datetime; SQLite hands back the stored text.
fn as_utc<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = value.py();
    let moment: Any<'py> = if value.is_instance_of::<PyString>() {
        attr(py, "datetime", "datetime")?.call_method1("fromisoformat", (value,))?
    } else {
        value.clone()
    };
    if !moment.is_instance_of::<PyDateTime>() {
        return Err(error(
            py,
            "ControlStoreError",
            format!(
                "Expected database timestamp, received {}",
                value.get_type().name()?
            ),
        ));
    }
    if moment.getattr("tzinfo")?.is_none() {
        moment.call_method("replace", (), Some(&py_kwargs!(py; tzinfo = utc(py)?)?))
    } else {
        moment.call_method1("astimezone", (utc(py)?,))
    }
}

fn as_utc_or_none<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    if value.is_none() {
        return Ok(value.clone());
    }
    as_utc(value)
}

/// One driver connection with its transaction open.
struct Transaction<'py> {
    driver: Any<'py>,
    connection: Any<'py>,
    postgres: bool,
}

impl<'py> Transaction<'py> {
    fn py(&self) -> Python<'py> {
        self.driver.py()
    }

    /// Run one statement; return its rows and affected row count.
    fn run(
        &self,
        statement: &str,
        parameters: &[(&str, Any<'py>)],
    ) -> PyResult<(Vec<Row<'py>>, i64)> {
        let bound: Bound<'py, PyDict> = PyDict::new(self.py());
        for (name, value) in parameters {
            bound.set_item(name, value)?;
        }
        let result: Bound<'py, PyTuple> = self
            .driver
            .call_method(
                "execute",
                (),
                Some(&py_kwargs!(self.py(); connection = &self.connection, sql = statement, parameters = bound)?),
            )?
            .cast_into()?;
        let rows: Vec<Row<'py>> = result
            .get_item(0)?
            .try_iter()?
            .map(|row| row?.cast_into::<PyDict>().map_err(Into::into))
            .collect::<PyResult<_>>()?;
        Ok((rows, result.get_item(1)?.extract()?))
    }

    fn rows(&self, statement: &str, parameters: &[(&str, Any<'py>)]) -> PyResult<Vec<Row<'py>>> {
        self.run(statement, parameters).map(|(rows, _)| rows)
    }

    fn first(
        &self,
        statement: &str,
        parameters: &[(&str, Any<'py>)],
    ) -> PyResult<Option<Row<'py>>> {
        Ok(self.rows(statement, parameters)?.into_iter().next())
    }

    /// The first column of the first row, or `None`.
    fn scalar(
        &self,
        statement: &str,
        parameters: &[(&str, Any<'py>)],
    ) -> PyResult<Option<Any<'py>>> {
        match self.first(statement, parameters)? {
            Some(row) => Ok(row.values().iter().next()),
            None => Ok(None),
        }
    }

    fn execute(&self, statement: &str, parameters: &[(&str, Any<'py>)]) -> PyResult<i64> {
        self.run(statement, parameters).map(|(_, count)| count)
    }

    /// A timestamp as this dialect binds it: SQLite stores SQLAlchemy's text form.
    fn time(&self, moment: &Any<'py>) -> PyResult<Any<'py>> {
        if self.postgres {
            return Ok(moment.clone());
        }
        let field = |name: &str| -> PyResult<i64> { moment.getattr(name)?.extract() };
        Ok(PyString::new(
            self.py(),
            &format!(
                "{:04}-{:02}-{:02} {:02}:{:02}:{:02}.{:06}",
                field("year")?,
                field("month")?,
                field("day")?,
                field("hour")?,
                field("minute")?,
                field("second")?,
                field("microsecond")?
            ),
        )
        .into_any())
    }
}

fn driver_class(py: Python<'_>) -> PyResult<Any<'_>> {
    attr(py, DRIVER, "RelationalDriver")
}

/// Whether `failure` is a constraint violation the driver reported.
fn is_integrity(py: Python<'_>, failure: &PyErr) -> PyResult<bool> {
    driver_class(py)?
        .getattr("error_kind")?
        .call1((failure.value(py),))?
        .eq("integrity")
}

fn object<'py, T: IntoPyObject<'py>>(py: Python<'py>, value: T) -> PyResult<Any<'py>> {
    pyo3::IntoPyObjectExt::into_bound_py_any(value, py)
}

fn model<'py>(py: Python<'py>, name: &str, arguments: Bound<'py, PyDict>) -> PyResult<Any<'py>> {
    attr(py, AUTH_MODELS, name)?.call((), Some(&arguments))
}

/// Synchronous account repository shared by dev server worker threads and the CLI. Instances
/// keep a `__dict__` so callers can wrap one method of one store, as with the Python class.
#[pyclass(module = "sqltide._native", frozen, dict)]
pub(crate) struct ControlStore {
    url: Py<PyAny>,
    driver: Py<PyAny>,
    postgres: bool,
    revision: AtomicU64,
}

impl ControlStore {
    /// Run `work` in one transaction: committed when it succeeds, rolled back when it fails.
    fn transaction<'py, T>(
        &self,
        py: Python<'py>,
        work: impl FnOnce(&Transaction<'py>) -> PyResult<T>,
    ) -> PyResult<T> {
        let driver: &Any<'py> = self.driver.bind(py);
        let transaction: Transaction<'py> = Transaction {
            connection: driver.call_method0("begin")?,
            driver: driver.clone(),
            postgres: self.postgres,
        };
        match work(&transaction) {
            Ok(value) => {
                driver.call_method1("commit", (&transaction.connection,))?;
                Ok(value)
            }
            Err(failure) => {
                if let Err(rollback) = driver.call_method1("rollback", (&transaction.connection,)) {
                    rollback
                        .value(py)
                        .setattr("__context__", failure.value(py))?;
                    return Err(rollback);
                }
                Err(failure)
            }
        }
    }

    fn invalidate(&self) {
        self.revision.fetch_add(1, Ordering::SeqCst);
    }

    fn rename_streambuild_tables(&self, py: Python<'_>) -> PyResult<()> {
        self.transaction(py, |transaction| {
            let listing: &str = if self.postgres {
                sql::POSTGRES_TABLE_NAMES
            } else {
                sql::SQLITE_TABLE_NAMES
            };
            let mut existing: Vec<String> = Vec::new();
            for row in transaction.rows(listing, &[])? {
                if let Some(name) = row.values().iter().next() {
                    existing.push(name.extract()?);
                }
            }
            existing.sort();
            for name in &existing {
                let Some(rest) = name.strip_prefix(STREAMBUILD_TABLE_PREFIX) else {
                    continue;
                };
                let renamed: String = format!("{TABLE_PREFIX}{rest}");
                if existing.contains(&renamed) {
                    continue;
                }
                transaction.execute(
                    &format!("ALTER TABLE \"{name}\" RENAME TO \"{renamed}\""),
                    &[],
                )?;
            }
            Ok(())
        })
    }

    fn create_tables(&self, py: Python<'_>) -> PyResult<()> {
        self.transaction(py, |transaction| {
            let mut missing: Vec<&str> = Vec::new();
            for name in sql::PROBE_ORDER {
                let present: bool = if self.postgres {
                    transaction
                        .first(sql::POSTGRES_HAS_TABLE, &[("name", object(py, name)?)])?
                        .is_some()
                } else {
                    let mut present: bool = false;
                    for probe in sql::sqlite_has_table(name) {
                        present = present || !transaction.rows(&probe, &[])?.is_empty();
                    }
                    present
                };
                if !present {
                    missing.push(name);
                }
            }
            for (name, ddl) in sql::TABLES {
                if !missing.contains(&name) {
                    continue;
                }
                if self.postgres {
                    transaction.execute(&sql::postgres_ddl(ddl), &[])?;
                } else {
                    transaction.execute(ddl, &[])?;
                }
            }
            Ok(())
        })
    }

    fn roles_for_user<'py>(
        &self,
        transaction: &Transaction<'py>,
        user_id: &Any<'py>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let mut roles: Vec<Any<'py>> = Vec::new();
        for row in transaction.rows(sql::USER_ROLES, &[("user_id", user_id.str()?.into_any())])? {
            roles.push(
                row.get_item("role_name")?
                    .unwrap_or_else(|| none(transaction.py())),
            );
        }
        PyTuple::new(transaction.py(), roles)
    }

    fn account_from_row<'py>(
        &self,
        transaction: &Transaction<'py>,
        row: &Row<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = transaction.py();
        let user_id: Any<'py> = uuid(&column(row, "id")?)?;
        let key: Any<'py> = user_id.str()?.into_any();
        let roles: Bound<'py, PyTuple> = self.roles_for_user(transaction, &user_id)?;
        let source_class: Any<'py> = attr(py, AUTH_TYPES, "AuthenticationSource")?;
        let mut sources: Vec<Any<'py>> = Vec::new();
        for source in transaction.rows(sql::USER_SOURCES, &[("user_id", key.clone())])? {
            sources.push(source_class.call1((column(&source, "source")?,))?);
        }
        if transaction
            .first(sql::PASSWORD_PRESENT, &[("user_id", key)])?
            .is_some()
        {
            sources.push(source_class.getattr("PASSWORD")?);
        }
        let unique: Any<'py> = py
            .get_type::<PyDict>()
            .call_method1("fromkeys", (PyTuple::new(py, sources)?,))?;
        model(
            py,
            "UserAccount",
            py_kwargs!(py;
                user_id = user_id,
                username = column(row, "username")?.str()?,
                display_name = text_or_none(&column(row, "display_name")?)?,
                email = text_or_none(&column(row, "email")?)?,
                is_active = column(row, "is_active")?.is_truthy()?,
                created_at = as_utc(&column(row, "created_at")?)?,
                updated_at = as_utc(&column(row, "updated_at")?)?,
                roles = roles,
                authentication_sources = PyTuple::new(py, unique.try_iter()?.collect::<PyResult<Vec<Any<'py>>>>()?)?,
            )?,
        )
    }

    fn user_by<'py>(
        &self,
        py: Python<'py>,
        clause: &str,
        parameters: &[(&str, Any<'py>)],
    ) -> PyResult<Option<Any<'py>>> {
        self.transaction(py, |transaction| {
            transaction
                .first(&sql::users_where(clause), parameters)?
                .map(|row| self.account_from_row(transaction, &row))
                .transpose()
        })
    }

    fn reload<'py>(
        &self,
        py: Python<'py>,
        user_id: &Any<'py>,
        missing: impl FnOnce() -> PyErr,
    ) -> PyResult<Any<'py>> {
        self.user_by(
            py,
            " WHERE sqltide_users.id = :id",
            &[("id", user_id.str()?.into_any())],
        )?
        .ok_or_else(missing)
    }

    fn not_found(py: Python<'_>, user_id: &Any<'_>) -> PyErr {
        match user_id.str() {
            Ok(user_id) => error(
                py,
                "AccountNotFoundError",
                format!("User '{user_id}' was not found"),
            ),
            Err(failure) => failure,
        }
    }

    fn assign_role<'py>(
        &self,
        transaction: &Transaction<'py>,
        user_id: &Any<'py>,
        role_name: &Any<'py>,
        actor_user_id: &Any<'py>,
        now: &Any<'py>,
    ) -> PyResult<()> {
        let py: Python<'py> = transaction.py();
        if transaction
            .scalar(sql::ROLE_BY_NAME, &[("name", role_name.clone())])?
            .is_none()
        {
            return Err(error(
                py,
                "AccountNotFoundError",
                format!("Role '{}' was not found", role_name.str()?),
            ));
        }
        transaction.execute(
            sql::INSERT_USER_ROLE,
            &[
                ("user_id", user_id.str()?.into_any()),
                ("role_name", role_name.clone()),
                ("assigned_at", transaction.time(now)?),
                ("assigned_by", text_or_none(actor_user_id)?),
            ],
        )?;
        Ok(())
    }

    fn audit<'py>(
        &self,
        transaction: &Transaction<'py>,
        operation: &str,
        actor_user_id: &Any<'py>,
        affected_user_id: &Any<'py>,
        details: Bound<'py, PyDict>,
        now: &Any<'py>,
    ) -> PyResult<()> {
        let py: Python<'py> = transaction.py();
        let details_json: Any<'py> = attr(py, "json", "dumps")?
            .call((details,), Some(&py_kwargs!(py; sort_keys = true)?))?;
        transaction.execute(
            sql::INSERT_AUDIT,
            &[
                ("operation", object(py, operation)?),
                ("actor_user_id", text_or_none(actor_user_id)?),
                ("affected_user_id", text_or_none(affected_user_id)?),
                ("occurred_at", transaction.time(now)?),
                ("details_json", details_json),
            ],
        )?;
        Ok(())
    }

    fn is_last_admin<'py>(
        &self,
        transaction: &Transaction<'py>,
        user_id: &Any<'py>,
    ) -> PyResult<bool> {
        let py: Python<'py> = transaction.py();
        let admin: Any<'py> = object(py, ADMIN_ROLE)?;
        if transaction
            .scalar(
                &sql::lock_admin_role(self.postgres),
                &[("name", admin.clone())],
            )?
            .is_none()
        {
            return Err(error(
                py,
                "ControlStoreError",
                "The administrator role is missing".to_owned(),
            ));
        }
        let member: Option<Any<'py>> = transaction.scalar(
            sql::ADMIN_MEMBERSHIP,
            &[
                ("user_id", user_id.str()?.into_any()),
                ("role_name", admin.clone()),
            ],
        )?;
        if member.is_none() {
            return Ok(false);
        }
        let count: i64 = transaction
            .scalar(
                &sql::active_admin_count(self.postgres),
                &[("role_name", admin)],
            )?
            .map_or(Ok(0), |count| count.extract())?;
        Ok(count <= 1)
    }

    fn revoke_user_sessions<'py>(
        &self,
        transaction: &Transaction<'py>,
        user_id: &Any<'py>,
        now: &Any<'py>,
    ) -> PyResult<()> {
        transaction.execute(
            sql::REVOKE_USER_SESSIONS,
            &[
                ("revoked_at", transaction.time(now)?),
                ("user_id", user_id.str()?.into_any()),
            ],
        )?;
        Ok(())
    }

    fn create_session<'py>(
        &self,
        transaction: &Transaction<'py>,
        user_id: &Any<'py>,
        ttl_seconds: Any<'py>,
        now: &Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = transaction.py();
        let token: String = token_urlsafe(SESSION_TOKEN_BYTES)?;
        let csrf_token: String = token_urlsafe(CSRF_TOKEN_BYTES)?;
        let expires_at: Any<'py> = now.add(seconds(py, ttl_seconds)?)?;
        let stamp: Any<'py> = transaction.time(now)?;
        transaction.execute(
            sql::INSERT_SESSION,
            &[
                ("token_hash", object(py, token_hash(&object(py, &token)?)?)?),
                ("user_id", user_id.str()?.into_any()),
                ("csrf_token", object(py, &csrf_token)?),
                ("created_at", stamp.clone()),
                ("last_seen_at", stamp),
                ("expires_at", transaction.time(&expires_at)?),
                ("revoked_at", none(py)),
            ],
        )?;
        model(
            py,
            "SessionCredentials",
            py_kwargs!(py; token = token, csrf_token = csrf_token, expires_at = expires_at)?,
        )
    }

    fn dummy_password_hash<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        Ok(DUMMY_PASSWORD_HASH
            .get_or_try_init(py, || {
                hash_password(&object(py, DUMMY_PASSWORD)?).map(Bound::unbind)
            })?
            .bind(py)
            .clone())
    }
}

fn column<'py>(row: &Row<'py>, name: &str) -> PyResult<Any<'py>> {
    row.get_item(name)?
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(name.to_owned()))
}

/// `PasswordValidationError` for short passwords, else the argon2 hash.
fn hash_password<'py>(password: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = password.py();
    if password.len()? < PASSWORD_MIN_LENGTH {
        return Err(error(
            py,
            "PasswordValidationError",
            format!("Password must contain at least {PASSWORD_MIN_LENGTH} characters"),
        ));
    }
    attr(py, PASSWORDS, "hash_password")?.call1((password,))
}

fn principal<'py>(row: &Row<'py>, source: Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = row.py();
    model(
        py,
        "Principal",
        py_kwargs!(py;
            user_id = uuid(&column(row, "id")?)?,
            username = column(row, "username")?.str()?,
            display_name = text_or_none(&column(row, "display_name")?)?,
            email = text_or_none(&column(row, "email")?)?,
            authentication_source = source,
        )?,
    )
}

fn assignment_from_row<'py>(
    row: &Row<'py>,
    revoked_by: Any<'py>,
    revoked_at: Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = row.py();
    let revoked_by: Any<'py> = if revoked_by.is_none() {
        uuid_or_none(&column(row, "revoked_by")?)?
    } else {
        revoked_by
    };
    let revoked_at: Any<'py> = if revoked_at.is_none() {
        as_utc_or_none(&column(row, "revoked_at")?)?
    } else {
        revoked_at
    };
    model(
        py,
        "ProjectRoleAssignment",
        py_kwargs!(py;
            assignment_id = uuid(&column(row, "id")?)?,
            user_id = uuid(&column(row, "user_id")?)?,
            project_name = column(row, "project_name")?.str()?,
            role_name = column(row, "role_name")?.str()?,
            target_name = text_or_none(&column(row, "target_name")?)?,
            assigned_by = uuid_or_none(&column(row, "assigned_by")?)?,
            assigned_at = as_utc(&column(row, "assigned_at")?)?,
            revoked_by = revoked_by,
            revoked_at = revoked_at,
        )?,
    )
}

#[pymethods]
impl ControlStore {
    #[new]
    #[pyo3(signature = (*, url))]
    fn new(py: Python<'_>, url: Bound<'_, PyAny>) -> PyResult<Self> {
        let url_text: String = url.extract()?;
        if let Some(path) = url_text.strip_prefix(SQLITE_FILE_PREFIX)
            && path != SQLITE_MEMORY_PATH
        {
            attr(py, "pathlib", "Path")?
                .call1((path,))?
                .getattr("parent")?
                .call_method(
                    "mkdir",
                    (),
                    Some(&py_kwargs!(py; parents = true, exist_ok = true)?),
                )?;
        }
        let initialize = || -> PyResult<Self> {
            let driver: Any<'_> = driver_class(py)?.call((), Some(&py_kwargs!(py; url = &url)?))?;
            let store: Self = Self {
                url: url.clone().unbind(),
                postgres: !url_text.starts_with(SQLITE_URL_PREFIX),
                driver: driver.unbind(),
                revision: AtomicU64::new(0),
            };
            store.bootstrap(py)?;
            store.dummy_password_hash(py)?;
            Ok(store)
        };
        initialize().map_err(|failure| {
            let driver_failure: bool = driver_class(py)
                .and_then(|class| class.getattr("error_kind"))
                .and_then(|kind| kind.call1((failure.value(py),)))
                .is_ok_and(|kind| !kind.is_none());
            if !driver_failure && !failure.is_instance_of::<pyo3::exceptions::PyOSError>(py) {
                return failure;
            }
            let message: String = match failure.value(py).str() {
                Ok(text) => format!("Could not initialize SQLTide control store: {text}"),
                Err(inner) => return inner,
            };
            chained(py, error(py, "ControlStoreError", message), failure)
        })
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        self.driver.bind(py).call_method0("close")?;
        Ok(())
    }

    /// The configured URL for cooperating control-store components.
    #[getter]
    fn url<'py>(&self, py: Python<'py>) -> Any<'py> {
        self.url.bind(py).clone()
    }

    /// The process-local generation for cached identity invalidation.
    #[getter]
    fn authentication_revision(&self) -> u64 {
        self.revision.load(Ordering::SeqCst)
    }

    /// Create the current clean schema and reject incompatible state.
    fn bootstrap(&self, py: Python<'_>) -> PyResult<()> {
        self.rename_streambuild_tables(py)?;
        self.create_tables(py)?;
        self.transaction(py, |transaction| {
            let component: Any<'_> = object(py, ACCOUNT_SCHEMA_COMPONENT)?;
            match transaction.scalar(sql::SCHEMA_VERSION, &[("component", component.clone())])? {
                None => {
                    transaction.execute(
                        sql::INSERT_SCHEMA_VERSION,
                        &[("component", component), ("version", object(py, SCHEMA_VERSION)?)],
                    )?;
                }
                Some(current) if current.ne(SCHEMA_VERSION)? => {
                    return Err(error(
                        py,
                        "ControlStoreError",
                        format!(
                            "SQLTide account schema is incompatible: found version {}, expected {SCHEMA_VERSION}",
                            current.str()?
                        ),
                    ));
                }
                Some(_) => {}
            }
            let mut existing: Vec<String> = Vec::new();
            for row in transaction.rows(sql::ROLE_NAMES, &[])? {
                existing.push(column(&row, "name")?.extract()?);
            }
            for (name, description) in [
                (VIEWER_ROLE, "Authenticated project viewer"),
                (ADMIN_ROLE, "SQLTide system administrator"),
            ] {
                if !existing.iter().any(|role| role == name) {
                    transaction.execute(
                        sql::INSERT_ROLE,
                        &[
                            ("name", object(py, name)?),
                            ("description", object(py, description)?),
                            ("is_system", object(py, true)?),
                        ],
                    )?;
                }
            }
            Ok(())
        })
    }

    fn list_users<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        self.transaction(py, |transaction| {
            let accounts: Vec<Any<'py>> = transaction
                .rows(&sql::users_where(" ORDER BY sqltide_users.username"), &[])?
                .iter()
                .map(|row| self.account_from_row(transaction, row))
                .collect::<PyResult<_>>()?;
            PyTuple::new(py, accounts)
        })
    }

    #[pyo3(signature = (*, user_id))]
    fn get_user_by_id<'py>(
        &self,
        py: Python<'py>,
        user_id: Any<'py>,
    ) -> PyResult<Option<Any<'py>>> {
        self.user_by(
            py,
            " WHERE sqltide_users.id = :id",
            &[("id", user_id.str()?.into_any())],
        )
    }

    #[pyo3(signature = (*, username))]
    fn get_user_by_username<'py>(
        &self,
        py: Python<'py>,
        username: Any<'py>,
    ) -> PyResult<Option<Any<'py>>> {
        let canonical: Any<'py> = canonical_username(&username)?;
        self.user_by(
            py,
            " WHERE sqltide_users.username = :username",
            &[("username", canonical)],
        )
    }

    #[pyo3(signature = (
        *,
        username,
        display_name=None,
        email=None,
        password=None,
        authentication_source=None,
        external_subject=None,
        roles=None,
        actor_user_id=None,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn create_user<'py>(
        &self,
        py: Python<'py>,
        username: Any<'py>,
        display_name: Option<Any<'py>>,
        email: Option<Any<'py>>,
        password: Option<Any<'py>>,
        authentication_source: Option<Any<'py>>,
        external_subject: Option<Any<'py>>,
        roles: Option<Any<'py>>,
        actor_user_id: Option<Any<'py>>,
    ) -> PyResult<Any<'py>> {
        let canonical: Any<'py> = canonical_username(&username)?;
        let now: Any<'py> = now(py)?;
        let user_id: Any<'py> = attr(py, "uuid", "uuid4")?.call0()?;
        let key: Any<'py> = user_id.str()?.into_any();
        let actor: Any<'py> = actor_user_id.unwrap_or_else(|| none(py));
        let roles: Any<'py> = match roles {
            Some(roles) => roles,
            None => PyTuple::new(py, [VIEWER_ROLE])?.into_any(),
        };
        let created = self.transaction(py, |transaction| {
            let stamp: Any<'py> = transaction.time(&now)?;
            transaction.execute(
                sql::INSERT_USER,
                &[
                    ("id", key.clone()),
                    ("username", canonical.clone()),
                    (
                        "display_name",
                        clean_optional(&display_name.clone().unwrap_or_else(|| none(py)))?,
                    ),
                    (
                        "email",
                        clean_optional(&email.clone().unwrap_or_else(|| none(py)))?,
                    ),
                    ("is_active", object(py, true)?),
                    ("created_at", stamp.clone()),
                    ("updated_at", stamp.clone()),
                ],
            )?;
            if let Some(source) = authentication_source
                .as_ref()
                .filter(|source| !source.is_none())
            {
                if source.eq(attr(py, AUTH_TYPES, "AuthenticationSource")?.getattr("LOCAL")?)? {
                    return Err(error(
                        py,
                        "AccountValidationError",
                        "Local principals are not persisted as external identities".to_owned(),
                    ));
                }
                let chosen: Any<'py> = match external_subject.as_ref() {
                    Some(subject) if subject.is_truthy()? => subject.clone(),
                    _ => canonical.clone(),
                };
                let subject: Any<'py> = chosen.call_method0("strip")?;
                if !subject.is_truthy()? {
                    return Err(error(
                        py,
                        "AccountValidationError",
                        "External identity subject must not be blank".to_owned(),
                    ));
                }
                transaction.execute(
                    sql::INSERT_EXTERNAL_IDENTITY,
                    &[
                        ("source", source.clone()),
                        ("subject", subject),
                        ("user_id", key.clone()),
                        ("created_at", stamp.clone()),
                    ],
                )?;
            }
            if let Some(password) = password.as_ref().filter(|password| !password.is_none()) {
                transaction.execute(
                    sql::INSERT_PASSWORD,
                    &[
                        ("user_id", key.clone()),
                        ("password_hash", hash_password(password)?),
                        ("password_changed_at", stamp.clone()),
                    ],
                )?;
            }
            for role in roles.try_iter()? {
                self.assign_role(transaction, &user_id, &role?, &actor, &now)?;
            }
            self.audit(
                transaction,
                "user.created",
                &actor,
                &user_id,
                py_kwargs!(py; username = &canonical)?,
                &now,
            )
        });
        if let Err(failure) = created {
            if is_integrity(py, &failure)? {
                let conflict: PyErr = error(
                    py,
                    "AccountConflictError",
                    format!(
                        "User or external identity '{}' already exists",
                        canonical.str()?
                    ),
                );
                return Err(chained(py, conflict, failure));
            }
            return Err(failure);
        }
        self.invalidate();
        self.reload(py, &user_id, || {
            error(
                py,
                "ControlStoreError",
                "Created account could not be reloaded".to_owned(),
            )
        })
    }

    #[pyo3(signature = (*, source, subject))]
    fn resolve_external_identity<'py>(
        &self,
        py: Python<'py>,
        source: Any<'py>,
        subject: Any<'py>,
    ) -> PyResult<Option<Any<'py>>> {
        self.user_by(
            py,
            " JOIN sqltide_external_identities ON sqltide_external_identities.user_id = sqltide_users.id WHERE sqltide_external_identities.source = :source AND sqltide_external_identities.subject = :subject",
            &[("source", source), ("subject", subject)],
        )
    }

    #[pyo3(signature = (*, subject, username, display_name, email, default_role))]
    fn provision_proxy_user<'py>(
        &self,
        py: Python<'py>,
        subject: Any<'py>,
        username: Any<'py>,
        display_name: Any<'py>,
        email: Any<'py>,
        default_role: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let proxy: Any<'py> =
            attr(py, AUTH_TYPES, "AuthenticationSource")?.getattr("TRUSTED_PROXY")?;
        if let Some(existing) =
            self.resolve_external_identity(py, proxy.clone(), subject.clone())?
        {
            return Ok(existing);
        }
        if self.get_user_by_username(py, username.clone())?.is_some() {
            return self.provisioned_by_competing_writer(py, &subject);
        }
        match self.create_user(
            py,
            username,
            Some(display_name),
            Some(email),
            None,
            Some(proxy),
            Some(subject.clone()),
            Some(PyTuple::new(py, [default_role])?.into_any()),
            None,
        ) {
            Ok(account) => Ok(account),
            Err(failure)
                if failure.is_instance(py, &attr(py, AUTH_EXCEPTIONS, "AccountConflictError")?) =>
            {
                self.provisioned_by_competing_writer(py, &subject)
            }
            Err(failure) => Err(failure),
        }
    }

    #[pyo3(signature = (*, username, password, session_ttl_seconds))]
    fn authenticate_password<'py>(
        &self,
        py: Python<'py>,
        username: Any<'py>,
        password: Any<'py>,
        session_ttl_seconds: Any<'py>,
    ) -> PyResult<Option<Bound<'py, PyTuple>>> {
        let canonical: Any<'py> = match canonical_username(&username) {
            Ok(canonical) => canonical,
            Err(failure) if failure.is_instance_of::<PyValueError>(py) => return Ok(None),
            Err(failure) => return Err(failure),
        };
        let now: Any<'py> = now(py)?;
        self.transaction(py, |transaction| {
            let row: Option<Row<'py>> = transaction.first(
                &format!(
                    "SELECT {}, sqltide_password_credentials.password_hash FROM sqltide_users JOIN sqltide_password_credentials ON sqltide_password_credentials.user_id = sqltide_users.id WHERE sqltide_users.username = :username",
                    sql::USER_COLUMNS
                ),
                &[("username", canonical.clone())],
            )?;
            let candidate: Any<'py> = match &row {
                Some(row) => column(row, "password_hash")?.str()?.into_any(),
                None => self.dummy_password_hash(py)?,
            };
            let verified: Bound<'py, PyTuple> = attr(py, PASSWORDS, "verify_password")?
                .call((), Some(&py_kwargs!(py; password_hash = candidate, password = &password)?))?
                .cast_into()?;
            let valid: bool = verified.get_item(0)?.is_truthy()?;
            let Some(row) = row else {
                return Ok(None);
            };
            if !column(&row, "is_active")?.is_truthy()? || !valid {
                return Ok(None);
            }
            let replacement: Any<'py> = verified.get_item(1)?;
            let user_key: Any<'py> = column(&row, "id")?;
            if !replacement.is_none() {
                transaction.execute(
                    sql::UPDATE_PASSWORD,
                    &[
                        ("password_hash", replacement),
                        ("password_changed_at", transaction.time(&now)?),
                        ("user_id", user_key.clone()),
                    ],
                )?;
            }
            let user_id: Any<'py> = uuid(&user_key)?;
            let credentials: Any<'py> = self.create_session(transaction, &user_id, session_ttl_seconds.clone(), &now)?;
            self.audit(transaction, "session.created", &user_id, &user_id, PyDict::new(py), &now)?;
            let account: Any<'py> = self.account_from_row(transaction, &row)?;
            PyTuple::new(py, [account, credentials]).map(Some)
        })
    }

    #[pyo3(signature = (*, token))]
    fn resolve_session<'py>(&self, py: Python<'py>, token: Any<'py>) -> PyResult<Option<Any<'py>>> {
        if !token.is_truthy()? {
            return Ok(None);
        }
        let hashed: Any<'py> = object(py, token_hash(&token)?)?;
        let now: Any<'py> = now(py)?;
        self.transaction(py, |transaction| {
            let Some(row) = transaction.first(sql::SESSION_LOOKUP, &[("token_hash", hashed.clone())])? else {
                return Ok(None);
            };
            if !column(&row, "revoked_at")?.is_none() || !column(&row, "is_active")?.is_truthy()? {
                return Ok(None);
            }
            let expires_at: Any<'py> = as_utc(&column(&row, "expires_at")?)?;
            if expires_at.le(&now)? {
                return Ok(None);
            }
            let idle: Any<'py> = now.sub(as_utc(&column(&row, "last_seen_at")?)?)?;
            if idle.ge(seconds(py, object(py, LAST_SEEN_WRITE_INTERVAL_SECONDS)?)?)? {
                transaction.execute(
                    sql::TOUCH_SESSION,
                    &[("last_seen_at", transaction.time(&now)?), ("token_hash", hashed.clone())],
                )?;
            }
            let user_id: Any<'py> = uuid(&column(&row, "id")?)?;
            model(
                py,
                "ResolvedSession",
                py_kwargs!(py;
                    principal = principal(&row, attr(py, AUTH_TYPES, "AuthenticationSource")?.getattr("PASSWORD")?)?,
                    roles = self.roles_for_user(transaction, &user_id)?,
                    csrf_token = column(&row, "csrf_token")?.str()?,
                    expires_at = expires_at,
                )?,
            )
            .map(Some)
        })
    }

    #[pyo3(signature = (*, token, actor_user_id=None))]
    fn revoke_session<'py>(
        &self,
        py: Python<'py>,
        token: Any<'py>,
        actor_user_id: Option<Any<'py>>,
    ) -> PyResult<()> {
        let now: Any<'py> = now(py)?;
        let actor: Any<'py> = actor_user_id.unwrap_or_else(|| none(py));
        let revoked: bool = self.transaction(py, |transaction| {
            let hashed: Any<'py> = object(py, token_hash(&token)?)?;
            let Some(user_key) = transaction
                .scalar(sql::SESSION_USER, &[("token_hash", hashed.clone())])?
                .filter(|value| !value.is_none())
            else {
                return Ok(false);
            };
            transaction.execute(
                sql::REVOKE_SESSION,
                &[
                    ("revoked_at", transaction.time(&now)?),
                    ("token_hash", object(py, token_hash(&token)?)?),
                ],
            )?;
            self.audit(
                transaction,
                "session.revoked",
                &actor,
                &uuid(&user_key)?,
                PyDict::new(py),
                &now,
            )?;
            Ok(true)
        })?;
        if revoked {
            self.invalidate();
        }
        Ok(())
    }

    #[pyo3(signature = (*, user_id, is_active, actor_user_id))]
    fn set_user_active<'py>(
        &self,
        py: Python<'py>,
        user_id: Any<'py>,
        is_active: Any<'py>,
        actor_user_id: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let now: Any<'py> = now(py)?;
        let active: bool = is_active.is_truthy()?;
        self.transaction(py, |transaction| {
            if !active && self.is_last_admin(transaction, &user_id)? {
                return Err(error(
                    py,
                    "AccountConflictError",
                    "Cannot disable the last active administrator".to_owned(),
                ));
            }
            let affected: i64 = transaction.execute(
                sql::SET_USER_ACTIVE,
                &[
                    ("is_active", is_active.clone()),
                    ("updated_at", transaction.time(&now)?),
                    ("id", user_id.str()?.into_any()),
                ],
            )?;
            if affected != 1 {
                return Err(Self::not_found(py, &user_id));
            }
            if !active {
                self.revoke_user_sessions(transaction, &user_id, &now)?;
            }
            let operation: &str = if active {
                "user.enabled"
            } else {
                "user.disabled"
            };
            self.audit(
                transaction,
                operation,
                &actor_user_id,
                &user_id,
                PyDict::new(py),
                &now,
            )
        })?;
        self.invalidate();
        self.reload(py, &user_id, || Self::not_found(py, &user_id))
    }

    #[pyo3(signature = (*, user_id, display_name, email, actor_user_id))]
    fn update_profile<'py>(
        &self,
        py: Python<'py>,
        user_id: Any<'py>,
        display_name: Any<'py>,
        email: Any<'py>,
        actor_user_id: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let now: Any<'py> = now(py)?;
        self.transaction(py, |transaction| {
            let affected: i64 = transaction.execute(
                sql::SET_USER_PROFILE,
                &[
                    ("display_name", clean_optional(&display_name)?),
                    ("email", clean_optional(&email)?),
                    ("updated_at", transaction.time(&now)?),
                    ("id", user_id.str()?.into_any()),
                ],
            )?;
            if affected != 1 {
                return Err(Self::not_found(py, &user_id));
            }
            self.audit(
                transaction,
                "user.profile_updated",
                &actor_user_id,
                &user_id,
                PyDict::new(py),
                &now,
            )
        })?;
        self.invalidate();
        self.reload(py, &user_id, || Self::not_found(py, &user_id))
    }

    /// Apply one complete account edit atomically, including session revocation.
    #[pyo3(signature = (*, user_id, display_name, email, is_active, actor_user_id))]
    fn update_account<'py>(
        &self,
        py: Python<'py>,
        user_id: Any<'py>,
        display_name: Any<'py>,
        email: Any<'py>,
        is_active: Any<'py>,
        actor_user_id: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let now: Any<'py> = now(py)?;
        let active: bool = is_active.is_truthy()?;
        self.transaction(py, |transaction| {
            let Some(current) = transaction.first(
                &sql::users_where(" WHERE sqltide_users.id = :id"),
                &[("id", user_id.str()?.into_any())],
            )?
            else {
                return Err(Self::not_found(py, &user_id));
            };
            if !active
                && column(&current, "is_active")?.is_truthy()?
                && self.is_last_admin(transaction, &user_id)?
            {
                return Err(error(
                    py,
                    "AccountConflictError",
                    "Cannot disable the last active administrator".to_owned(),
                ));
            }
            transaction.execute(
                sql::SET_USER_ACCOUNT,
                &[
                    ("display_name", clean_optional(&display_name)?),
                    ("email", clean_optional(&email)?),
                    ("is_active", is_active.clone()),
                    ("updated_at", transaction.time(&now)?),
                    ("id", user_id.str()?.into_any()),
                ],
            )?;
            if !active {
                self.revoke_user_sessions(transaction, &user_id, &now)?;
            }
            self.audit(
                transaction,
                "user.updated",
                &actor_user_id,
                &user_id,
                py_kwargs!(py; active = &is_active)?,
                &now,
            )
        })?;
        self.invalidate();
        self.reload(py, &user_id, || Self::not_found(py, &user_id))
    }

    #[pyo3(signature = (*, user_id, password, actor_user_id))]
    fn reset_password<'py>(
        &self,
        py: Python<'py>,
        user_id: Any<'py>,
        password: Any<'py>,
        actor_user_id: Any<'py>,
    ) -> PyResult<()> {
        let password_hash: Any<'py> = hash_password(&password)?;
        let now: Any<'py> = now(py)?;
        self.transaction(py, |transaction| {
            let key: Any<'py> = user_id.str()?.into_any();
            if transaction
                .scalar(sql::USER_ID, &[("id", key.clone())])?
                .is_none()
            {
                return Err(Self::not_found(py, &user_id));
            }
            let stamp: Any<'py> = transaction.time(&now)?;
            let statement: &str = if transaction
                .scalar(sql::PASSWORD_PRESENT, &[("user_id", key.clone())])?
                .is_none()
            {
                sql::INSERT_PASSWORD
            } else {
                sql::UPDATE_PASSWORD
            };
            transaction.execute(
                statement,
                &[
                    ("user_id", key),
                    ("password_hash", password_hash.clone()),
                    ("password_changed_at", stamp),
                ],
            )?;
            self.revoke_user_sessions(transaction, &user_id, &now)?;
            self.audit(
                transaction,
                "password.reset",
                &actor_user_id,
                &user_id,
                PyDict::new(py),
                &now,
            )
        })?;
        self.invalidate();
        Ok(())
    }

    #[pyo3(signature = (*, user_id, role_name, actor_user_id))]
    fn grant_role<'py>(
        &self,
        py: Python<'py>,
        user_id: Any<'py>,
        role_name: Any<'py>,
        actor_user_id: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let now: Any<'py> = now(py)?;
        let granted = self.transaction(py, |transaction| {
            self.assign_role(transaction, &user_id, &role_name, &actor_user_id, &now)?;
            self.audit(
                transaction,
                "role.granted",
                &actor_user_id,
                &user_id,
                py_kwargs!(py; role = &role_name)?,
                &now,
            )
        });
        if let Err(failure) = granted {
            if is_integrity(py, &failure)? {
                let conflict: PyErr = error(
                    py,
                    "AccountConflictError",
                    format!(
                        "Role '{}' is already assigned or does not exist",
                        role_name.str()?
                    ),
                );
                return Err(chained(py, conflict, failure));
            }
            return Err(failure);
        }
        self.invalidate();
        self.reload(py, &user_id, || Self::not_found(py, &user_id))
    }

    #[pyo3(signature = (*, user_id, role_name, actor_user_id))]
    fn revoke_role<'py>(
        &self,
        py: Python<'py>,
        user_id: Any<'py>,
        role_name: Any<'py>,
        actor_user_id: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let now: Any<'py> = now(py)?;
        self.transaction(py, |transaction| {
            if role_name.eq(ADMIN_ROLE)? && self.is_last_admin(transaction, &user_id)? {
                return Err(error(
                    py,
                    "AccountConflictError",
                    "Cannot remove the last active administrator".to_owned(),
                ));
            }
            let affected: i64 = transaction.execute(
                sql::DELETE_USER_ROLE,
                &[
                    ("user_id", user_id.str()?.into_any()),
                    ("role_name", role_name.clone()),
                ],
            )?;
            if affected != 1 {
                return Err(error(
                    py,
                    "AccountNotFoundError",
                    format!("Role '{}' is not assigned to user", role_name.str()?),
                ));
            }
            self.audit(
                transaction,
                "role.revoked",
                &actor_user_id,
                &user_id,
                py_kwargs!(py; role = &role_name)?,
                &now,
            )
        })?;
        self.invalidate();
        self.reload(py, &user_id, || Self::not_found(py, &user_id))
    }

    /// Grant one project-authored role at all targets or one explicit target.
    #[pyo3(signature = (*, user_id, project_name, role_name, target_name, actor_user_id))]
    fn grant_project_role<'py>(
        &self,
        py: Python<'py>,
        user_id: Any<'py>,
        project_name: Any<'py>,
        role_name: Any<'py>,
        target_name: Any<'py>,
        actor_user_id: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let clean_project: Any<'py> = required_assignment_name(&project_name, "Project name", 256)?;
        let clean_role: Any<'py> = required_assignment_name(&role_name, "Role name", 128)?;
        let clean_target: Any<'py> = if target_name.is_none() {
            target_name
        } else {
            required_assignment_name(&target_name, "Target name", 128)?
        };
        let assignment_id: Any<'py> = attr(py, "uuid", "uuid4")?.call0()?;
        let now: Any<'py> = now(py)?;
        let granted = self.transaction(py, |transaction| {
            let scope: Any<'py> = if clean_target.is_truthy()? {
                clean_target.clone()
            } else {
                object(py, "")?
            };
            transaction.execute(
                sql::INSERT_ASSIGNMENT,
                &[
                    ("id", assignment_id.str()?.into_any()),
                    ("user_id", user_id.str()?.into_any()),
                    ("project_name", clean_project.clone()),
                    ("role_name", clean_role.clone()),
                    ("target_name", clean_target.clone()),
                    ("assignment_scope", scope),
                    ("assigned_by", text_or_none(&actor_user_id)?),
                    ("assigned_at", transaction.time(&now)?),
                    ("revoked_by", none(py)),
                    ("revoked_at", none(py)),
                    ("active_key", object(py, "active")?),
                ],
            )?;
            let details: Bound<'py, PyDict> = PyDict::new(py);
            details.set_item("projectName", &clean_project)?;
            details.set_item("role", &clean_role)?;
            details.set_item("targetName", &clean_target)?;
            self.audit(
                transaction,
                "project_role.granted",
                &actor_user_id,
                &user_id,
                details,
                &now,
            )
        });
        if let Err(failure) = granted {
            if is_integrity(py, &failure)? {
                let conflict: PyErr = error(
                    py,
                    "AccountConflictError",
                    format!(
                        "Role '{}' is already assigned for the requested project scope or the user does not exist",
                        clean_role.str()?
                    ),
                );
                return Err(chained(py, conflict, failure));
            }
            return Err(failure);
        }
        model(
            py,
            "ProjectRoleAssignment",
            py_kwargs!(py;
                assignment_id = assignment_id,
                user_id = user_id,
                project_name = clean_project,
                role_name = clean_role,
                target_name = clean_target,
                assigned_by = actor_user_id,
                assigned_at = now,
            )?,
        )
    }

    /// Soft-revoke one exact project-role assignment and retain its history.
    #[pyo3(signature = (*, assignment_id, actor_user_id))]
    fn revoke_project_role<'py>(
        &self,
        py: Python<'py>,
        assignment_id: Any<'py>,
        actor_user_id: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let now: Any<'py> = now(py)?;
        let key: Any<'py> = assignment_id.str()?.into_any();
        let row: Row<'py> = self.transaction(py, |transaction| {
            let Some(row) = transaction.first(
                &sql::assignments_where(" WHERE sqltide_project_role_assignments.id = :id AND sqltide_project_role_assignments.revoked_at IS NULL"),
                &[("id", key.clone())],
            )?
            else {
                return Err(error(
                    py,
                    "AccountNotFoundError",
                    format!("Project role assignment '{}' was not found", assignment_id.str()?),
                ));
            };
            transaction.execute(
                sql::REVOKE_ASSIGNMENT,
                &[
                    ("revoked_by", text_or_none(&actor_user_id)?),
                    ("revoked_at", transaction.time(&now)?),
                    ("active_key", key.clone()),
                    ("id", key.clone()),
                ],
            )?;
            let details: Bound<'py, PyDict> = PyDict::new(py);
            details.set_item("projectName", column(&row, "project_name")?.str()?)?;
            details.set_item("role", column(&row, "role_name")?.str()?)?;
            details.set_item("targetName", column(&row, "target_name")?)?;
            self.audit(transaction, "project_role.revoked", &actor_user_id, &uuid(&column(&row, "user_id")?)?, details, &now)?;
            Ok(row)
        })?;
        assignment_from_row(&row, actor_user_id, now)
    }

    /// List deterministic project memberships for one user.
    #[pyo3(signature = (*, user_id, project_name, include_revoked=false))]
    fn list_project_role_assignments<'py>(
        &self,
        py: Python<'py>,
        user_id: Any<'py>,
        project_name: Any<'py>,
        include_revoked: bool,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let active: &str = if include_revoked {
            ""
        } else {
            " AND sqltide_project_role_assignments.revoked_at IS NULL"
        };
        let statement: String = sql::assignments_where(&format!(
            " WHERE sqltide_project_role_assignments.user_id = :user_id AND sqltide_project_role_assignments.project_name = :project_name{active} ORDER BY sqltide_project_role_assignments.role_name, sqltide_project_role_assignments.assignment_scope, sqltide_project_role_assignments.assigned_at"
        ));
        let rows: Vec<Row<'py>> = self.transaction(py, |transaction| {
            transaction.rows(
                &statement,
                &[
                    ("user_id", user_id.str()?.into_any()),
                    ("project_name", project_name.clone()),
                ],
            )
        })?;
        let assignments: Vec<Any<'py>> = rows
            .iter()
            .map(|row| assignment_from_row(row, none(py), none(py)))
            .collect::<PyResult<_>>()?;
        PyTuple::new(py, assignments)
    }

    fn list_audit_records<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        self.transaction(py, |transaction| {
            let loads: Any<'py> = attr(py, "json", "loads")?;
            let mut records: Vec<Any<'py>> = Vec::new();
            for row in transaction.rows(sql::AUDIT_RECORDS, &[])? {
                records.push(model(
                    py,
                    "AccountAuditRecord",
                    py_kwargs!(py;
                        operation = column(&row, "operation")?.str()?,
                        actor_user_id = uuid_or_none(&column(&row, "actor_user_id")?)?,
                        affected_user_id = uuid_or_none(&column(&row, "affected_user_id")?)?,
                        occurred_at = as_utc(&column(&row, "occurred_at")?)?,
                        details = loads.call1((column(&row, "details_json")?.str()?,))?,
                    )?,
                )?);
            }
            PyTuple::new(py, records)
        })
    }

    fn prune_sessions(&self, py: Python<'_>) -> PyResult<i64> {
        let now: Any<'_> = now(py)?;
        self.transaction(py, |transaction| {
            transaction.execute(sql::PRUNE_SESSIONS, &[("now", transaction.time(&now)?)])
        })
    }
}

impl ControlStore {
    /// The identity a competing writer linked, or a real conflict.
    fn provisioned_by_competing_writer<'py>(
        &self,
        py: Python<'py>,
        subject: &Any<'py>,
    ) -> PyResult<Any<'py>> {
        let proxy: Any<'py> =
            attr(py, AUTH_TYPES, "AuthenticationSource")?.getattr("TRUSTED_PROXY")?;
        self.resolve_external_identity(py, proxy, subject.clone())?
            .ok_or_else(|| match subject.str() {
                Ok(subject) => error(
                    py,
                    "AccountConflictError",
                    format!(
                        "Proxy identity '{subject}' conflicts with an existing unlinked username"
                    ),
                ),
                Err(failure) => failure,
            })
    }
}

/// The project-local SQLite URL, moving pre-rename `.streambuild/` state first.
#[pyfunction]
#[pyo3(signature = (*, project_dir))]
pub(crate) fn default_control_store_url(project_dir: &Bound<'_, PyAny>) -> PyResult<String> {
    let state_dir: Any<'_> = project_dir.div(".sqltide")?.call_method0("resolve")?;
    let streambuild_state_dir: Any<'_> =
        project_dir.div(".streambuild")?.call_method0("resolve")?;
    if !state_dir.call_method0("exists")?.is_truthy()?
        && streambuild_state_dir.call_method0("is_dir")?.is_truthy()?
    {
        streambuild_state_dir.call_method1("rename", (&state_dir,))?;
    }
    Ok(format!("sqlite:///{}", state_dir.div("control.db")?.str()?))
}

/// Rename every pre-rename table at `url` whose SQLTide name is still free, waiting up to
/// `sqlite_timeout_seconds` for a SQLite lock as the caller's own engine would.
#[pyfunction]
#[pyo3(signature = (*, url, sqlite_timeout_seconds = None))]
fn rename_streambuild_tables(
    py: Python<'_>,
    url: Bound<'_, PyAny>,
    sqlite_timeout_seconds: Option<f64>,
) -> PyResult<()> {
    let url_text: String = url.extract()?;
    let driver: Any<'_> = driver_class(py)?.call(
        (),
        Some(&py_kwargs!(py; url = &url, sqlite_timeout_seconds = sqlite_timeout_seconds)?),
    )?;
    let store: ControlStore = ControlStore {
        url: url.unbind(),
        postgres: !url_text.starts_with(SQLITE_URL_PREFIX),
        driver: driver.clone().unbind(),
        revision: AtomicU64::new(0),
    };
    let renamed: PyResult<()> = store.rename_streambuild_tables(py);
    driver.call_method0("close")?;
    renamed
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<ControlStore>()?;
    module.add_function(wrap_pyfunction!(canonical_username_py, module)?)?;
    module.add_function(wrap_pyfunction!(default_control_store_url, module)?)?;
    module.add_function(wrap_pyfunction!(rename_streambuild_tables, module)?)
}

#[cfg(test)]
mod tests {
    use super::urlsafe_base64;

    #[test]
    fn urlsafe_base64_matches_rfc_4648_without_padding() {
        let cases: [(&[u8], &str); 8] = [
            (b"", ""),
            (b"f", "Zg"),
            (b"fo", "Zm8"),
            (b"foo", "Zm9v"),
            (b"foob", "Zm9vYg"),
            (b"fooba", "Zm9vYmE"),
            (b"foobar", "Zm9vYmFy"),
            (&[0xfb, 0xff, 0xbf, 0x00, 0x10], "-_-_ABA"),
        ];
        for (bytes, expected) in cases {
            assert_eq!(urlsafe_base64(bytes), expected);
        }
    }
}
