"""Typed authentication and account-domain models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sqltide._native import AccountAuditRecord as AccountAuditRecord
from sqltide._native import AuthenticatedRequest as AuthenticatedRequest
from sqltide._native import AuthSettings as AuthSettings
from sqltide._native import Principal as Principal
from sqltide._native import ProjectRoleAssignment as ProjectRoleAssignment
from sqltide._native import ResolvedSession as ResolvedSession
from sqltide._native import SessionCredentials as SessionCredentials
from sqltide._native import UserAccount as UserAccount


class LoginRequest(BaseModel):
    """Password-login request body."""

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class CreateUserRequest(BaseModel):
    """Account-creation request body."""

    username: str
    displayName: str | None = None
    email: str | None = None
    authenticationSource: Literal["password", "trusted_proxy"]
    password: str | None = None
    roles: list[str] = Field(default_factory=lambda: ["viewer"])


class UpdateUserRequest(BaseModel):
    """Account-profile and status update request body."""

    displayName: str | None = None
    email: str | None = None
    active: bool | None = None


class PasswordResetRequest(BaseModel):
    """Password-reset request body."""

    password: str


class RoleRequest(BaseModel):
    """Role-assignment request body."""

    role: str


class ProjectRoleRequest(BaseModel):
    """Project-role assignment request body; a null target means all targets."""

    projectName: str = Field(min_length=1, max_length=256)  # noqa: N815 - wire format
    role: str = Field(min_length=1, max_length=128)
    targetName: str | None = Field(default=None, max_length=128)  # noqa: N815 - wire format
