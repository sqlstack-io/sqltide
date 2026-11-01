"""Compare compiled direct models with their last applied fingerprints."""

from __future__ import annotations

import json
from hashlib import sha256

import sqltide._native as _native
from sqltide.adapter.models import AdapterDirectFingerprintRecord
from sqltide.compiler.compile.models import CompiledModel, CompiledTableModel

type _NativeStorage = tuple[str, list[str], str | None, str | None, list[tuple[str, str]] | None]
type _NativeIdentity = tuple[str, str, str, list[tuple[str, str]], _NativeStorage | None]


def _native_identity(model: CompiledModel) -> _NativeIdentity:
    storage: _NativeStorage | None = None
    if isinstance(model, CompiledTableModel):
        storage = (
            model.transform.engine,
            list(model.transform.order_by),
            model.transform.partition_by,
            model.transform.ttl,
            None if model.transform.settings is None else list(model.transform.settings.items()),
        )
    return (
        model.key.name,
        str(model.kind),
        model.relation_name,
        [(column.name, column.type) for column in model.output_columns],
        storage,
    )


class DirectModelFingerprint:
    """Build and compare canonical semantic direct-model fingerprints."""

    @staticmethod
    def identity(*, model: CompiledModel) -> dict[str, object]:
        """Return the allowlisted semantic identity persisted for changed selection."""

        return json.loads(_native.direct_model_identity_metadata(_native_identity(model)))

    @staticmethod
    def query_hash(query_sql: str) -> str:
        return sha256(query_sql.encode()).hexdigest()

    @staticmethod
    def record(
        *, model: CompiledModel, database: str, workflow_id: str, tool_version: str
    ) -> AdapterDirectFingerprintRecord:
        fingerprint_id: str
        logical_identity: str
        definition_hash: str
        identity_metadata: str
        fingerprint_id, logical_identity, definition_hash, identity_metadata = (
            _native.direct_fingerprint_record(
                _native_identity(model), model.query, database, workflow_id
            )
        )
        return AdapterDirectFingerprintRecord(
            fingerprint_id=fingerprint_id,
            logical_model_identity=logical_identity,
            definition_sql=model.query,
            definition_hash=definition_hash,
            identity_metadata=identity_metadata,
            workflow_id=workflow_id,
            tool_version=tool_version,
        )

    @staticmethod
    def drift_reasons(
        *,
        model: CompiledModel,
        baseline: AdapterDirectFingerprintRecord | None,
    ) -> tuple[str, ...]:
        """Return direct fingerprint differences shared by native planning and the UI."""

        return tuple(
            _native.direct_model_drift_reasons(
                _native_identity(model),
                model.query,
                None
                if baseline is None
                else (baseline.definition_hash, baseline.identity_metadata),
            )
        )
