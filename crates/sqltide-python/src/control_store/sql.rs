//! Statement text for the account control store, as SQLAlchemy 2.0 compiled it for SQLite and
//! PostgreSQL. Values are always bound as `:name` parameters.

/// Tables in the dependency order SQLAlchemy creates them, with their SQLite DDL. PostgreSQL differs only in
/// [`postgres_ddl`]'s two type substitutions.
pub(crate) const TABLES: [(&str, &str); 9] = [
    (
        "sqltide_schema_versions",
        "\nCREATE TABLE sqltide_schema_versions (\n\tcomponent VARCHAR(64) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tPRIMARY KEY (component)\n)\n\n",
    ),
    (
        "sqltide_users",
        "\nCREATE TABLE sqltide_users (\n\tid VARCHAR(36) NOT NULL, \n\tusername VARCHAR(128) NOT NULL, \n\tdisplay_name VARCHAR(256), \n\temail VARCHAR(320), \n\tis_active BOOLEAN NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tupdated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (username)\n)\n\n",
    ),
    (
        "sqltide_roles",
        "\nCREATE TABLE sqltide_roles (\n\tname VARCHAR(128) NOT NULL, \n\tdescription VARCHAR(512) NOT NULL, \n\tis_system BOOLEAN NOT NULL, \n\tPRIMARY KEY (name)\n)\n\n",
    ),
    (
        "sqltide_account_audit_log",
        "\nCREATE TABLE sqltide_account_audit_log (\n\tid INTEGER NOT NULL, \n\toperation VARCHAR(128) NOT NULL, \n\tactor_user_id VARCHAR(36), \n\taffected_user_id VARCHAR(36), \n\toccurred_at DATETIME NOT NULL, \n\tdetails_json TEXT NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n",
    ),
    (
        "sqltide_external_identities",
        "\nCREATE TABLE sqltide_external_identities (\n\tsource VARCHAR(64) NOT NULL, \n\tsubject VARCHAR(256) NOT NULL, \n\tuser_id VARCHAR(36) NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (source, subject), \n\tFOREIGN KEY(user_id) REFERENCES sqltide_users (id) ON DELETE CASCADE\n)\n\n",
    ),
    (
        "sqltide_password_credentials",
        "\nCREATE TABLE sqltide_password_credentials (\n\tuser_id VARCHAR(36) NOT NULL, \n\tpassword_hash TEXT NOT NULL, \n\tpassword_changed_at DATETIME NOT NULL, \n\tPRIMARY KEY (user_id), \n\tFOREIGN KEY(user_id) REFERENCES sqltide_users (id) ON DELETE CASCADE\n)\n\n",
    ),
    (
        "sqltide_sessions",
        "\nCREATE TABLE sqltide_sessions (\n\ttoken_hash VARCHAR(64) NOT NULL, \n\tuser_id VARCHAR(36) NOT NULL, \n\tcsrf_token VARCHAR(128) NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tlast_seen_at DATETIME NOT NULL, \n\texpires_at DATETIME NOT NULL, \n\trevoked_at DATETIME, \n\tPRIMARY KEY (token_hash), \n\tFOREIGN KEY(user_id) REFERENCES sqltide_users (id) ON DELETE CASCADE\n)\n\n",
    ),
    (
        "sqltide_user_roles",
        "\nCREATE TABLE sqltide_user_roles (\n\tuser_id VARCHAR(36) NOT NULL, \n\trole_name VARCHAR(128) NOT NULL, \n\tassigned_at DATETIME NOT NULL, \n\tassigned_by VARCHAR(36), \n\tCONSTRAINT uq_sqltide_user_role UNIQUE (user_id, role_name), \n\tFOREIGN KEY(user_id) REFERENCES sqltide_users (id) ON DELETE CASCADE, \n\tFOREIGN KEY(role_name) REFERENCES sqltide_roles (name) ON DELETE CASCADE\n)\n\n",
    ),
    (
        "sqltide_project_role_assignments",
        "\nCREATE TABLE sqltide_project_role_assignments (\n\tid VARCHAR(36) NOT NULL, \n\tuser_id VARCHAR(36) NOT NULL, \n\tproject_name VARCHAR(256) NOT NULL, \n\trole_name VARCHAR(128) NOT NULL, \n\ttarget_name VARCHAR(128), \n\tassignment_scope VARCHAR(128) NOT NULL, \n\tassigned_by VARCHAR(36), \n\tassigned_at DATETIME NOT NULL, \n\trevoked_by VARCHAR(36), \n\trevoked_at DATETIME, \n\tactive_key VARCHAR(36) NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_sqltide_active_project_role UNIQUE (user_id, project_name, role_name, assignment_scope, active_key), \n\tFOREIGN KEY(user_id) REFERENCES sqltide_users (id) ON DELETE CASCADE\n)\n\n",
    ),
];

