//! Durable relational storage for actor-bound destruction plans: SQLite or PostgreSQL through
//! the relational driver, with review and single-use consumption recorded atomically.

use pyo3::exceptions::PyOSError;
use pyo3::prelude::*;
use pyo3::types::{PyDateTime, PyDict, PyString, PyTuple};
use sqltide_planner::direct::sha256_hex;

use crate::control_store::sql;
use crate::dev_server::support::attr;
use crate::handling::while_handling;
use crate::py_kwargs;

const DRIVER: &str = "sqltide.auth.classes.relational_driver";
const EXCEPTIONS: &str = "sqltide.executor.destruction.exceptions";
const SCHEMA_COMPONENT: &str = "destruction_plans";
const SCHEMA_VERSION: i64 = 1;
const PENDING: &str = "pending";
const REVIEWED: &str = "reviewed";
const CONSUMED: &str = "consumed";
const EXPIRED: &str = "expired";
const SQLITE_FILE_PREFIX: &str = "sqlite:///";
const SQLITE_TIMEOUT_SECONDS: f64 = 30.0;

/// This store's tables in creation order, with their SQLite DDL.
const TABLES: [(&str, &str); 2] = [
    (
        "sqltide_schema_versions",
        "\nCREATE TABLE sqltide_schema_versions (\n\tcomponent VARCHAR(64) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tPRIMARY KEY (component)\n)\n\n",
    ),
    (
        "sqltide_destruction_plans",
        "\nCREATE TABLE sqltide_destruction_plans (\n\tplan_id VARCHAR(128) NOT NULL, \n\tactor VARCHAR(128) NOT NULL, \n\tpayload_version INTEGER NOT NULL, \n\tpayload_json TEXT NOT NULL, \n\tpayload_sha256 VARCHAR(64) NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\texpires_at DATETIME NOT NULL, \n\treviewed_at DATETIME, \n\tconsumed_at DATETIME, \n\tstatus VARCHAR(16) NOT NULL, \n\tPRIMARY KEY (plan_id)\n)\n\n",
    ),
];
const SELECT_SCHEMA_VERSION: &str = "SELECT sqltide_schema_versions.version \nFROM sqltide_schema_versions \nWHERE sqltide_schema_versions.component = :component_1";
const INSERT_PLAN: &str = "INSERT INTO sqltide_destruction_plans (plan_id, actor, payload_version, payload_json, payload_sha256, created_at, expires_at, reviewed_at, consumed_at, status) VALUES (:plan_id, :actor, :payload_version, :payload_json, :payload_sha256, :created_at, :expires_at, :reviewed_at, :consumed_at, :status)";
const MARK_REVIEWED: &str = "UPDATE sqltide_destruction_plans SET reviewed_at=:reviewed_at, status=:status WHERE sqltide_destruction_plans.plan_id = :plan_id_1 AND sqltide_destruction_plans.actor = :actor_1 AND sqltide_destruction_plans.status IN (:status_1_1, :status_1_2) AND sqltide_destruction_plans.expires_at > :expires_at_1 AND sqltide_destruction_plans.payload_json = :payload_json_1 AND sqltide_destruction_plans.payload_sha256 = :payload_sha256_1";
const CONSUME: &str = "UPDATE sqltide_destruction_plans SET consumed_at=:consumed_at, status=:status WHERE sqltide_destruction_plans.plan_id = :plan_id_1 AND sqltide_destruction_plans.actor = :actor_1 AND sqltide_destruction_plans.status = :status_1 AND sqltide_destruction_plans.reviewed_at IS NOT NULL AND sqltide_destruction_plans.consumed_at IS NULL AND sqltide_destruction_plans.expires_at > :expires_at_1 AND sqltide_destruction_plans.payload_json = :payload_json_1 AND sqltide_destruction_plans.payload_sha256 = :payload_sha256_1";
const READ_PLAN: &str = "SELECT sqltide_destruction_plans.plan_id, sqltide_destruction_plans.actor, sqltide_destruction_plans.payload_version, sqltide_destruction_plans.payload_json, sqltide_destruction_plans.payload_sha256, sqltide_destruction_plans.created_at, sqltide_destruction_plans.expires_at, sqltide_destruction_plans.reviewed_at, sqltide_destruction_plans.consumed_at, sqltide_destruction_plans.status \nFROM sqltide_destruction_plans \nWHERE sqltide_destruction_plans.plan_id = :plan_id_1 AND sqltide_destruction_plans.actor = :actor_1";
const MARK_EXPIRED: &str = "UPDATE sqltide_destruction_plans SET status=:status WHERE sqltide_destruction_plans.plan_id = :plan_id_1 AND sqltide_destruction_plans.actor = :actor_1 AND sqltide_destruction_plans.status IN (:status_1_1, :status_1_2) AND sqltide_destruction_plans.expires_at <= :expires_at_1";

