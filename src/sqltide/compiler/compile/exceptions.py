"""Custom exceptions for transform SQL contract validation."""

from sqltide._native import AuditCompileError as AuditCompileError
from sqltide._native import PipelineCompileError as PipelineCompileError
from sqltide._native import TransformOrderByUnknownColumnError as TransformOrderByUnknownColumnError
from sqltide._native import (
    TransformPartitionByUnknownColumnError as TransformPartitionByUnknownColumnError,
)
from sqltide._native import TransformSqlContractError as TransformSqlContractError
from sqltide._native import TransformSqlDuplicateAliasError as TransformSqlDuplicateAliasError
from sqltide._native import TransformSqlFinalQueryShapeError as TransformSqlFinalQueryShapeError
from sqltide._native import (
    TransformSqlMultipleStatementsError as TransformSqlMultipleStatementsError,
)
from sqltide._native import TransformSqlParseError as TransformSqlParseError
from sqltide._native import TransformSqlStarProjectionError as TransformSqlStarProjectionError
from sqltide._native import (
    TransformSqlTopLevelSetOperationError as TransformSqlTopLevelSetOperationError,
)
from sqltide._native import TransformSqlUntypedProjectionError as TransformSqlUntypedProjectionError
from sqltide._native import TransformTtlUnknownColumnError as TransformTtlUnknownColumnError