/// The order SQLAlchemy probes for existing tables before it creates the missing ones.
pub(crate) const PROBE_ORDER: [&str; 9] = [
    "sqltide_schema_versions",
    "sqltide_users",
    "sqltide_external_identities",
    "sqltide_password_credentials",
    "sqltide_sessions",
    "sqltide_roles",
    "sqltide_user_roles",
    "sqltide_project_role_assignments",
    "sqltide_account_audit_log",
];

/// The PostgreSQL form of one table's SQLite DDL.
pub(crate) fn postgres_ddl(ddl: &str) -> String {
    ddl.replace("DATETIME", "TIMESTAMP WITH TIME ZONE").replace(
        "\tid INTEGER NOT NULL, \n\toperation",
        "\tid SERIAL NOT NULL, \n\toperation",
    )
}

pub(crate) const SQLITE_TABLE_NAMES: &str = "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite~_%' ESCAPE '~' ORDER BY name";
pub(crate) const POSTGRES_TABLE_NAMES: &str = "SELECT pg_catalog.pg_class.relname FROM pg_catalog.pg_class JOIN pg_catalog.pg_namespace ON pg_catalog.pg_namespace.oid = pg_catalog.pg_class.relnamespace WHERE pg_catalog.pg_class.relkind = ANY (ARRAY['r', 'p']) AND pg_catalog.pg_class.relpersistence != 't' AND pg_catalog.pg_table_is_visible(pg_catalog.pg_class.oid) AND pg_catalog.pg_namespace.nspname != 'pg_catalog'";
pub(crate) const POSTGRES_HAS_TABLE: &str = "SELECT pg_catalog.pg_class.relname FROM pg_catalog.pg_class JOIN pg_catalog.pg_namespace ON pg_catalog.pg_namespace.oid = pg_catalog.pg_class.relnamespace WHERE pg_catalog.pg_class.relname = :name AND pg_catalog.pg_class.relkind = ANY (ARRAY['r', 'p', 'f', 'v', 'm']) AND pg_catalog.pg_table_is_visible(pg_catalog.pg_class.oid) AND pg_catalog.pg_namespace.nspname != 'pg_catalog'";

/// SQLite's table probes for one of this store's own table names, main schema then temp.
pub(crate) fn sqlite_has_table(name: &str) -> [String; 2] {
    ["main", "temp"].map(|schema| format!("PRAGMA {schema}.table_info(\"{name}\")"))
}

pub(crate) const USER_COLUMNS: &str = "sqltide_users.id, sqltide_users.username, sqltide_users.display_name, sqltide_users.email, sqltide_users.is_active, sqltide_users.created_at, sqltide_users.updated_at";
pub(crate) const ASSIGNMENT_COLUMNS: &str = "sqltide_project_role_assignments.id, sqltide_project_role_assignments.user_id, sqltide_project_role_assignments.project_name, sqltide_project_role_assignments.role_name, sqltide_project_role_assignments.target_name, sqltide_project_role_assignments.assignment_scope, sqltide_project_role_assignments.assigned_by, sqltide_project_role_assignments.assigned_at, sqltide_project_role_assignments.revoked_by, sqltide_project_role_assignments.revoked_at, sqltide_project_role_assignments.active_key";

