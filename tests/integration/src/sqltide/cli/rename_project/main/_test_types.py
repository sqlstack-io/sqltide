from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectRenameTestCase:
    description: str
    old_name: str
    new_name: str
    expected_config: str


@dataclass(frozen=True)
class ProjectRenameConflictTestCase:
    description: str
    new_name: str
    conflicting_manifest_id: str
    expected_error_fragment: str


@dataclass(frozen=True)
class RoleAssignmentRenameTestCase:
    description: str
    new_name: str
    role_name: str
    expected_moved_roles: tuple[str, ...]


@dataclass(frozen=True)
class RoleAssignmentConflictTestCase:
    description: str
    new_name: str
    role_name: str
    expected_error_fragment: str


@dataclass(frozen=True)
class ConsumerGroupDriftTestCase:
    description: str
    renamed_config: str
    expected_live_group_prefix: str
    expected_declared_group_prefix: str


@dataclass(frozen=True)
class MultiTargetRenameTestCase:
    description: str
    new_name: str
    role_name: str
    expected_config: str