type Any<'py> = Bound<'py, PyAny>;
type Row<'py> = Bound<'py, PyDict>;

fn error(py: Python<'_>, class: &str, message: String) -> PyErr {
    match attr(py, EXCEPTIONS, class).and_then(|class| class.call1((message,))) {
        Ok(error) => PyErr::from_value(error),
        Err(failure) => failure,
    }
}

/// `error` raised `from cause`.
fn chained(py: Python<'_>, error: PyErr, cause: &PyErr) -> PyErr {
    let value: Any<'_> = error.value(py).clone().into_any();
    let _ = value.setattr("__cause__", cause.value(py));
    let _ = value.setattr("__context__", cause.value(py));
    error
}

fn driver_class(py: Python<'_>) -> PyResult<Any<'_>> {
    attr(py, DRIVER, "RelationalDriver")
}

fn error_kind(py: Python<'_>, failure: &PyErr) -> PyResult<Option<String>> {
    driver_class(py)?
        .getattr("error_kind")?
        .call1((failure.value(py),))?
        .extract()
}

fn object<'py, T: IntoPyObject<'py>>(py: Python<'py>, value: T) -> PyResult<Any<'py>> {
    pyo3::IntoPyObjectExt::into_bound_py_any(value, py)
}

fn utc(py: Python<'_>) -> PyResult<Any<'_>> {
    attr(py, "datetime", "UTC")
}

/// A stored timestamp as an aware UTC datetime; SQLite hands back the stored text.
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
            "DestructionPlanCorruptError",
            format!(
                "Expected destruction plan timestamp, received {}",
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

fn optional_utc<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    if value.is_none() {
        Ok(value.clone())
    } else {
        as_utc(value)
    }
}

fn column<'py>(row: &Row<'py>, name: &str) -> PyResult<Any<'py>> {
    row.get_item(name)?
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(name.to_owned()))
}

fn schema_version_error(py: Python<'_>, version: &Any<'_>) -> PyResult<()> {
    if version.ne(SCHEMA_VERSION)? {
        return Err(error(
            py,
            "DestructionValidationError",
            format!(
                "SQLTide destruction plan schema is incompatible: found version {}, expected {SCHEMA_VERSION}",
                version.str()?
            ),
        ));
    }
    Ok(())
}

/// One driver connection with its transaction open.
struct Transaction<'py> {
    driver: Any<'py>,
    connection: Any<'py>,
    postgres: bool,
}