pub(crate) const SCHEMA_VERSION: &str = "SELECT sqltide_schema_versions.version FROM sqltide_schema_versions WHERE sqltide_schema_versions.component = :component";
pub(crate) const INSERT_SCHEMA_VERSION: &str =
    "INSERT INTO sqltide_schema_versions (component, version) VALUES (:component, :version)";
pub(crate) const ROLE_NAMES: &str = "SELECT sqltide_roles.name FROM sqltide_roles";
pub(crate) const INSERT_ROLE: &str = "INSERT INTO sqltide_roles (name, description, is_system) VALUES (:name, :description, :is_system)";
pub(crate) const ROLE_BY_NAME: &str =
    "SELECT sqltide_roles.name FROM sqltide_roles WHERE sqltide_roles.name = :name";
pub(crate) const INSERT_USER_ROLE: &str = "INSERT INTO sqltide_user_roles (user_id, role_name, assigned_at, assigned_by) VALUES (:user_id, :role_name, :assigned_at, :assigned_by)";
pub(crate) const USER_ROLES: &str = "SELECT sqltide_user_roles.role_name FROM sqltide_user_roles WHERE sqltide_user_roles.user_id = :user_id ORDER BY sqltide_user_roles.role_name";
pub(crate) const USER_SOURCES: &str = "SELECT sqltide_external_identities.source FROM sqltide_external_identities WHERE sqltide_external_identities.user_id = :user_id ORDER BY sqltide_external_identities.source";
pub(crate) const PASSWORD_PRESENT: &str = "SELECT sqltide_password_credentials.user_id FROM sqltide_password_credentials WHERE sqltide_password_credentials.user_id = :user_id";
pub(crate) const INSERT_USER: &str = "INSERT INTO sqltide_users (id, username, display_name, email, is_active, created_at, updated_at) VALUES (:id, :username, :display_name, :email, :is_active, :created_at, :updated_at)";
pub(crate) const INSERT_EXTERNAL_IDENTITY: &str = "INSERT INTO sqltide_external_identities (source, subject, user_id, created_at) VALUES (:source, :subject, :user_id, :created_at)";
pub(crate) const INSERT_PASSWORD: &str = "INSERT INTO sqltide_password_credentials (user_id, password_hash, password_changed_at) VALUES (:user_id, :password_hash, :password_changed_at)";
pub(crate) const UPDATE_PASSWORD: &str = "UPDATE sqltide_password_credentials SET password_hash=:password_hash, password_changed_at=:password_changed_at WHERE sqltide_password_credentials.user_id = :user_id";
pub(crate) const INSERT_SESSION: &str = "INSERT INTO sqltide_sessions (token_hash, user_id, csrf_token, created_at, last_seen_at, expires_at, revoked_at) VALUES (:token_hash, :user_id, :csrf_token, :created_at, :last_seen_at, :expires_at, :revoked_at)";
pub(crate) const SESSION_LOOKUP: &str = "SELECT sqltide_sessions.csrf_token, sqltide_sessions.last_seen_at, sqltide_sessions.expires_at, sqltide_sessions.revoked_at, sqltide_users.id, sqltide_users.username, sqltide_users.display_name, sqltide_users.email, sqltide_users.is_active, sqltide_users.created_at, sqltide_users.updated_at FROM sqltide_sessions JOIN sqltide_users ON sqltide_users.id = sqltide_sessions.user_id WHERE sqltide_sessions.token_hash = :token_hash";
pub(crate) const TOUCH_SESSION: &str = "UPDATE sqltide_sessions SET last_seen_at=:last_seen_at WHERE sqltide_sessions.token_hash = :token_hash";
pub(crate) const SESSION_USER: &str = "SELECT sqltide_sessions.user_id FROM sqltide_sessions WHERE sqltide_sessions.token_hash = :token_hash";
pub(crate) const REVOKE_SESSION: &str = "UPDATE sqltide_sessions SET revoked_at=:revoked_at WHERE sqltide_sessions.token_hash = :token_hash";
pub(crate) const REVOKE_USER_SESSIONS: &str = "UPDATE sqltide_sessions SET revoked_at=:revoked_at WHERE sqltide_sessions.user_id = :user_id AND sqltide_sessions.revoked_at IS NULL";
pub(crate) const SET_USER_ACTIVE: &str = "UPDATE sqltide_users SET is_active=:is_active, updated_at=:updated_at WHERE sqltide_users.id = :id";
pub(crate) const SET_USER_PROFILE: &str = "UPDATE sqltide_users SET display_name=:display_name, email=:email, updated_at=:updated_at WHERE sqltide_users.id = :id";
pub(crate) const SET_USER_ACCOUNT: &str = "UPDATE sqltide_users SET display_name=:display_name, email=:email, is_active=:is_active, updated_at=:updated_at WHERE sqltide_users.id = :id";
pub(crate) const USER_ID: &str =
    "SELECT sqltide_users.id FROM sqltide_users WHERE sqltide_users.id = :id";
