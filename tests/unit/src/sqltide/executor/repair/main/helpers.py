from sqltide._native import (
    render_clickhouse_stable_binding_replacement,
)
from sqltide.adapter.models import AdapterBindingReplacementRequest
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection


class RepairWorkflowAdapterConnection(RecordingAdapterConnection):
    def __init__(self) -> None:
        super().__init__()

    def render_replace_stable_bindings(
        self, request: AdapterBindingReplacementRequest
    ) -> tuple[str, ...]:
        return render_clickhouse_stable_binding_replacement(
            connection=self,
            request=request,
        )
