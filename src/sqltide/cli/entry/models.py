from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class CliEntrypointHandlers:
    run_discover: Callable[..., int]
    run_compile: Callable[..., int]
    run_test: Callable[..., int]
    run_audit: Callable[..., int]
    run_plan: Callable[..., int]
    run_build: Callable[..., int]
    run_destruction: Callable[..., int]
    run_deployment_diff: Callable[..., int]
    run_deployment_list: Callable[..., int]
    run_deployment_show: Callable[..., int]
    run_deployment_audit: Callable[..., int]
    run_deployment_promote: Callable[..., int]
    run_deployment_rollback: Callable[..., int]
    run_reconcile: Callable[..., int]
    run_janitor: Callable[..., int]
    run_doctor: Callable[..., int]
    run_repair_active_view: Callable[..., int]
    run_dev: Callable[..., int]
