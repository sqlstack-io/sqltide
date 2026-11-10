"""Initialize metadata and publish the complete compiled project manifest."""

import os

from sqltide._native import build_manifest, resolve_manifest_project_identity
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.cli.build.constants import SQLTIDE_TOOL_VERSION
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.executor.observability.constants import PROJECT_REVISION_ENV_VAR
from sqltide.executor.observability.main.initialize_observability import (
    initialize_observability,
)
from sqltide.executor.observability.main.publish_manifest import publish_manifest


def initialize_build_metadata(
    *,
    analysis: CompileAnalysis,
    invocation_id: str,
    target_database: str,
    metadata_database: str,
    connection: AdapterConnection,
) -> None:
    """Migrate metadata then append one full-project manifest."""

    initialize_observability(connection=connection, database=metadata_database)
    publish_manifest(
        connection=connection,
        database=metadata_database,
        manifest=build_manifest(
            analysis=analysis,
            invocation_id=invocation_id,
            project_identity=resolve_manifest_project_identity(analysis=analysis),
            target_database=target_database,
            tool_version=SQLTIDE_TOOL_VERSION,
            project_revision=os.environ.get(PROJECT_REVISION_ENV_VAR),
        ),
    )