impl<'py> Transaction<'py> {
    fn run(
        &self,
        statement: &str,
        parameters: &[(&str, Any<'py>)],
    ) -> PyResult<(Vec<Row<'py>>, Any<'py>)> {
        let py: Python<'py> = self.driver.py();
        let bound: Bound<'py, PyDict> = PyDict::new(py);
        for (name, value) in parameters {
            bound.set_item(name, value)?;
        }
        let result: Bound<'py, PyTuple> = self
            .driver
            .call_method(
                "execute",
                (),
                Some(&py_kwargs!(py; connection = &self.connection, sql = statement, parameters = bound)?),
            )?
            .cast_into()?;
        let rows: Vec<Row<'py>> = result
            .get_item(0)?
            .try_iter()?
            .map(|row| row?.cast_into::<PyDict>().map_err(Into::into))
            .collect::<PyResult<_>>()?;
        Ok((rows, result.get_item(1)?))
    }

    /// A timestamp as this dialect binds it: SQLite stores SQLAlchemy's text form.
    fn time(&self, moment: &Any<'py>) -> PyResult<Any<'py>> {
        if self.postgres || moment.is_none() {
            return Ok(moment.clone());
        }
        let field = |name: &str| -> PyResult<i64> { moment.getattr(name)?.extract() };
        Ok(PyString::new(
            moment.py(),
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

/// A stored plan row, validated against its payload.
struct Stored<'py> {
    plan: Any<'py>,
    payload_json: Any<'py>,
    payload_sha256: Any<'py>,
    status: String,
    reviewed_at: Any<'py>,
}

/// SQLite/PostgreSQL store with durable review and atomic consumption state. Instances keep a
/// `__dict__` so callers can wrap one method of one store.
#[pyclass(module = "sqltide._native", frozen, dict)]
pub(crate) struct RelationalDestructionPlanStore {
    url: Py<PyAny>,
    driver: Py<PyAny>,
    clock: Option<Py<PyAny>>,
    postgres: bool,
}

impl RelationalDestructionPlanStore {
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

    /// Run a read and discard its transaction, as an autobegun connection closes.
    fn read_only<'py, T>(
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
        let outcome: PyResult<T> = work(&transaction);
        match driver.call_method1("rollback", (&transaction.connection,)) {
            Ok(_) => outcome,
            Err(rollback) => {
                if let Err(failure) = &outcome {
                    rollback
                        .value(py)
                        .setattr("__context__", failure.value(py))?;
                }
                Err(rollback)
            }
        }
    }

    fn now<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        match &self.clock {
            Some(clock) => clock.bind(py).call0(),
            None => attr(py, "datetime", "datetime")?.call_method1("now", (utc(py)?,)),
        }
    }

    fn create_tables(&self, py: Python<'_>) -> PyResult<()> {
        self.transaction(py, |transaction| {
            let mut missing: Vec<&str> = Vec::new();
            for (name, _) in TABLES {
                let present: bool = if self.postgres {
                    !transaction
                        .run(sql::POSTGRES_HAS_TABLE, &[("name", object(py, name)?)])?
                        .0
                        .is_empty()
                } else {
                    let mut present: bool = false;
                    for probe in sql::sqlite_has_table(name) {
                        present = present || !transaction.run(&probe, &[])?.0.is_empty();
                    }
                    present
                };
                if !present {
                    missing.push(name);
                }
            }
            for (name, ddl) in TABLES {
                if missing.contains(&name) {
                    let ddl: String = if self.postgres {
                        sql::postgres_ddl(ddl)
                    } else {
                        ddl.to_owned()
                    };
                    transaction.run(&ddl, &[])?;
                }
            }
            Ok(())
        })
    }

    fn schema_version<'py>(&self, transaction: &Transaction<'py>) -> PyResult<Any<'py>> {
        let py: Python<'py> = transaction.driver.py();
        let rows: Vec<Row<'py>> = transaction
            .run(
                SELECT_SCHEMA_VERSION,
                &[("component_1", object(py, SCHEMA_COMPONENT)?)],
            )?
            .0;
        match rows.first() {
            Some(row) => Ok(column(row, "version")?),
            None => Ok(py.None().into_bound(py)),
        }
    }

    fn stored<'py>(
        &self,
        py: Python<'py>,
        plan_id: &Any<'py>,
        actor: &Any<'py>,
    ) -> PyResult<Stored<'py>> {
        let rows: Vec<Row<'py>> = self.read_only(py, |transaction| {
            Ok(transaction
                .run(
                    READ_PLAN,
                    &[("plan_id_1", plan_id.clone()), ("actor_1", actor.clone())],
                )?
                .0)
        })?;
        let Some(row) = rows.first() else {
            return Err(error(
                py,
                "DestructionPlanNotFoundError",
                "Destruction plan was not found for this actor".to_owned(),
            ));
        };
        let builtins = |name: &str| attr(py, "builtins", name);
        let payload_json: Any<'py> = builtins("str")?.call1((column(row, "payload_json")?,))?;
        let payload_sha256: Any<'py> = builtins("str")?.call1((column(row, "payload_sha256")?,))?;
        let payload_version: Any<'py> = attr(
            py,
            "sqltide.executor.destruction.constants",
            "DESTRUCTION_PLAN_PAYLOAD_VERSION",
        )?;
        if builtins("int")?
            .call1((column(row, "payload_version")?,))?
            .ne(&payload_version)?
        {
            return Err(error(
                py,
                "DestructionPlanCorruptError",
                "Stored destruction plan payload version is incompatible".to_owned(),
            ));
        }
        let computed: String = sha256_hex(&payload_json.extract::<String>()?);
        if !attr(py, "hmac", "compare_digest")?
            .call1((&payload_sha256, computed))?
            .is_truthy()?
        {
            return Err(error(
                py,
                "DestructionPlanCorruptError",
                "Stored destruction plan payload checksum does not match".to_owned(),
            ));
        }
        let plan: Any<'py> =
            attr(py, "sqltide._native", "deserialize_destruction_plan")?.call1((&payload_json,))?;
        let created_at: Any<'py> = as_utc(&column(row, "created_at")?)?;
        let expires_at: Any<'py> = as_utc(&column(row, "expires_at")?)?;
        if plan.getattr("plan_id")?.ne(plan_id)?
            || plan.getattr("created_at")?.ne(created_at)?
            || plan.getattr("expires_at")?.ne(expires_at)?
        {
            return Err(error(
                py,
                "DestructionPlanCorruptError",
                "Stored destruction plan identity or timestamps do not match its payload"
                    .to_owned(),
            ));
        }
        let status: Any<'py> = builtins("str")?.call1((column(row, "status")?,))?;
        let reviewed_at: Any<'py> = optional_utc(&column(row, "reviewed_at")?)?;
        let consumed_at: Any<'py> = optional_utc(&column(row, "consumed_at")?)?;
        let status: String = status.extract()?;
        let (reviewed, consumed) = (!reviewed_at.is_none(), !consumed_at.is_none());
        let valid: bool = (status == PENDING && !reviewed && !consumed)
            || (status == REVIEWED && reviewed && !consumed)
            || (status == CONSUMED && reviewed && consumed)
            || (status == EXPIRED && !consumed);
        if !valid {
            return Err(error(
                py,
                "DestructionPlanCorruptError",
                "Stored destruction plan lifecycle state is corrupt".to_owned(),
            ));
        }
        Ok(Stored {
            plan,
            payload_json,
            payload_sha256,
            status,
            reviewed_at,
        })
    }

    fn require_current<'py>(
        &self,
        py: Python<'py>,
        plan_id: &Any<'py>,
        actor: &Any<'py>,
    ) -> PyResult<Stored<'py>> {
        let stored: Stored<'py> = self.stored(py, plan_id, actor)?;
        if stored.status == CONSUMED {
            return Err(error(
                py,
                "DestructionPlanNotFoundError",
                format!("Destruction plan {} was not found", plan_id.repr()?),
            ));
        }
        let now: Any<'py> = self.now(py)?;
        if stored.status == EXPIRED || now.ge(stored.plan.getattr("expires_at")?)? {
            self.transaction(py, |transaction| {
                transaction.run(
                    MARK_EXPIRED,
                    &[
                        ("status", object(py, EXPIRED)?),
                        ("plan_id_1", plan_id.clone()),
                        ("actor_1", actor.clone()),
                        ("expires_at_1", transaction.time(&now)?),
                        ("status_1_1", object(py, PENDING)?),
                        ("status_1_2", object(py, REVIEWED)?),
                    ],
                )?;
                Ok(())
            })?;
            return Err(error(
                py,
                "DestructionPlanExpiredError",
                format!("Destruction plan {} has expired", plan_id.repr()?),
            ));
        }
        Ok(stored)
    }

    fn not_reviewed(py: Python<'_>, plan_id: &Any<'_>) -> PyResult<PyErr> {
        Ok(error(
            py,
            "DestructionPlanNotReviewedError",
            format!("Destruction plan {} has not been reviewed", plan_id.repr()?),
        ))
    }
}

