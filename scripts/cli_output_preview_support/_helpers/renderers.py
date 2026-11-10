"""One preview renderer per scenario."""

from scripts.cli_output_preview_support._helpers.plan_fixtures import (
    build_direct_plan_preview,
    build_multi_target_plan_preview,
    build_multi_target_plan_preview_desired_state,
    build_plan_preview,
    build_plan_preview_desired_state,
    build_type_change_plan_preview,
    build_type_change_plan_preview_desired_state,
)
from scripts.cli_output_preview_support._helpers.result_fixtures import (
    build_ambiguity_candidates,
    build_audit_caution_preview,
    build_audit_preview,
    build_backfill_preview,
    build_publish_preview,
)
from scripts.cli_output_preview_support.models import PreviewRequest
from sqltide._native import render_virtual_build_result
from sqltide.adapters.clickhouse.constants import CLICKHOUSE_ADAPTER_NAME
from sqltide.cli.plan.main.render_direct_plan_text import render_direct_plan_text
from sqltide.cli.plan.main.render_plan_result import render_plan_result
from sqltide.cli.promotion.main.render_promotion_result import render_promotion_result
from sqltide.cli.readiness.main.render_ambiguous_deployment_message import (
    render_ambiguous_deployment_message,
)
from sqltide.cli.readiness.main.render_deployment_audit_result import (
    render_deployment_audit_result,
)


def render_plan_preview(request: PreviewRequest) -> str:
    """Render the plan preview scenario."""

    return render_plan_result(
        plan=build_plan_preview(),
        desired_state=build_plan_preview_desired_state(),
        database=request.database,
        adapter_name=CLICKHOUSE_ADAPTER_NAME,
        json_output=request.json_output,
        verbose=request.verbose,
    )


def render_multi_target_plan_preview(request: PreviewRequest) -> str:
    """Render the multi target plan preview scenario."""

    return render_plan_result(
        plan=build_multi_target_plan_preview(),
        desired_state=build_multi_target_plan_preview_desired_state(),
        database=request.database,
        adapter_name=CLICKHOUSE_ADAPTER_NAME,
        json_output=request.json_output,
        verbose=request.verbose,
    )


def render_type_change_plan_preview(request: PreviewRequest) -> str:
    """Render the type change plan preview scenario."""

    return render_plan_result(
        plan=build_type_change_plan_preview(),
        desired_state=build_type_change_plan_preview_desired_state(),
        database=request.database,
        adapter_name=CLICKHOUSE_ADAPTER_NAME,
        json_output=request.json_output,
        verbose=request.verbose,
    )


def render_direct_plan_preview(request: PreviewRequest) -> str:
    """Render the direct plan preview scenario."""

    return render_direct_plan_text(
        plan=build_direct_plan_preview(),
        adapter_name=CLICKHOUSE_ADAPTER_NAME,
        verbose=request.verbose,
    )


def render_backfill_preview(request: PreviewRequest) -> str:
    """Render the backfill preview scenario."""

    return render_virtual_build_result(
        result=build_backfill_preview(),
        database=request.database,
        json_output=request.json_output,
    )


def render_audit_preview(request: PreviewRequest) -> str:
    """Render the audit preview scenario."""

    return render_deployment_audit_result(
        result=build_audit_preview(),
        database=request.database,
        json_output=request.json_output,
    )


def render_audit_caution_preview(request: PreviewRequest) -> str:
    """Render the audit caution preview scenario."""

    return render_deployment_audit_result(
        result=build_audit_caution_preview(),
        database=request.database,
        json_output=request.json_output,
    )


def render_publish_preview(request: PreviewRequest) -> str:
    """Render the publish preview scenario."""

    return render_promotion_result(
        result=build_publish_preview(),
        database=request.database,
        json_output=request.json_output,
    )


def render_audit_ambiguous_preview(request: PreviewRequest) -> str:
    """Render the audit ambiguous preview scenario."""

    return render_ambiguous_deployment_message(
        command_name="deployment audit",
        database=request.database,
        root_names=("tbl__orders_enriched",),
        candidates=build_ambiguity_candidates(),
    )
