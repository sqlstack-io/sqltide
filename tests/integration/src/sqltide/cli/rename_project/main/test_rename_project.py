import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from clickhouse_connect.driver.client import Client

import sqltide._native as _native
from sqltide._native import (
    ControlStore,
    analyze_project,
    build_compiler_adapter_profile,
    build_state_payload,
    run_cli,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterManifestSnapshot
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.auth.models import ProjectRoleAssignment
from tests.integration.src.sqltide.cli.helpers import build_managed_clickhouse_client
from tests.integration.src.sqltide.cli.rename_project.main._test_types import (
    ConsumerGroupDriftTestCase,
    MultiTargetRenameTestCase,
    ProjectRenameConflictTestCase,
    ProjectRenameTestCase,
    RoleAssignmentConflictTestCase,
    RoleAssignmentRenameTestCase,
)
from tests.integration.src.sqltide.cli.rename_project.main.helpers import (
    PROJECT_CONFIG,
    TWO_TARGET_PROJECT_CONFIG,
    built_shop_project,
    catalog_definitions,
    destroy_history_ids,
    kafka_group_name,
    manifest_ids,
    plan_json,
    role_names,
    target_arguments,
    write_shop_project,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ProjectRenameTestCase(
            description="keeps groups, plan and manifest history; repeats are no-ops",
            old_name="shop",
            new_name="storefront",
            expected_config=PROJECT_CONFIG.replace(
                'name = "shop"  # project identity\n',
                'name = "storefront"  # project identity\n'
                "# Keeps the Kafka consumer groups this project used before it was renamed.\n"
                'consumer_group_identity = "shop"\n',
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_built_project_when_renaming_then_live_state_carries_over(
    test_case: ProjectRenameTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_dir, arguments = built_shop_project(
        tmp_path=tmp_path,
        settings=clickhouse_connection_settings,
        database=clickhouse_database,
        monkeypatch=monkeypatch,
    )
    config_path: Path = project_dir / "sqltide_project.toml"
    plan_before: str = plan_json(arguments, capsys)
    catalog_before: tuple[tuple[str, str], ...] = catalog_definitions(
        clickhouse_client, database=clickhouse_database
    )
    group_before: str = kafka_group_name(clickhouse_client, database=clickhouse_database)
    recorded: tuple[str, ...] = manifest_ids(
        clickhouse_client, database=clickhouse_database, identity=test_case.old_name
    )

    assert run_cli(("sqt", "rename-project", test_case.new_name, *arguments)) == 0
    dry_run: str = capsys.readouterr().out
    assert config_path.read_text(encoding="utf-8") == PROJECT_CONFIG
    assert (
        manifest_ids(clickhouse_client, database=clickhouse_database, identity=test_case.new_name)
        == ()
    )
    assert "Kafka consumer groups: 1 unchanged" in dry_run
    assert f"{len(recorded)} of {len(recorded)} manifest(s) to copy" in dry_run
    assert "Dry run: nothing was changed." in dry_run

    assert run_cli(("sqt", "rename-project", test_case.new_name, "--apply", *arguments)) == 0
    renamed_config: str = config_path.read_text(encoding="utf-8")
    assert renamed_config == test_case.expected_config
    assert kafka_group_name(clickhouse_client, database=clickhouse_database) == group_before
    assert catalog_definitions(clickhouse_client, database=clickhouse_database) == catalog_before
    assert plan_json(arguments, capsys) == plan_before
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    destroy_history: AdapterManifestSnapshot = connection.load_manifests(
        database=clickhouse_database,
        project_identity=test_case.new_name,
        target_name="test",
        target_database=clickhouse_database,
    )
    assert tuple(sorted(manifest.manifest_id for manifest in destroy_history.manifests)) == (
        recorded
    )

    assert run_cli(("sqt", "rename-project", test_case.new_name, "--apply", *arguments)) == 1
    assert "already named" in capsys.readouterr().err
    config_path.write_text(PROJECT_CONFIG, encoding="utf-8")
    assert run_cli(("sqt", "rename-project", test_case.new_name, "--apply", *arguments)) == 0
    assert f"0 of {len(recorded)} manifest(s) copied" in capsys.readouterr().out
    assert config_path.read_text(encoding="utf-8") == renamed_config
    assert (
        manifest_ids(clickhouse_client, database=clickhouse_database, identity=test_case.new_name)
        == recorded
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ProjectRenameConflictTestCase(
            description="refuses when the new name already has its own manifest history",
            new_name="storefront",
            conflicting_manifest_id="manifest-from-another-project",
            expected_error_fragment="another project already uses that name here",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_new_name_with_foreign_manifests_when_renaming_then_rename_is_refused(
    test_case: ProjectRenameConflictTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_dir, arguments = built_shop_project(
        tmp_path=tmp_path,
        settings=clickhouse_connection_settings,
        database=clickhouse_database,
        monkeypatch=monkeypatch,
    )
    clickhouse_client.command(
        f"INSERT INTO {clickhouse_database}._sqltide_manifests "
        "(manifest_id, project_identity, target_name, target_database, manifest_version, "
        "published_at) VALUES ({id:String}, {identity:String}, 'test', {database:String}, 1, "
        "now64(6))",
        parameters={
            "id": test_case.conflicting_manifest_id,
            "identity": test_case.new_name,
            "database": clickhouse_database,
        },
    )
    _ = capsys.readouterr()

    assert run_cli(("sqt", "rename-project", test_case.new_name, "--apply", *arguments)) == 1

    assert test_case.expected_error_fragment in capsys.readouterr().err
    assert (project_dir / "sqltide_project.toml").read_text(encoding="utf-8") == PROJECT_CONFIG
    assert manifest_ids(
        clickhouse_client, database=clickhouse_database, identity=test_case.new_name
    ) == (test_case.conflicting_manifest_id,)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        RoleAssignmentRenameTestCase(
            description="moves project role assignments to the new name",
            new_name="storefront",
            role_name="operator",
            expected_moved_roles=("operator",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_project_role_grants_when_renaming_then_grants_follow_the_project(
    test_case: RoleAssignmentRenameTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, arguments = built_shop_project(
        tmp_path=tmp_path,
        settings=clickhouse_connection_settings,
        database=clickhouse_database,
        monkeypatch=monkeypatch,
    )
    url: str = f"sqlite:///{tmp_path / 'control.db'}"
    store: ControlStore = ControlStore(url=url)
    user_id: UUID = store.create_user(username="ada").user_id
    store.grant_project_role(
        user_id=user_id,
        project_name="shop",
        role_name=test_case.role_name,
        target_name=None,
        actor_user_id=None,
    )

    assert (
        run_cli(
            (
                "sqt",
                "rename-project",
                test_case.new_name,
                "--apply",
                "--control-store-url",
                url,
                *arguments,
            )
        )
        == 0
    )

    moved: tuple[ProjectRoleAssignment, ...] = store.list_project_role_assignments(
        user_id=user_id, project_name=test_case.new_name
    )
    assert tuple(assignment.role_name for assignment in moved) == test_case.expected_moved_roles
    assert store.list_project_role_assignments(user_id=user_id, project_name="shop") == ()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        RoleAssignmentConflictTestCase(
            description="refuses when both names hold the same active role",
            new_name="storefront",
            role_name="operator",
            expected_error_fragment="already holds role assignments",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_conflicting_role_grants_when_renaming_then_rename_is_refused(
    test_case: RoleAssignmentConflictTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_dir, arguments = built_shop_project(
        tmp_path=tmp_path,
        settings=clickhouse_connection_settings,
        database=clickhouse_database,
        monkeypatch=monkeypatch,
    )
    url: str = f"sqlite:///{tmp_path / 'control.db'}"
    store: ControlStore = ControlStore(url=url)
    user_id: UUID = store.create_user(username="ada").user_id
    for project_name in ("shop", test_case.new_name):
        store.grant_project_role(
            user_id=user_id,
            project_name=project_name,
            role_name=test_case.role_name,
            target_name=None,
            actor_user_id=None,
        )
    _ = capsys.readouterr()

    assert (
        run_cli(
            (
                "sqt",
                "rename-project",
                test_case.new_name,
                "--apply",
                "--control-store-url",
                url,
                *arguments,
            )
        )
        == 1
    )

    assert test_case.expected_error_fragment in capsys.readouterr().err
    assert (project_dir / "sqltide_project.toml").read_text(encoding="utf-8") == PROJECT_CONFIG
    assert (
        manifest_ids(clickhouse_client, database=clickhouse_database, identity=test_case.new_name)
        == ()
    )
    assert len(store.list_project_role_assignments(user_id=user_id, project_name="shop")) == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ConsumerGroupDriftTestCase(
            description="plan and source health warn when the project name changes by hand",
            renamed_config=PROJECT_CONFIG.replace('name = "shop"', 'name = "storefront"'),
            expected_live_group_prefix="streambuild_shop_test_orders_",
            expected_declared_group_prefix="streambuild_storefront_test_orders_",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_live_group_differs_when_planning_then_drift_is_reported_without_blocking(
    test_case: ConsumerGroupDriftTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_dir, arguments = built_shop_project(
        tmp_path=tmp_path,
        settings=clickhouse_connection_settings,
        database=clickhouse_database,
        monkeypatch=monkeypatch,
    )
    (project_dir / "sqltide_project.toml").write_text(test_case.renamed_config, encoding="utf-8")
    live_group: str = f"{test_case.expected_live_group_prefix}{clickhouse_database}"
    declared_group: str = f"{test_case.expected_declared_group_prefix}{clickhouse_database}"
    _ = capsys.readouterr()

    assert run_cli(("sqt", "plan", *arguments)) == 0
    plan_text: str = capsys.readouterr().out
    warnings: list[dict[str, str]] = json.loads(plan_json(arguments, capsys))[
        "consumer_group_warnings"
    ]
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    payload: dict[str, object] = build_state_payload(
        analysis=analyze_project(
            pipelines_root=project_dir / "pipelines",
            loaded_project=_native.load_project_input_for_path(path=project_dir),
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        ),
        connection=connection,
        database=clickhouse_database,
    )

    assert (
        f"consumes as Kafka consumer group '{live_group}' but the project declares "
        f"'{declared_group}'"
    ) in plan_text
    assert "would not carry over" in plan_text
    assert [
        (warning["live_consumer_group"], warning["declared_consumer_group"]) for warning in warnings
    ] == [(live_group, declared_group)]
    sources: Mapping[str, Mapping[str, Mapping[str, str]]] = cast(
        Mapping[str, Mapping[str, Mapping[str, str]]], payload["sources"]
    )
    assert sources["orders"]["consumerGroupDrift"]["liveGroup"] == live_group
    assert kafka_group_name(clickhouse_client, database=clickhouse_database) == live_group


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        MultiTargetRenameTestCase(
            description="renames on one target, then completes with --from on the other",
            new_name="storefront",
            role_name="operator",
            expected_config=TWO_TARGET_PROJECT_CONFIG.replace(
                'name = "shop"\n',
                'name = "storefront"\n'
                "# Keeps the Kafka consumer groups this project used before it was renamed.\n"
                'consumer_group_identity = "shop"\n',
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_two_targets_when_completing_rename_with_from_then_each_target_carries_over(
    test_case: MultiTargetRenameTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    request: pytest.FixtureRequest,
) -> None:
    monkeypatch.delenv("SQLTIDE_CONTROL_STORE_URL", raising=False)
    west_database: str = f"{clickhouse_database}_west"
    request.addfinalizer(
        lambda: clickhouse_client.command(f"DROP DATABASE IF EXISTS {west_database} SYNC")
    )
    project_dir: Path = write_shop_project(tmp_path / "shop", TWO_TARGET_PROJECT_CONFIG)
    config_path: Path = project_dir / "sqltide_project.toml"
    databases: dict[str, str] = {"east": clickhouse_database, "west": west_database}
    stores: dict[str, ControlStore] = {
        target: ControlStore(url=f"sqlite:///{tmp_path / target / 'control.db'}")
        for target in databases
    }
    build_arguments: dict[str, tuple[str, ...]] = {
        target: target_arguments(
            settings=clickhouse_connection_settings,
            project_dir=project_dir,
            target=target,
            database=database,
        )
        for target, database in databases.items()
    }
    arguments: dict[str, tuple[str, ...]] = {
        target: (*build_arguments[target], "--control-store-url", str(stores[target].url))
        for target in databases
    }
    user_ids: dict[str, UUID] = {
        target: store.create_user(username="ada").user_id for target, store in stores.items()
    }
    _ = [
        store.grant_project_role(
            user_id=user_ids[target],
            project_name="shop",
            role_name=test_case.role_name,
            target_name=target,
            actor_user_id=None,
        )
        for target, store in stores.items()
    ]
    assert [
        run_cli(("sqt", "build", "--auto-approve", *build_arguments[target]))
        for target in databases
    ] == [0, 0]
    groups: dict[str, str] = {
        target: kafka_group_name(clickhouse_client, database=database)
        for target, database in databases.items()
    }
    recorded: dict[str, tuple[str, ...]] = {
        target: manifest_ids(clickhouse_client, database=database, identity="shop")
        for target, database in databases.items()
    }

    assert (
        run_cli(("sqt", "rename-project", test_case.new_name, "--from", "shop", *arguments["west"]))
        == 1
    )
    assert "sqltide_project.toml is still named 'shop'" in capsys.readouterr().err
    assert (
        run_cli(("sqt", "rename-project", test_case.new_name, "--apply", *arguments["east"])) == 0
    )
    renamed_config: str = config_path.read_text(encoding="utf-8")
    assert run_cli(("sqt", "rename-project", "outlet", "--from", "shop", *arguments["west"])) == 1
    assert "the new name must be the name sqltide_project.toml already carries" in (
        capsys.readouterr().err
    )
    assert (
        run_cli(
            (
                "sqt",
                "rename-project",
                test_case.new_name,
                "--from",
                test_case.new_name,
                *arguments["west"],
            )
        )
        == 1
    )
    assert "cannot be the new name 'storefront'" in capsys.readouterr().err
    assert run_cli(("sqt", "build", "--auto-approve", *build_arguments["west"])) == 0
    built_under_new_name: tuple[str, ...] = manifest_ids(
        clickhouse_client, database=west_database, identity=test_case.new_name
    )
    assert (
        run_cli(
            ("sqt", "rename-project", test_case.new_name, "--from", "shopp", *arguments["west"])
        )
        == 1
    )
    assert "Nothing on target 'west' records project 'shopp'" in capsys.readouterr().err
    assert (
        run_cli(("sqt", "rename-project", test_case.new_name, "--from", "shop", *arguments["west"]))
        == 0
    )
    dry_run: str = capsys.readouterr().out
    assert (
        manifest_ids(clickhouse_client, database=west_database, identity=test_case.new_name)
        == built_under_new_name
    )
    assert (
        run_cli(
            (
                "sqt",
                "rename-project",
                test_case.new_name,
                "--from",
                "shop",
                "--apply",
                *arguments["west"],
            )
        )
        == 0
    )
    assert (
        run_cli(
            (
                "sqt",
                "rename-project",
                test_case.new_name,
                "--from",
                "shop",
                "--apply",
                *arguments["west"],
            )
        )
        == 0
    )

    assert renamed_config == test_case.expected_config
    assert config_path.read_text(encoding="utf-8") == renamed_config
    assert 'already named "storefront"; unchanged' in dry_run
    assert "Kafka consumer groups: 1 unchanged" in dry_run
    assert {
        target: kafka_group_name(clickhouse_client, database=database)
        for target, database in databases.items()
    } == groups
    destroy_history: dict[str, tuple[str, ...]] = {
        target: destroy_history_ids(
            settings=clickhouse_connection_settings,
            database=database,
            identity=test_case.new_name,
            target=target,
        )
        for target, database in databases.items()
    }
    assert len(built_under_new_name) == 1
    assert destroy_history == {
        "east": recorded["east"],
        "west": tuple(sorted(recorded["west"] + built_under_new_name)),
    }
    assert {
        target: role_names(store, user_id=user_ids[target], project_name=test_case.new_name)
        for target, store in stores.items()
    } == {target: (test_case.role_name,) for target in databases}


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ConsumerGroupDriftTestCase(
            description="refuses to rename while live groups differ from the declaration",
            renamed_config=PROJECT_CONFIG.replace('name = "shop"', 'name = "storefront"'),
            expected_live_group_prefix="streambuild_shop_test_orders_",
            expected_declared_group_prefix="streambuild_storefront_test_orders_",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_live_group_drift_when_renaming_then_rename_is_refused(
    test_case: ConsumerGroupDriftTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_dir, arguments = built_shop_project(
        tmp_path=tmp_path,
        settings=clickhouse_connection_settings,
        database=clickhouse_database,
        monkeypatch=monkeypatch,
    )
    config_path: Path = project_dir / "sqltide_project.toml"
    config_path.write_text(test_case.renamed_config, encoding="utf-8")
    _ = capsys.readouterr()

    assert run_cli(("sqt", "rename-project", "outlet", "--apply", *arguments)) == 1

    error: str = capsys.readouterr().err
    assert (
        f"kafka__orders consumes as '{test_case.expected_live_group_prefix}{clickhouse_database}' "
        "but the project declares "
        f"'{test_case.expected_declared_group_prefix}{clickhouse_database}'"
    ) in error
    assert "consumer group drift warning" in error
    assert config_path.read_text(encoding="utf-8") == test_case.renamed_config
    assert manifest_ids(clickhouse_client, database=clickhouse_database, identity="outlet") == ()
