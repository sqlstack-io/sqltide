"""Relational SQLite/PostgreSQL account control store."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    event,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.sql import Select

from sqltide.auth._helpers.password_hashing import hash_password, verify_password
from sqltide.auth._helpers.usernames import canonical_username
from sqltide.auth.constants import (
    ACCOUNT_SCHEMA_COMPONENT,
    ADMIN_ROLE,
    SQLITE_MEMORY_PATH,
    VIEWER_ROLE,
)
from sqltide.auth.exceptions import (
    AccountConflictError,
    AccountNotFoundError,
    AccountValidationError,
    ControlStoreError,
)
from sqltide.auth.main.rename_streambuild_tables import rename_streambuild_tables
from sqltide.auth.models import (
    AccountAuditRecord,
    Principal,
    ProjectRoleAssignment,
    ResolvedSession,
    SessionCredentials,
    UserAccount,
)
from sqltide.auth.types import AuthenticationSource

_SCHEMA_VERSION: int = 1
_DUMMY_PASSWORD_HASH: str = hash_password("sqltide dummy password verification")
_SESSION_LAST_SEEN_WRITE_INTERVAL: timedelta = timedelta(minutes=1)

_METADATA: MetaData = MetaData()
_SCHEMA_VERSIONS: Table = Table(
    "sqltide_schema_versions",
    _METADATA,
    Column("component", String(64), primary_key=True),
    Column("version", Integer, nullable=False),
)
_USERS: Table = Table(
    "sqltide_users",
    _METADATA,
    Column("id", String(36), primary_key=True),
    Column("username", String(128), nullable=False, unique=True),
    Column("display_name", String(256)),
    Column("email", String(320)),
    Column("is_active", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
_EXTERNAL_IDENTITIES: Table = Table(
    "sqltide_external_identities",
    _METADATA,
    Column("source", String(64), primary_key=True),
    Column("subject", String(256), primary_key=True),
    Column("user_id", String(36), ForeignKey(_USERS.c.id, ondelete="CASCADE"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
_PASSWORD_CREDENTIALS: Table = Table(
    "sqltide_password_credentials",
    _METADATA,
    Column("user_id", String(36), ForeignKey(_USERS.c.id, ondelete="CASCADE"), primary_key=True),
    Column("password_hash", Text, nullable=False),
    Column("password_changed_at", DateTime(timezone=True), nullable=False),
)
_SESSIONS: Table = Table(
    "sqltide_sessions",
    _METADATA,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", String(36), ForeignKey(_USERS.c.id, ondelete="CASCADE"), nullable=False),
    Column("csrf_token", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
)
_ROLES: Table = Table(
    "sqltide_roles",
    _METADATA,
    Column("name", String(128), primary_key=True),
    Column("description", String(512), nullable=False),
    Column("is_system", Boolean, nullable=False),
)
_USER_ROLES: Table = Table(
    "sqltide_user_roles",
    _METADATA,
    Column("user_id", String(36), ForeignKey(_USERS.c.id, ondelete="CASCADE"), nullable=False),
    Column("role_name", String(128), ForeignKey(_ROLES.c.name, ondelete="CASCADE"), nullable=False),
    Column("assigned_at", DateTime(timezone=True), nullable=False),
    Column("assigned_by", String(36)),
    UniqueConstraint("user_id", "role_name", name="uq_sqltide_user_role"),
)
_PROJECT_ROLE_ASSIGNMENTS: Table = Table(
    "sqltide_project_role_assignments",
    _METADATA,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(36), ForeignKey(_USERS.c.id, ondelete="CASCADE"), nullable=False),
    Column("project_name", String(256), nullable=False),
    Column("role_name", String(128), nullable=False),
    Column("target_name", String(128)),
    Column("assignment_scope", String(128), nullable=False),
    Column("assigned_by", String(36)),
    Column("assigned_at", DateTime(timezone=True), nullable=False),
    Column("revoked_by", String(36)),
    Column("revoked_at", DateTime(timezone=True)),
    Column("active_key", String(36), nullable=False),
    UniqueConstraint(
        "user_id",
        "project_name",
        "role_name",
        "assignment_scope",
        "active_key",
        name="uq_sqltide_active_project_role",
    ),
)
_ACCOUNT_AUDIT: Table = Table(
    "sqltide_account_audit_log",
    _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("operation", String(128), nullable=False),
    Column("actor_user_id", String(36)),
    Column("affected_user_id", String(36)),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("details_json", Text, nullable=False),
)


class ControlStore:
    """Synchronous account repository shared by dev server worker threads and the CLI."""

    def __init__(self, *, url: str) -> None:
        self._url = url
        self._authentication_revision: int = 0
        if url.startswith("sqlite:///"):
            path_value: str = url.removeprefix("sqlite:///")
            if path_value != SQLITE_MEMORY_PATH:
                Path(path_value).parent.mkdir(parents=True, exist_ok=True)
        connect_args: dict[str, object] = (
            {"check_same_thread": False} if url.startswith("sqlite:") else {}
        )
        try:
            self._engine: Engine = create_engine(
                url,
                connect_args=connect_args,
                pool_pre_ping=True,
            )
            if url.startswith("sqlite:"):
                event.listen(self._engine, "connect", _enable_sqlite_foreign_keys)
            self.bootstrap()
        except (SQLAlchemyError, OSError) as error:
            raise ControlStoreError(
                f"Could not initialize SQLTide control store: {error}"
            ) from error

    def close(self) -> None:
        self._engine.dispose()

    @property
    def url(self) -> str:
        """Return the configured URL for cooperating control-store components."""

        return self._url

    @property
    def authentication_revision(self) -> int:
        """Return the process-local generation for cached identity invalidation."""

        return self._authentication_revision

    def bootstrap(self) -> None:
        """Create the current clean schema and reject incompatible state."""

        rename_streambuild_tables(self._engine)
        _METADATA.create_all(self._engine)
        with self._engine.begin() as connection:
            current: int | None = connection.execute(
                select(_SCHEMA_VERSIONS.c.version).where(
                    _SCHEMA_VERSIONS.c.component == ACCOUNT_SCHEMA_COMPONENT
                )
            ).scalar_one_or_none()
            if current is None:
                connection.execute(
                    insert(_SCHEMA_VERSIONS).values(
                        component=ACCOUNT_SCHEMA_COMPONENT, version=_SCHEMA_VERSION
                    )
                )
            elif current != _SCHEMA_VERSION:
                raise ControlStoreError(
                    "SQLTide account schema is incompatible: "
                    f"found version {current}, expected {_SCHEMA_VERSION}"
                )
            self._seed_system_roles(connection)

    def list_users(self) -> tuple[UserAccount, ...]:
        with self._engine.connect() as connection:
            rows: list[RowMapping] = list(
                connection.execute(select(_USERS).order_by(_USERS.c.username)).mappings()
            )
            return tuple(self._account_from_row(connection=connection, row=row) for row in rows)

    def get_user_by_id(self, *, user_id: UUID) -> UserAccount | None:
        with self._engine.connect() as connection:
            row: RowMapping | None = (
                connection.execute(select(_USERS).where(_USERS.c.id == str(user_id)))
                .mappings()
                .one_or_none()
            )
            return None if row is None else self._account_from_row(connection=connection, row=row)

    def get_user_by_username(self, *, username: str) -> UserAccount | None:
        canonical: str = canonical_username(username)
        with self._engine.connect() as connection:
            row: RowMapping | None = (
                connection.execute(select(_USERS).where(_USERS.c.username == canonical))
                .mappings()
                .one_or_none()
            )
            return None if row is None else self._account_from_row(connection=connection, row=row)

    def create_user(
        self,
        *,
        username: str,
        display_name: str | None = None,
        email: str | None = None,
        password: str | None = None,
        authentication_source: AuthenticationSource | None = None,
        external_subject: str | None = None,
        roles: tuple[str, ...] = ("viewer",),
        actor_user_id: UUID | None = None,
    ) -> UserAccount:
        canonical: str = canonical_username(username)
        now: datetime = _utc_now()
        user_id: UUID = uuid4()
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(_USERS).values(
                        id=str(user_id),
                        username=canonical,
                        display_name=_clean_optional(display_name),
                        email=_clean_optional(email),
                        is_active=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
                if authentication_source is not None:
                    if authentication_source == AuthenticationSource.LOCAL:
                        raise AccountValidationError(
                            "Local principals are not persisted as external identities"
                        )
                    subject: str = (external_subject or canonical).strip()
                    if not subject:
                        raise AccountValidationError("External identity subject must not be blank")
                    connection.execute(
                        insert(_EXTERNAL_IDENTITIES).values(
                            source=authentication_source,
                            subject=subject,
                            user_id=str(user_id),
                            created_at=now,
                        )
                    )
                if password is not None:
                    connection.execute(
                        insert(_PASSWORD_CREDENTIALS).values(
                            user_id=str(user_id),
                            password_hash=hash_password(password),
                            password_changed_at=now,
                        )
                    )
                for role in roles:
                    self._assign_role(
                        connection=connection,
                        user_id=user_id,
                        role_name=role,
                        actor_user_id=actor_user_id,
                        now=now,
                    )
                self._audit(
                    connection=connection,
                    operation="user.created",
                    actor_user_id=actor_user_id,
                    affected_user_id=user_id,
                    details={"username": canonical},
                    now=now,
                )
        except IntegrityError as error:
            raise AccountConflictError(
                f"User or external identity '{canonical}' already exists"
            ) from error
        self._invalidate_authentication()
        account: UserAccount | None = self.get_user_by_id(user_id=user_id)
        if account is None:
            raise ControlStoreError("Created account could not be reloaded")
        return account

    def resolve_external_identity(
        self,
        *,
        source: AuthenticationSource,
        subject: str,
    ) -> UserAccount | None:
        with self._engine.connect() as connection:
            row: RowMapping | None = (
                connection.execute(
                    select(_USERS)
                    .join(_EXTERNAL_IDENTITIES, _EXTERNAL_IDENTITIES.c.user_id == _USERS.c.id)
                    .where(
                        _EXTERNAL_IDENTITIES.c.source == source,
                        _EXTERNAL_IDENTITIES.c.subject == subject,
                    )
                )
                .mappings()
                .one_or_none()
            )
            return None if row is None else self._account_from_row(connection=connection, row=row)

    def provision_proxy_user(
        self,
        *,
        subject: str,
        username: str,
        display_name: str | None,
        email: str | None,
        default_role: str,
    ) -> UserAccount:
        existing: UserAccount | None = self.resolve_external_identity(
            source=AuthenticationSource.TRUSTED_PROXY,
            subject=subject,
        )
        if existing is not None:
            return existing
        if self.get_user_by_username(username=username) is not None:
            return self._provisioned_by_competing_writer(subject=subject)
        try:
            return self.create_user(
                username=username,
                display_name=display_name,
                email=email,
                authentication_source=AuthenticationSource.TRUSTED_PROXY,
                external_subject=subject,
                roles=(default_role,),
            )
        except AccountConflictError:
            return self._provisioned_by_competing_writer(subject=subject)

    def _provisioned_by_competing_writer(self, *, subject: str) -> UserAccount:
        """Resolve the identity a competing writer linked, or report a real conflict."""

        raced: UserAccount | None = self.resolve_external_identity(
            source=AuthenticationSource.TRUSTED_PROXY,
            subject=subject,
        )
        if raced is None:
            raise AccountConflictError(
                f"Proxy identity '{subject}' conflicts with an existing unlinked username"
            )
        return raced

    def authenticate_password(
        self,
        *,
        username: str,
        password: str,
        session_ttl_seconds: int,
    ) -> tuple[UserAccount, SessionCredentials] | None:
        canonical: str
        try:
            canonical = canonical_username(username)
        except ValueError:
            return None
        now: datetime = _utc_now()
        with self._engine.begin() as connection:
            row: RowMapping | None = (
                connection.execute(
                    select(_USERS, _PASSWORD_CREDENTIALS.c.password_hash)
                    .join(_PASSWORD_CREDENTIALS, _PASSWORD_CREDENTIALS.c.user_id == _USERS.c.id)
                    .where(_USERS.c.username == canonical)
                )
                .mappings()
                .one_or_none()
            )
            candidate_hash: str = _DUMMY_PASSWORD_HASH if row is None else str(row["password_hash"])
            valid, replacement = verify_password(
                password_hash=candidate_hash,
                password=password,
            )
            if row is None or not bool(row["is_active"]) or not valid:
                return None
            if replacement is not None:
                connection.execute(
                    update(_PASSWORD_CREDENTIALS)
                    .where(_PASSWORD_CREDENTIALS.c.user_id == row["id"])
                    .values(password_hash=replacement, password_changed_at=now)
                )
            credentials: SessionCredentials = self._create_session(
                connection=connection,
                user_id=UUID(str(row["id"])),
                ttl_seconds=session_ttl_seconds,
                now=now,
            )
            self._audit(
                connection=connection,
                operation="session.created",
                actor_user_id=UUID(str(row["id"])),
                affected_user_id=UUID(str(row["id"])),
                details={},
                now=now,
            )
            account: UserAccount = self._account_from_row(connection=connection, row=row)
            return account, credentials

    def resolve_session(self, *, token: str) -> ResolvedSession | None:
        if not token:
            return None
        token_hash: str = _token_hash(token)
        now: datetime = _utc_now()
        with self._engine.begin() as connection:
            row: RowMapping | None = (
                connection.execute(
                    select(
                        _SESSIONS.c.csrf_token,
                        _SESSIONS.c.last_seen_at,
                        _SESSIONS.c.expires_at,
                        _SESSIONS.c.revoked_at,
                        *_USERS.c,
                    )
                    .join(_USERS, _USERS.c.id == _SESSIONS.c.user_id)
                    .where(_SESSIONS.c.token_hash == token_hash)
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["revoked_at"] is not None or not bool(row["is_active"]):
                return None
            expires_at: datetime = _as_utc(row["expires_at"])
            if expires_at <= now:
                return None
            if now - _as_utc(row["last_seen_at"]) >= _SESSION_LAST_SEEN_WRITE_INTERVAL:
                connection.execute(
                    update(_SESSIONS)
                    .where(_SESSIONS.c.token_hash == token_hash)
                    .values(last_seen_at=now)
                )
            return ResolvedSession(
                principal=_principal_from_row(row=row, source=AuthenticationSource.PASSWORD),
                roles=self._roles_for_user(connection=connection, user_id=UUID(str(row["id"]))),
                csrf_token=str(row["csrf_token"]),
                expires_at=expires_at,
            )

    def revoke_session(self, *, token: str, actor_user_id: UUID | None = None) -> None:
        now: datetime = _utc_now()
        with self._engine.begin() as connection:
            user_id_value: str | None = connection.execute(
                select(_SESSIONS.c.user_id).where(_SESSIONS.c.token_hash == _token_hash(token))
            ).scalar_one_or_none()
            if user_id_value is None:
                return
            connection.execute(
                update(_SESSIONS)
                .where(_SESSIONS.c.token_hash == _token_hash(token))
                .values(revoked_at=now)
            )
            self._audit(
                connection=connection,
                operation="session.revoked",
                actor_user_id=actor_user_id,
                affected_user_id=UUID(user_id_value),
                details={},
                now=now,
            )
        self._invalidate_authentication()

    def set_user_active(
        self,
        *,
        user_id: UUID,
        is_active: bool,
        actor_user_id: UUID | None,
    ) -> UserAccount:
        now: datetime = _utc_now()
        with self._engine.begin() as connection:
            if not is_active and self._is_last_admin(connection=connection, user_id=user_id):
                raise AccountConflictError("Cannot disable the last active administrator")
            affected_rows: int | None = connection.execute(
                update(_USERS)
                .where(_USERS.c.id == str(user_id))
                .values(is_active=is_active, updated_at=now)
            ).rowcount
            if affected_rows != 1:
                raise AccountNotFoundError(f"User '{user_id}' was not found")
            if not is_active:
                connection.execute(
                    update(_SESSIONS)
                    .where(_SESSIONS.c.user_id == str(user_id), _SESSIONS.c.revoked_at.is_(None))
                    .values(revoked_at=now)
                )
            self._audit(
                connection=connection,
                operation="user.enabled" if is_active else "user.disabled",
                actor_user_id=actor_user_id,
                affected_user_id=user_id,
                details={},
                now=now,
            )
        self._invalidate_authentication()
        account: UserAccount | None = self.get_user_by_id(user_id=user_id)
        if account is None:
            raise AccountNotFoundError(f"User '{user_id}' was not found")
        return account

    def update_profile(
        self,
        *,
        user_id: UUID,
        display_name: str | None,
        email: str | None,
        actor_user_id: UUID | None,
    ) -> UserAccount:
        now: datetime = _utc_now()
        with self._engine.begin() as connection:
            affected_rows: int | None = connection.execute(
                update(_USERS)
                .where(_USERS.c.id == str(user_id))
                .values(
                    display_name=_clean_optional(display_name),
                    email=_clean_optional(email),
                    updated_at=now,
                )
            ).rowcount
            if affected_rows != 1:
                raise AccountNotFoundError(f"User '{user_id}' was not found")
            self._audit(
                connection=connection,
                operation="user.profile_updated",
                actor_user_id=actor_user_id,
                affected_user_id=user_id,
                details={},
                now=now,
            )
        self._invalidate_authentication()
        account: UserAccount | None = self.get_user_by_id(user_id=user_id)
        if account is None:
            raise AccountNotFoundError(f"User '{user_id}' was not found")
        return account

    def update_account(
        self,
        *,
        user_id: UUID,
        display_name: str | None,
        email: str | None,
        is_active: bool,
        actor_user_id: UUID | None,
    ) -> UserAccount:
        """Apply one complete account edit atomically, including session revocation."""

        now: datetime = _utc_now()
        with self._engine.begin() as connection:
            current: RowMapping | None = (
                connection.execute(select(_USERS).where(_USERS.c.id == str(user_id)))
                .mappings()
                .one_or_none()
            )
            if current is None:
                raise AccountNotFoundError(f"User '{user_id}' was not found")
            if not is_active and bool(current["is_active"]):
                if self._is_last_admin(connection=connection, user_id=user_id):
                    raise AccountConflictError("Cannot disable the last active administrator")
            connection.execute(
                update(_USERS)
                .where(_USERS.c.id == str(user_id))
                .values(
                    display_name=_clean_optional(display_name),
                    email=_clean_optional(email),
                    is_active=is_active,
                    updated_at=now,
                )
            )
            if not is_active:
                connection.execute(
                    update(_SESSIONS)
                    .where(_SESSIONS.c.user_id == str(user_id), _SESSIONS.c.revoked_at.is_(None))
                    .values(revoked_at=now)
                )
            self._audit(
                connection=connection,
                operation="user.updated",
                actor_user_id=actor_user_id,
                affected_user_id=user_id,
                details={"active": is_active},
                now=now,
            )
        self._invalidate_authentication()
        account: UserAccount | None = self.get_user_by_id(user_id=user_id)
        if account is None:
            raise AccountNotFoundError(f"User '{user_id}' was not found")
        return account

    def reset_password(
        self,
        *,
        user_id: UUID,
        password: str,
        actor_user_id: UUID | None,
    ) -> None:
        password_hash: str = hash_password(password)
        now: datetime = _utc_now()
        with self._engine.begin() as connection:
            present: str | None = connection.execute(
                select(_USERS.c.id).where(_USERS.c.id == str(user_id))
            ).scalar_one_or_none()
            if present is None:
                raise AccountNotFoundError(f"User '{user_id}' was not found")
            credential_user: str | None = connection.execute(
                select(_PASSWORD_CREDENTIALS.c.user_id).where(
                    _PASSWORD_CREDENTIALS.c.user_id == str(user_id)
                )
            ).scalar_one_or_none()
            if credential_user is None:
                connection.execute(
                    insert(_PASSWORD_CREDENTIALS).values(
                        user_id=str(user_id),
                        password_hash=password_hash,
                        password_changed_at=now,
                    )
                )
            else:
                connection.execute(
                    update(_PASSWORD_CREDENTIALS)
                    .where(_PASSWORD_CREDENTIALS.c.user_id == str(user_id))
                    .values(password_hash=password_hash, password_changed_at=now)
                )
            connection.execute(
                update(_SESSIONS)
                .where(_SESSIONS.c.user_id == str(user_id), _SESSIONS.c.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            self._audit(
                connection=connection,
                operation="password.reset",
                actor_user_id=actor_user_id,
                affected_user_id=user_id,
                details={},
                now=now,
            )
        self._invalidate_authentication()

    def grant_role(
        self,
        *,
        user_id: UUID,
        role_name: str,
        actor_user_id: UUID | None,
    ) -> UserAccount:
        now: datetime = _utc_now()
        try:
            with self._engine.begin() as connection:
                self._assign_role(
                    connection=connection,
                    user_id=user_id,
                    role_name=role_name,
                    actor_user_id=actor_user_id,
                    now=now,
                )
                self._audit(
                    connection=connection,
                    operation="role.granted",
                    actor_user_id=actor_user_id,
                    affected_user_id=user_id,
                    details={"role": role_name},
                    now=now,
                )
        except IntegrityError as error:
            raise AccountConflictError(
                f"Role '{role_name}' is already assigned or does not exist"
            ) from error
        self._invalidate_authentication()
        account: UserAccount | None = self.get_user_by_id(user_id=user_id)
        if account is None:
            raise AccountNotFoundError(f"User '{user_id}' was not found")
        return account

    def revoke_role(
        self,
        *,
        user_id: UUID,
        role_name: str,
        actor_user_id: UUID | None,
    ) -> UserAccount:
        now: datetime = _utc_now()
        with self._engine.begin() as connection:
            if role_name == ADMIN_ROLE and self._is_last_admin(
                connection=connection, user_id=user_id
            ):
                raise AccountConflictError("Cannot remove the last active administrator")
            affected_rows: int | None = connection.execute(
                delete(_USER_ROLES).where(
                    _USER_ROLES.c.user_id == str(user_id),
                    _USER_ROLES.c.role_name == role_name,
                )
            ).rowcount
            if affected_rows != 1:
                raise AccountNotFoundError(f"Role '{role_name}' is not assigned to user")
            self._audit(
                connection=connection,
                operation="role.revoked",
                actor_user_id=actor_user_id,
                affected_user_id=user_id,
                details={"role": role_name},
                now=now,
            )
        self._invalidate_authentication()
        account: UserAccount | None = self.get_user_by_id(user_id=user_id)
        if account is None:
            raise AccountNotFoundError(f"User '{user_id}' was not found")
        return account

    def grant_project_role(
        self,
        *,
        user_id: UUID,
        project_name: str,
        role_name: str,
        target_name: str | None,
        actor_user_id: UUID | None,
    ) -> ProjectRoleAssignment:
        """Grant one project-authored role at all targets or one explicit target."""

        clean_project: str = _required_assignment_name(
            value=project_name, label="Project name", maximum_length=256
        )
        clean_role: str = _required_assignment_name(
            value=role_name, label="Role name", maximum_length=128
        )
        clean_target: str | None = (
            None
            if target_name is None
            else _required_assignment_name(
                value=target_name, label="Target name", maximum_length=128
            )
        )
        assignment_id: UUID = uuid4()
        now: datetime = _utc_now()
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(_PROJECT_ROLE_ASSIGNMENTS).values(
                        id=str(assignment_id),
                        user_id=str(user_id),
                        project_name=clean_project,
                        role_name=clean_role,
                        target_name=clean_target,
                        assignment_scope=clean_target or "",
                        assigned_by=None if actor_user_id is None else str(actor_user_id),
                        assigned_at=now,
                        revoked_by=None,
                        revoked_at=None,
                        active_key="active",
                    )
                )
                self._audit(
                    connection=connection,
                    operation="project_role.granted",
                    actor_user_id=actor_user_id,
                    affected_user_id=user_id,
                    details={
                        "projectName": clean_project,
                        "role": clean_role,
                        "targetName": clean_target,
                    },
                    now=now,
                )
        except IntegrityError as error:
            raise AccountConflictError(
                f"Role '{clean_role}' is already assigned for the requested project scope "
                "or the user does not exist"
            ) from error
        return ProjectRoleAssignment(
            assignment_id=assignment_id,
            user_id=user_id,
            project_name=clean_project,
            role_name=clean_role,
            target_name=clean_target,
            assigned_by=actor_user_id,
            assigned_at=now,
        )

    def revoke_project_role(
        self, *, assignment_id: UUID, actor_user_id: UUID | None
    ) -> ProjectRoleAssignment:
        """Soft-revoke one exact project-role assignment and retain its history."""

        now: datetime = _utc_now()
        with self._engine.begin() as connection:
            row: RowMapping | None = (
                connection.execute(
                    select(_PROJECT_ROLE_ASSIGNMENTS).where(
                        _PROJECT_ROLE_ASSIGNMENTS.c.id == str(assignment_id),
                        _PROJECT_ROLE_ASSIGNMENTS.c.revoked_at.is_(None),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise AccountNotFoundError(
                    f"Project role assignment '{assignment_id}' was not found"
                )
            connection.execute(
                update(_PROJECT_ROLE_ASSIGNMENTS)
                .where(_PROJECT_ROLE_ASSIGNMENTS.c.id == str(assignment_id))
                .values(
                    revoked_by=None if actor_user_id is None else str(actor_user_id),
                    revoked_at=now,
                    active_key=str(assignment_id),
                )
            )
            self._audit(
                connection=connection,
                operation="project_role.revoked",
                actor_user_id=actor_user_id,
                affected_user_id=UUID(str(row["user_id"])),
                details={
                    "projectName": str(row["project_name"]),
                    "role": str(row["role_name"]),
                    "targetName": row["target_name"],
                },
                now=now,
            )
        return _project_role_assignment_from_row(
            row=row,
            revoked_by=actor_user_id,
            revoked_at=now,
        )

    def list_project_role_assignments(
        self,
        *,
        user_id: UUID,
        project_name: str,
        include_revoked: bool = False,
    ) -> tuple[ProjectRoleAssignment, ...]:
        """List deterministic project memberships for one user."""

        statement: Select[tuple[object, ...]] = select(_PROJECT_ROLE_ASSIGNMENTS).where(
            _PROJECT_ROLE_ASSIGNMENTS.c.user_id == str(user_id),
            _PROJECT_ROLE_ASSIGNMENTS.c.project_name == project_name,
        )
        if not include_revoked:
            statement = statement.where(_PROJECT_ROLE_ASSIGNMENTS.c.revoked_at.is_(None))
        statement = statement.order_by(
            _PROJECT_ROLE_ASSIGNMENTS.c.role_name,
            _PROJECT_ROLE_ASSIGNMENTS.c.assignment_scope,
            _PROJECT_ROLE_ASSIGNMENTS.c.assigned_at,
        )
        with self._engine.connect() as connection:
            rows: list[RowMapping] = list(connection.execute(statement).mappings())
        return tuple(_project_role_assignment_from_row(row=row) for row in rows)

    def list_audit_records(self) -> tuple[AccountAuditRecord, ...]:
        with self._engine.connect() as connection:
            rows: list[RowMapping] = list(
                connection.execute(select(_ACCOUNT_AUDIT).order_by(_ACCOUNT_AUDIT.c.id)).mappings()
            )
            return tuple(
                AccountAuditRecord(
                    operation=str(row["operation"]),
                    actor_user_id=(
                        None if row["actor_user_id"] is None else UUID(str(row["actor_user_id"]))
                    ),
                    affected_user_id=(
                        None
                        if row["affected_user_id"] is None
                        else UUID(str(row["affected_user_id"]))
                    ),
                    occurred_at=_as_utc(row["occurred_at"]),
                    details=json.loads(str(row["details_json"])),
                )
                for row in rows
            )

    def prune_sessions(self) -> int:
        now: datetime = _utc_now()
        with self._engine.begin() as connection:
            affected_rows: int | None = connection.execute(
                delete(_SESSIONS).where(
                    (_SESSIONS.c.expires_at <= now) | (_SESSIONS.c.revoked_at.is_not(None))
                )
            ).rowcount
            return int(affected_rows or 0)

    def _seed_system_roles(self, connection: Connection) -> None:
        existing: set[str] = set(connection.execute(select(_ROLES.c.name)).scalars())
        for name, description in {
            VIEWER_ROLE: "Authenticated project viewer",
            ADMIN_ROLE: "SQLTide system administrator",
        }.items():
            if name not in existing:
                connection.execute(
                    insert(_ROLES).values(name=name, description=description, is_system=True)
                )

    def _assign_role(
        self,
        *,
        connection: Connection,
        user_id: UUID,
        role_name: str,
        actor_user_id: UUID | None,
        now: datetime,
    ) -> None:
        role_present: str | None = connection.execute(
            select(_ROLES.c.name).where(_ROLES.c.name == role_name)
        ).scalar_one_or_none()
        if role_present is None:
            raise AccountNotFoundError(f"Role '{role_name}' was not found")
        connection.execute(
            insert(_USER_ROLES).values(
                user_id=str(user_id),
                role_name=role_name,
                assigned_at=now,
                assigned_by=None if actor_user_id is None else str(actor_user_id),
            )
        )

    def _create_session(
        self,
        *,
        connection: Connection,
        user_id: UUID,
        ttl_seconds: int,
        now: datetime,
    ) -> SessionCredentials:
        token: str = secrets.token_urlsafe(48)
        csrf_token: str = secrets.token_urlsafe(32)
        expires_at: datetime = now + timedelta(seconds=ttl_seconds)
        connection.execute(
            insert(_SESSIONS).values(
                token_hash=_token_hash(token),
                user_id=str(user_id),
                csrf_token=csrf_token,
                created_at=now,
                last_seen_at=now,
                expires_at=expires_at,
                revoked_at=None,
            )
        )
        return SessionCredentials(token=token, csrf_token=csrf_token, expires_at=expires_at)

    def _account_from_row(self, *, connection: Connection, row: RowMapping) -> UserAccount:
        user_id: UUID = UUID(str(row["id"]))
        roles: tuple[str, ...] = self._roles_for_user(connection=connection, user_id=user_id)
        sources: tuple[AuthenticationSource, ...] = tuple(
            AuthenticationSource(value)
            for value in connection.execute(
                select(_EXTERNAL_IDENTITIES.c.source)
                .where(_EXTERNAL_IDENTITIES.c.user_id == str(user_id))
                .order_by(_EXTERNAL_IDENTITIES.c.source)
            ).scalars()
        )
        password_present: bool = (
            connection.execute(
                select(_PASSWORD_CREDENTIALS.c.user_id).where(
                    _PASSWORD_CREDENTIALS.c.user_id == str(user_id)
                )
            ).scalar_one_or_none()
            is not None
        )
        all_sources: tuple[AuthenticationSource, ...] = sources + (
            (AuthenticationSource.PASSWORD,) if password_present else ()
        )
        return UserAccount(
            user_id=user_id,
            username=str(row["username"]),
            display_name=None if row["display_name"] is None else str(row["display_name"]),
            email=None if row["email"] is None else str(row["email"]),
            is_active=bool(row["is_active"]),
            created_at=_as_utc(row["created_at"]),
            updated_at=_as_utc(row["updated_at"]),
            roles=roles,
            authentication_sources=tuple(dict.fromkeys(all_sources)),
        )

    def _roles_for_user(self, *, connection: Connection, user_id: UUID) -> tuple[str, ...]:
        return tuple(
            connection.execute(
                select(_USER_ROLES.c.role_name)
                .where(_USER_ROLES.c.user_id == str(user_id))
                .order_by(_USER_ROLES.c.role_name)
            ).scalars()
        )

    def _invalidate_authentication(self) -> None:
        self._authentication_revision += 1

    def _is_last_admin(self, *, connection: Connection, user_id: UUID) -> bool:
        connection.execute(
            select(_ROLES.c.name).where(_ROLES.c.name == ADMIN_ROLE).with_for_update()
        ).scalar_one()
        is_admin: bool = (
            connection.execute(
                select(_USER_ROLES.c.user_id).where(
                    _USER_ROLES.c.user_id == str(user_id),
                    _USER_ROLES.c.role_name == ADMIN_ROLE,
                )
            ).scalar_one_or_none()
            is not None
        )
        if not is_admin:
            return False
        active_admin_count: int = int(
            connection.execute(
                select(func.count())
                .select_from(_USERS)
                .join(_USER_ROLES, _USER_ROLES.c.user_id == _USERS.c.id)
                .where(_USERS.c.is_active.is_(True), _USER_ROLES.c.role_name == ADMIN_ROLE)
            ).scalar_one()
        )
        return active_admin_count <= 1

    def _audit(
        self,
        *,
        connection: Connection,
        operation: str,
        actor_user_id: UUID | None,
        affected_user_id: UUID | None,
        details: Mapping[str, object],
        now: datetime,
    ) -> None:
        connection.execute(
            insert(_ACCOUNT_AUDIT).values(
                operation=operation,
                actor_user_id=None if actor_user_id is None else str(actor_user_id),
                affected_user_id=None if affected_user_id is None else str(affected_user_id),
                occurred_at=now,
                details_json=json.dumps(dict(details), sort_keys=True),
            )
        )


def _principal_from_row(*, row: RowMapping, source: AuthenticationSource) -> Principal:
    return Principal(
        user_id=UUID(str(row["id"])),
        username=str(row["username"]),
        display_name=None if row["display_name"] is None else str(row["display_name"]),
        email=None if row["email"] is None else str(row["email"]),
        authentication_source=source,
    )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ControlStoreError(f"Expected database timestamp, received {type(value).__name__}")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned: str = value.strip()
    return cleaned or None


def _required_assignment_name(*, value: str, label: str, maximum_length: int) -> str:
    cleaned: str = value.strip()
    if not cleaned:
        raise AccountValidationError(f"{label} must not be empty")
    if len(cleaned) > maximum_length:
        raise AccountValidationError(f"{label} must contain at most {maximum_length} characters")
    return cleaned


def _project_role_assignment_from_row(
    *,
    row: RowMapping,
    revoked_by: UUID | None = None,
    revoked_at: datetime | None = None,
) -> ProjectRoleAssignment:
    stored_revoked_by: object = row["revoked_by"]
    stored_revoked_at: object = row["revoked_at"]
    assigned_by: object = row["assigned_by"]
    return ProjectRoleAssignment(
        assignment_id=UUID(str(row["id"])),
        user_id=UUID(str(row["user_id"])),
        project_name=str(row["project_name"]),
        role_name=str(row["role_name"]),
        target_name=None if row["target_name"] is None else str(row["target_name"]),
        assigned_by=None if assigned_by is None else UUID(str(assigned_by)),
        assigned_at=_as_utc(row["assigned_at"]),
        revoked_by=(
            revoked_by
            if revoked_by is not None
            else None
            if stored_revoked_by is None
            else UUID(str(stored_revoked_by))
        ),
        revoked_at=(
            revoked_at
            if revoked_at is not None
            else None
            if stored_revoked_at is None
            else _as_utc(stored_revoked_at)
        ),
    )


def _enable_sqlite_foreign_keys(*arguments: object) -> None:
    dbapi_connection: sqlite3.Connection = cast(sqlite3.Connection, arguments[0])
    cursor: sqlite3.Cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()
