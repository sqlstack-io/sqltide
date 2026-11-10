"""Durable relational storage for actor-bound destruction plans."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from sqltide._native import (
    deserialize_destruction_plan,
    rename_streambuild_tables,
    serialize_destruction_plan,
)
from sqltide.auth.constants import SQLITE_MEMORY_PATH
from sqltide.executor.destruction.constants import DESTRUCTION_PLAN_PAYLOAD_VERSION
from sqltide.executor.destruction.exceptions import (
    DestructionChallengeError,
    DestructionPlanCorruptError,
    DestructionPlanExpiredError,
    DestructionPlanNotFoundError,
    DestructionPlanNotReviewedError,
    DestructionValidationError,
)
from sqltide.executor.destruction.models import (
    DestructionPlan,
    RelationalStoredDestructionPlan,
)
from sqltide.executor.destruction.types import DestructionClock

_STORE_SCHEMA_COMPONENT: str = "destruction_plans"
_STORE_SCHEMA_VERSION: int = 1
_STATUS_PENDING: str = "pending"
_STATUS_REVIEWED: str = "reviewed"
_STATUS_CONSUMED: str = "consumed"
_STATUS_EXPIRED: str = "expired"

_METADATA: MetaData = MetaData()
_SCHEMA_VERSIONS: Table = Table(
    "sqltide_schema_versions",
    _METADATA,
    Column("component", String(64), primary_key=True),
    Column("version", Integer, nullable=False),
)
_DESTRUCTION_PLANS: Table = Table(
    "sqltide_destruction_plans",
    _METADATA,
    Column("plan_id", String(128), primary_key=True),
    Column("actor", String(128), nullable=False),
    Column("payload_version", Integer, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("reviewed_at", DateTime(timezone=True)),
    Column("consumed_at", DateTime(timezone=True)),
    Column("status", String(16), nullable=False),
)


class RelationalDestructionPlanStore:
    """SQLite/PostgreSQL store with durable review and atomic consumption state."""

    def __init__(self, *, url: str, clock: DestructionClock | None = None) -> None:
        self._clock: DestructionClock = clock or (lambda: datetime.now(tz=UTC))
        self._url: str = url
        if url.startswith("sqlite:///"):
            path_value: str = url.removeprefix("sqlite:///")
            if path_value != SQLITE_MEMORY_PATH:
                Path(path_value).parent.mkdir(parents=True, exist_ok=True)
        connect_args: dict[str, object] = (
            {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite:") else {}
        )
        try:
            self._engine: Engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
            self.bootstrap()
        except (SQLAlchemyError, OSError) as error:
            raise DestructionValidationError(
                f"Could not initialize destruction plan store: {error}"
            ) from error

    def close(self) -> None:
        self._engine.dispose()

    def bootstrap(self) -> None:
        """Create the additive schema component and reject incompatible versions."""

        rename_streambuild_tables(url=self._url)
        _METADATA.create_all(self._engine)
        try:
            with self._engine.begin() as connection:
                current: int | None = connection.execute(
                    select(_SCHEMA_VERSIONS.c.version).where(
                        _SCHEMA_VERSIONS.c.component == _STORE_SCHEMA_COMPONENT
                    )
                ).scalar_one_or_none()
                if current is None:
                    connection.execute(
                        insert(_SCHEMA_VERSIONS).values(
                            component=_STORE_SCHEMA_COMPONENT,
                            version=_STORE_SCHEMA_VERSION,
                        )
                    )
                else:
                    _require_schema_version(current)
        except IntegrityError:
            with self._engine.connect() as connection:
                raced: int | None = connection.execute(
                    select(_SCHEMA_VERSIONS.c.version).where(
                        _SCHEMA_VERSIONS.c.component == _STORE_SCHEMA_COMPONENT
                    )
                ).scalar_one_or_none()
            _require_schema_version(raced)

    def save(self, *, plan: DestructionPlan, actor: str) -> None:
        if not actor:
            raise DestructionValidationError("Destruction plan actor must not be empty")
        payload_json: str = serialize_destruction_plan(plan)
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(_DESTRUCTION_PLANS).values(
                        plan_id=plan.plan_id,
                        actor=actor,
                        payload_version=DESTRUCTION_PLAN_PAYLOAD_VERSION,
                        payload_json=payload_json,
                        payload_sha256=_payload_sha256(payload_json),
                        created_at=plan.created_at,
                        expires_at=plan.expires_at,
                        reviewed_at=None,
                        consumed_at=None,
                        status=_STATUS_PENDING,
                    )
                )
        except IntegrityError as error:
            raise DestructionValidationError(
                f"Destruction plan {plan.plan_id!r} already exists"
            ) from error

    def get(self, *, plan_id: str, actor: str) -> DestructionPlan:
        return self._require_current(plan_id=plan_id, actor=actor).plan

    def mark_reviewed(self, *, plan_id: str, actor: str) -> datetime:
        stored: RelationalStoredDestructionPlan = self._require_current(
            plan_id=plan_id, actor=actor
        )
        now: datetime = self._clock()
        with self._engine.begin() as connection:
            affected: int | None = connection.execute(
                update(_DESTRUCTION_PLANS)
                .where(
                    _DESTRUCTION_PLANS.c.plan_id == plan_id,
                    _DESTRUCTION_PLANS.c.actor == actor,
                    _DESTRUCTION_PLANS.c.status.in_((_STATUS_PENDING, _STATUS_REVIEWED)),
                    _DESTRUCTION_PLANS.c.expires_at > now,
                    _DESTRUCTION_PLANS.c.payload_json == stored.payload_json,
                    _DESTRUCTION_PLANS.c.payload_sha256 == stored.payload_sha256,
                )
                .values(status=_STATUS_REVIEWED, reviewed_at=now)
            ).rowcount
        if affected != 1:
            _ = self._require_current(plan_id=plan_id, actor=actor)
            raise DestructionPlanNotFoundError(
                f"Destruction plan {plan_id!r} changed during review"
            )
        return now

    def reviewed_at(self, *, plan_id: str, actor: str) -> datetime:
        stored: RelationalStoredDestructionPlan = self._require_current(
            plan_id=plan_id, actor=actor
        )
        if stored.reviewed_at is None:
            raise DestructionPlanNotReviewedError(
                f"Destruction plan {plan_id!r} has not been reviewed"
            )
        return stored.reviewed_at

    def consume(
        self,
        *,
        plan_id: str,
        challenge_responses: tuple[str, ...],
        actor: str,
    ) -> DestructionPlan:
        stored: RelationalStoredDestructionPlan = self._require_current(
            plan_id=plan_id, actor=actor
        )
        if stored.reviewed_at is None:
            raise DestructionPlanNotReviewedError(
                f"Destruction plan {plan_id!r} has not been reviewed"
            )
        if challenge_responses != stored.plan.challenges:
            raise DestructionChallengeError(
                "Challenge responses must exactly match the frozen plan in order"
            )
        now: datetime = self._clock()
        with self._engine.begin() as connection:
            affected: int | None = connection.execute(
                update(_DESTRUCTION_PLANS)
                .where(
                    _DESTRUCTION_PLANS.c.plan_id == plan_id,
                    _DESTRUCTION_PLANS.c.actor == actor,
                    _DESTRUCTION_PLANS.c.status == _STATUS_REVIEWED,
                    _DESTRUCTION_PLANS.c.reviewed_at.is_not(None),
                    _DESTRUCTION_PLANS.c.consumed_at.is_(None),
                    _DESTRUCTION_PLANS.c.expires_at > now,
                    _DESTRUCTION_PLANS.c.payload_json == stored.payload_json,
                    _DESTRUCTION_PLANS.c.payload_sha256 == stored.payload_sha256,
                )
                .values(status=_STATUS_CONSUMED, consumed_at=now)
            ).rowcount
        if affected != 1:
            _ = self._require_current(plan_id=plan_id, actor=actor)
            raise DestructionPlanNotFoundError(f"Destruction plan {plan_id!r} was already consumed")
        return stored.plan

    def _require_current(self, *, plan_id: str, actor: str) -> RelationalStoredDestructionPlan:
        stored: RelationalStoredDestructionPlan = self._read(plan_id=plan_id, actor=actor)
        if stored.status == _STATUS_CONSUMED:
            raise DestructionPlanNotFoundError(f"Destruction plan {plan_id!r} was not found")
        now: datetime = self._clock()
        if stored.status == _STATUS_EXPIRED or now >= stored.plan.expires_at:
            self._mark_expired(plan_id=plan_id, actor=actor, now=now)
            raise DestructionPlanExpiredError(f"Destruction plan {plan_id!r} has expired")
        return stored

    def _read(self, *, plan_id: str, actor: str) -> RelationalStoredDestructionPlan:
        with self._engine.connect() as connection:
            row: RowMapping | None = (
                connection.execute(
                    select(_DESTRUCTION_PLANS).where(
                        _DESTRUCTION_PLANS.c.plan_id == plan_id,
                        _DESTRUCTION_PLANS.c.actor == actor,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise DestructionPlanNotFoundError("Destruction plan was not found for this actor")
        return _stored_plan_from_row(row=row, expected_plan_id=plan_id)

    def _mark_expired(self, *, plan_id: str, actor: str, now: datetime) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                update(_DESTRUCTION_PLANS)
                .where(
                    _DESTRUCTION_PLANS.c.plan_id == plan_id,
                    _DESTRUCTION_PLANS.c.actor == actor,
                    _DESTRUCTION_PLANS.c.status.in_((_STATUS_PENDING, _STATUS_REVIEWED)),
                    _DESTRUCTION_PLANS.c.expires_at <= now,
                )
                .values(status=_STATUS_EXPIRED)
            )


def _stored_plan_from_row(
    *, row: RowMapping, expected_plan_id: str
) -> RelationalStoredDestructionPlan:
    payload_json: str = str(row["payload_json"])
    payload_sha256: str = str(row["payload_sha256"])
    if int(row["payload_version"]) != DESTRUCTION_PLAN_PAYLOAD_VERSION:
        raise DestructionPlanCorruptError("Stored destruction plan payload version is incompatible")
    if not hmac.compare_digest(payload_sha256, _payload_sha256(payload_json)):
        raise DestructionPlanCorruptError("Stored destruction plan payload checksum does not match")
    plan: DestructionPlan = deserialize_destruction_plan(payload_json)
    created_at: datetime = _as_utc(row["created_at"])
    expires_at: datetime = _as_utc(row["expires_at"])
    if (
        plan.plan_id != expected_plan_id
        or plan.created_at != created_at
        or plan.expires_at != expires_at
    ):
        raise DestructionPlanCorruptError(
            "Stored destruction plan identity or timestamps do not match its payload"
        )
    status: str = str(row["status"])
    reviewed_at: datetime | None = _optional_utc(row["reviewed_at"])
    consumed_at: datetime | None = _optional_utc(row["consumed_at"])
    valid_state: bool = (
        (status == _STATUS_PENDING and reviewed_at is None and consumed_at is None)
        or (status == _STATUS_REVIEWED and reviewed_at is not None and consumed_at is None)
        or (status == _STATUS_CONSUMED and reviewed_at is not None and consumed_at is not None)
        or (status == _STATUS_EXPIRED and consumed_at is None)
    )
    if not valid_state:
        raise DestructionPlanCorruptError("Stored destruction plan lifecycle state is corrupt")
    return RelationalStoredDestructionPlan(
        plan=plan,
        payload_json=payload_json,
        payload_sha256=payload_sha256,
        status=status,
        reviewed_at=reviewed_at,
        consumed_at=consumed_at,
    )


def _require_schema_version(version: int | None) -> None:
    if version != _STORE_SCHEMA_VERSION:
        raise DestructionValidationError(
            "SQLTide destruction plan schema is incompatible: "
            f"found version {version}, expected {_STORE_SCHEMA_VERSION}"
        )


def _payload_sha256(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _optional_utc(value: object) -> datetime | None:
    return None if value is None else _as_utc(value)


def _as_utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise DestructionPlanCorruptError(
            f"Expected destruction plan timestamp, received {type(value).__name__}"
        )
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
