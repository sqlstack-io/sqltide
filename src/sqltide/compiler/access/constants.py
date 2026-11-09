"""Access-policy file name and the permissions each grant kind may give."""

import sqltide._native as _native
from sqltide.compiler.access.types import Permission

ACCESS_POLICY_FILE_NAME: str = "access.yml"
PIPELINE_PERMISSIONS: frozenset[Permission] = frozenset(
    map(Permission, _native.ACCESS_PIPELINE_PERMISSIONS)
)
PROJECT_PERMISSIONS: frozenset[Permission] = frozenset(
    map(Permission, _native.ACCESS_PROJECT_PERMISSIONS)
)
TARGET_PERMISSIONS: frozenset[Permission] = frozenset(
    map(Permission, _native.ACCESS_TARGET_PERMISSIONS)
)
