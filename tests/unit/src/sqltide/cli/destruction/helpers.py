from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from sqltide._native import InMemoryDestructionPlanStore
from sqltide.auth.models import UserAccount
from sqltide.cli.destruction.models import DestructionCommandOptions
from sqltide.executor.destruction.models import (
    DestructionPlan,
    DestructionRelationEvidence,
)
from sqltide.executor.destruction.types import (
    DestructionOperation,
    DestructionOwnership,
    DestructionRelationKind,
)

DESTRUCTION_ADMIN_ID: UUID = UUID("d61cf1a8-9a62-49f8-a0c4-f43cfd7dd41c")


def destruction_account(
    *,
    user_id: UUID = DESTRUCTION_ADMIN_ID,
    active: bool = True,
    roles: tuple[str, ...] = ("admin",),
) -> UserAccount:
    timestamp: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    return UserAccount(
        user_id=user_id,
        username="persisted-admin",
        display_name=None,
        email=None,
        is_active=active,
        created_at=timestamp,
        updated_at=timestamp,
        roles=roles,
    )


def destruction_options() -> DestructionCommandOptions:
    return DestructionCommandOptions(
        operation=DestructionOperation.DESTROY_PIPELINES,
        pipelines_root=Path("/project/pipelines"),
        project_dir=Path("/project"),
        selected_target="non-default-target",
        database="analytics",
        selectors=("pipeline:alpha",),
        control_store_url="sqlite:////custom/control.db",
        cli_variables=(("resource_suffix", "_cli"),),
        environment={"RESOURCE_SCHEMA": "environment_schema"},
    )


def use_process_local_plan_store_for_unit_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sqltide.executor.destruction.classes.relational_destruction_plan_store.RelationalDestructionPlanStore",
        MagicMock(return_value=InMemoryDestructionPlanStore()),
    )


def destruction_plan() -> DestructionPlan:
    created_at: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    return DestructionPlan(
        plan_id="destruction_reviewed",
        operation=DestructionOperation.DESTROY_PIPELINES,
        target="uat",
        database="analytics",
        metadata_database="analytics",
        requested_pipeline_names=("alpha",),
        included_dependent_pipeline_names=(),
        affected_pipeline_names=("alpha",),
        affected_model_names=("orders",),
        affected_source_names=(),
        relations=(
            DestructionRelationEvidence(
                database="analytics",
                name="tbl__orders",
                kind=DestructionRelationKind.TABLE,
                exists=True,
                total_bytes=2048,
                active_parts=2,
                catalog_fingerprint="catalog-sha",
                logical_names=("orders",),
                pipeline_names=("alpha",),
                ownership=(DestructionOwnership.CURRENT_MANIFEST,),
                dependency_relation_names=(),
            ),
        ),
        challenges=("alpha",),
        preserves_sources=True,
        preserves_replay_data=True,
        manifest_fingerprint="manifest-sha",
        plan_fingerprint="plan-sha",
        created_at=created_at,
        expires_at=datetime(2099, 8, 24, 12, 15, tzinfo=UTC),
        relation_drop_size_policy_observed=True,
    )
