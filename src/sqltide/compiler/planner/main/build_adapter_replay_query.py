"""Build a compiler-analyzed query for adapter replay."""

from sqltide._native import rewrite_query, rewrite_template_query
from sqltide.adapter.constants import ADAPTER_DATABASE_PLACEHOLDER
from sqltide.adapter.models import AdapterPhysicalRelationMapping, AdapterReplayQuery
from sqltide.compiler.planner.exceptions import DeploymentPlanError
from sqltide.compiler.sql_analysis.exceptions import SqlAnalysisError
from sqltide.compiler.sql_analysis.models import SqlQueryRewriteResult, SqlRelationRewrite


def build_adapter_replay_query(
    *,
    query: str,
    database_template: str | None = None,
    source_relation_name: str,
    database: str,
    physical_relation_mappings: tuple[AdapterPhysicalRelationMapping, ...],
) -> AdapterReplayQuery:
    """Rewrite side references physically and classify aggregate semantics."""

    relation_rewrites: tuple[SqlRelationRewrite, ...] = tuple(
        SqlRelationRewrite(
            source_name=mapping.logical_name,
            target_relation=f"{database}.{mapping.physical_name}",
        )
        for mapping in physical_relation_mappings
        if mapping.logical_name != source_relation_name
    )
    try:
        result: SqlQueryRewriteResult = (
            rewrite_template_query(
                template=database_template,
                dialect="clickhouse",
                database_placeholder=ADAPTER_DATABASE_PLACEHOLDER,
                relation_rewrites=relation_rewrites,
            )
            if database_template is not None
            else rewrite_query(
                sql=query,
                dialect="clickhouse",
                relation_rewrites=relation_rewrites,
            )
        )
    except SqlAnalysisError as error:
        raise DeploymentPlanError(str(error)) from None
    return AdapterReplayQuery(
        query=result.query,
        physical_relation_mappings=physical_relation_mappings,
        aggregate_semantics=result.has_aggregate_semantics,
    )