#[pymethods]
impl RelationalDestructionPlanStore {
    #[new]
    #[pyo3(signature = (*, url, clock = None))]
    fn new(
        py: Python<'_>,
        url: Bound<'_, PyAny>,
        clock: Option<Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        let url_text: String = url.extract()?;
        if let Some(path) = url_text.strip_prefix(SQLITE_FILE_PREFIX) {
            let memory: String =
                attr(py, "sqltide.auth.constants", "SQLITE_MEMORY_PATH")?.extract()?;
            if path != memory {
                attr(py, "pathlib", "Path")?
                    .call1((path,))?
                    .getattr("parent")?
                    .call_method(
                        "mkdir",
                        (),
                        Some(&py_kwargs!(py; parents = true, exist_ok = true)?),
                    )?;
            }
        }
        let sqlite: bool = url_text.starts_with("sqlite:");
        let initialize = || -> PyResult<Self> {
            let timeout: Option<f64> = sqlite.then_some(SQLITE_TIMEOUT_SECONDS);
            let driver: Any<'_> = driver_class(py)?.call(
                (),
                Some(&py_kwargs!(py; url = &url, sqlite_timeout_seconds = timeout)?),
            )?;
            let store: Self = Self {
                url: url.clone().unbind(),
                driver: driver.unbind(),
                clock: clock.filter(|clock| !clock.is_none()).map(Bound::unbind),
                postgres: !sqlite,
            };
            store.bootstrap(py)?;
            Ok(store)
        };
        initialize().map_err(|failure| {
            let wrap: bool = matches!(error_kind(py, &failure), Ok(Some(_)))
                || failure.is_instance_of::<PyOSError>(py);
            if !wrap {
                return failure;
            }
            match failure.value(py).str() {
                Ok(text) => chained(
                    py,
                    error(
                        py,
                        "DestructionValidationError",
                        format!("Could not initialize destruction plan store: {text}"),
                    ),
                    &failure,
                ),
                Err(inner) => inner,
            }
        })
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        self.driver.bind(py).call_method0("close")?;
        Ok(())
    }

    /// Create the additive schema component and reject incompatible versions.
    fn bootstrap(&self, py: Python<'_>) -> PyResult<()> {
        attr(py, "sqltide._native", "rename_streambuild_tables")?.call(
            (),
            Some(&py_kwargs!(py; url = self.url.bind(py), sqlite_timeout_seconds = 30)?),
        )?;
        self.create_tables(py)?;
        let recorded: PyResult<()> = self.transaction(py, |transaction| {
            let current: Any<'_> = self.schema_version(transaction)?;
            if current.is_none() {
                transaction.run(
                    sql::INSERT_SCHEMA_VERSION,
                    &[
                        ("component", object(py, SCHEMA_COMPONENT)?),
                        ("version", object(py, SCHEMA_VERSION)?),
                    ],
                )?;
                Ok(())
            } else {
                schema_version_error(py, &current)
            }
        });
        match recorded {
            Err(failure) if matches!(error_kind(py, &failure)?.as_deref(), Some("integrity")) => {
                self.read_only(py, |transaction| self.schema_version(transaction))
                    .and_then(|raced| schema_version_error(py, &raced))
                    .map_err(|raised| while_handling(py, raised, &failure))
            }
            other => other,
        }
    }

    #[pyo3(signature = (*, plan, actor))]
    fn save(
        &self,
        py: Python<'_>,
        plan: Bound<'_, PyAny>,
        actor: Bound<'_, PyAny>,
    ) -> PyResult<()> {
        if !actor.is_truthy()? {
            return Err(error(
                py,
                "DestructionValidationError",
                "Destruction plan actor must not be empty".to_owned(),
            ));
        }
        let payload_json: String = attr(py, "sqltide._native", "serialize_destruction_plan")?
            .call1((&plan,))?
            .extract()?;
        let payload_version: Any<'_> = attr(
            py,
            "sqltide.executor.destruction.constants",
            "DESTRUCTION_PLAN_PAYLOAD_VERSION",
        )?;
        let plan_id: Any<'_> = plan.getattr("plan_id")?;
        let saved: PyResult<()> = self.transaction(py, |transaction| {
            transaction.run(
                INSERT_PLAN,
                &[
                    ("plan_id", plan_id.clone()),
                    ("actor", actor.clone()),
                    ("payload_version", payload_version),
                    ("payload_json", object(py, &payload_json)?),
                    ("payload_sha256", object(py, sha256_hex(&payload_json))?),
                    (
                        "created_at",
                        transaction.time(&plan.getattr("created_at")?)?,
                    ),
                    (
                        "expires_at",
                        transaction.time(&plan.getattr("expires_at")?)?,
                    ),
                    ("reviewed_at", py.None().into_bound(py)),
                    ("consumed_at", py.None().into_bound(py)),
                    ("status", object(py, PENDING)?),
                ],
            )?;
            Ok(())
        });
        match saved {
            Err(failure) if matches!(error_kind(py, &failure)?.as_deref(), Some("integrity")) => {
                Err(chained(
                    py,
                    error(
                        py,
                        "DestructionValidationError",
                        format!("Destruction plan {} already exists", plan_id.repr()?),
                    ),
                    &failure,
                ))
            }
            other => other,
        }
    }

    #[pyo3(signature = (*, plan_id, actor))]
    fn get<'py>(
        &self,
        py: Python<'py>,
        plan_id: Bound<'py, PyAny>,
        actor: Bound<'py, PyAny>,
    ) -> PyResult<Any<'py>> {
        Ok(self.require_current(py, &plan_id, &actor)?.plan)
    }

    #[pyo3(signature = (*, plan_id, actor))]
    fn mark_reviewed<'py>(
        &self,
        py: Python<'py>,
        plan_id: Bound<'py, PyAny>,
        actor: Bound<'py, PyAny>,
    ) -> PyResult<Any<'py>> {
        let stored: Stored<'py> = self.require_current(py, &plan_id, &actor)?;
        let now: Any<'py> = self.now(py)?;
        let affected: Any<'py> = self.transaction(py, |transaction| {
            let at: Any<'py> = transaction.time(&now)?;
            Ok(transaction
                .run(
                    MARK_REVIEWED,
                    &[
                        ("reviewed_at", at.clone()),
                        ("status", object(py, REVIEWED)?),
                        ("plan_id_1", plan_id.clone()),
                        ("actor_1", actor.clone()),
                        ("expires_at_1", at),
                        ("payload_json_1", stored.payload_json.clone()),
                        ("payload_sha256_1", stored.payload_sha256.clone()),
                        ("status_1_1", object(py, PENDING)?),
                        ("status_1_2", object(py, REVIEWED)?),
                    ],
                )?
                .1)
        })?;
        if affected.ne(1)? {
            self.require_current(py, &plan_id, &actor)?;
            return Err(error(
                py,
                "DestructionPlanNotFoundError",
                format!("Destruction plan {} changed during review", plan_id.repr()?),
            ));
        }
        Ok(now)
    }

    #[pyo3(signature = (*, plan_id, actor))]
    fn reviewed_at<'py>(
        &self,
        py: Python<'py>,
        plan_id: Bound<'py, PyAny>,
        actor: Bound<'py, PyAny>,
    ) -> PyResult<Any<'py>> {
        let stored: Stored<'py> = self.require_current(py, &plan_id, &actor)?;
        if stored.reviewed_at.is_none() {
            return Err(Self::not_reviewed(py, &plan_id)?);
        }
        Ok(stored.reviewed_at)
    }

    #[pyo3(signature = (*, plan_id, challenge_responses, actor))]
    fn consume<'py>(
        &self,
        py: Python<'py>,
        plan_id: Bound<'py, PyAny>,
        challenge_responses: Bound<'py, PyAny>,
        actor: Bound<'py, PyAny>,
    ) -> PyResult<Any<'py>> {
        let stored: Stored<'py> = self.require_current(py, &plan_id, &actor)?;
        if stored.reviewed_at.is_none() {
            return Err(Self::not_reviewed(py, &plan_id)?);
        }
        if challenge_responses.ne(stored.plan.getattr("challenges")?)? {
            return Err(error(
                py,
                "DestructionChallengeError",
                "Challenge responses must exactly match the frozen plan in order".to_owned(),
            ));
        }
        let now: Any<'py> = self.now(py)?;
        let affected: Any<'py> = self.transaction(py, |transaction| {
            let at: Any<'py> = transaction.time(&now)?;
            Ok(transaction
                .run(
                    CONSUME,
                    &[
                        ("consumed_at", at.clone()),
                        ("status", object(py, CONSUMED)?),
                        ("plan_id_1", plan_id.clone()),
                        ("actor_1", actor.clone()),
                        ("status_1", object(py, REVIEWED)?),
                        ("expires_at_1", at),
                        ("payload_json_1", stored.payload_json.clone()),
                        ("payload_sha256_1", stored.payload_sha256.clone()),
                    ],
                )?
                .1)
        })?;
        if affected.ne(1)? {
            self.require_current(py, &plan_id, &actor)?;
            return Err(error(
                py,
                "DestructionPlanNotFoundError",
                format!("Destruction plan {} was already consumed", plan_id.repr()?),
            ));
        }
        Ok(stored.plan)
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<RelationalDestructionPlanStore>()
}