pub(crate) const DELETE_USER_ROLE: &str = "DELETE FROM sqltide_user_roles WHERE sqltide_user_roles.user_id = :user_id AND sqltide_user_roles.role_name = :role_name";
pub(crate) const INSERT_ASSIGNMENT: &str = "INSERT INTO sqltide_project_role_assignments (id, user_id, project_name, role_name, target_name, assignment_scope, assigned_by, assigned_at, revoked_by, revoked_at, active_key) VALUES (:id, :user_id, :project_name, :role_name, :target_name, :assignment_scope, :assigned_by, :assigned_at, :revoked_by, :revoked_at, :active_key)";
pub(crate) const REVOKE_ASSIGNMENT: &str = "UPDATE sqltide_project_role_assignments SET revoked_by=:revoked_by, revoked_at=:revoked_at, active_key=:active_key WHERE sqltide_project_role_assignments.id = :id";
pub(crate) const INSERT_AUDIT: &str = "INSERT INTO sqltide_account_audit_log (operation, actor_user_id, affected_user_id, occurred_at, details_json) VALUES (:operation, :actor_user_id, :affected_user_id, :occurred_at, :details_json)";
pub(crate) const AUDIT_RECORDS: &str = "SELECT sqltide_account_audit_log.id, sqltide_account_audit_log.operation, sqltide_account_audit_log.actor_user_id, sqltide_account_audit_log.affected_user_id, sqltide_account_audit_log.occurred_at, sqltide_account_audit_log.details_json FROM sqltide_account_audit_log ORDER BY sqltide_account_audit_log.id";
pub(crate) const PRUNE_SESSIONS: &str = "DELETE FROM sqltide_sessions WHERE sqltide_sessions.expires_at <= :now OR sqltide_sessions.revoked_at IS NOT NULL";
pub(crate) const ADMIN_MEMBERSHIP: &str = "SELECT sqltide_user_roles.user_id FROM sqltide_user_roles WHERE sqltide_user_roles.user_id = :user_id AND sqltide_user_roles.role_name = :role_name";

/// One statement whose text differs between SQLite and PostgreSQL.
pub(crate) fn lock_admin_role(postgres: bool) -> String {
    format!(
        "SELECT sqltide_roles.name FROM sqltide_roles WHERE sqltide_roles.name = :name{}",
        if postgres { " FOR UPDATE" } else { "" }
    )
}

pub(crate) fn active_admin_count(postgres: bool) -> String {
    format!(
        "SELECT count(*) AS count_1 FROM sqltide_users JOIN sqltide_user_roles ON sqltide_user_roles.user_id = sqltide_users.id WHERE sqltide_users.is_active IS {} AND sqltide_user_roles.role_name = :role_name",
        if postgres { "true" } else { "1" }
    )
}

pub(crate) fn users_where(clause: &str) -> String {
    format!("SELECT {USER_COLUMNS} FROM sqltide_users{clause}")
}

pub(crate) fn assignments_where(clause: &str) -> String {
    format!("SELECT {ASSIGNMENT_COLUMNS} FROM sqltide_project_role_assignments{clause}")
}
