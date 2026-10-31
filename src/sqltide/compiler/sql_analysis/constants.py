"""SQL scanner and Polyglot vocabulary."""

SQL_QUOTE_CHARACTERS: frozenset[str] = frozenset({"'", '"', "`"})
SQL_IDENTIFIER_PREFIX: str = "_"
SQL_OPEN_PARENTHESIS: str = "("
SQL_CLOSE_PARENTHESIS: str = ")"
SQL_ARGUMENT_SEPARATOR: str = ","
SQL_NAMED_ARGUMENT_SEPARATOR: str = "="
SQL_STATEMENT_DELIMITER: str = ";"
SQL_LINE_COMMENT: str = "--"
SQL_HASH_COMMENT: str = "#"
SQL_BLOCK_COMMENT_OPEN: str = "/*"
SQL_BLOCK_COMMENT_CLOSE: str = "*/"
SQL_ESCAPE_CHARACTER: str = "\\"
SQL_WILDCARD: str = "*"
SQL_WITH_KEYWORD: str = "WITH"
SQL_RECURSIVE_KEYWORD: str = "RECURSIVE"
SQL_AS_KEYWORD: str = "AS"
SCAN_ERROR_KIND_MISSING_WITH: str = "missing_with"
SCAN_ERROR_MISSING_WITH_SUFFIX: str = " must declare CTEs in a WITH clause"
CLICKHOUSE_AGGREGATE_STATE_TYPE_NAMES: frozenset[str] = frozenset(
    {"aggregatefunction", "simpleaggregatefunction"}
)
CLICKHOUSE_NAMED_FIELD_TYPE_NAMES: frozenset[str] = frozenset({"tuple", "nested"})

REFERENCE_FUNCTION_PREFIX: str = "__"
SOURCE_REFERENCE_FUNCTION: str = "__source"
MODEL_REFERENCE_FUNCTION: str = "__ref"
REFERENCE_FUNCTIONS: tuple[str, ...] = (
    SOURCE_REFERENCE_FUNCTION,
    MODEL_REFERENCE_FUNCTION,
)
REFERENCE_WITH_TYPE_ARGUMENT_COUNT: int = 2
PAIRED_QUOTE_CHARACTER_COUNT: int = 2
VALID_REFERENCE_ARGUMENT_COUNTS: frozenset[int] = frozenset({1, REFERENCE_WITH_TYPE_ARGUMENT_COUNT})


POLYGLOT_COMPACT_PROJECTIONS_KEY: str = "projections"
POLYGLOT_COMPACT_SET_OPERATIONS_KEY: str = "setOperations"
POLYGLOT_COMPACT_BRANCHES_KEY: str = "branches"
POLYGLOT_COMPACT_NAME_KEY: str = "name"
POLYGLOT_COMPACT_IS_STAR_KEY: str = "isStar"
POLYGLOT_COMPACT_UPSTREAM_KEY: str = "upstream"
POLYGLOT_COMPACT_SOURCE_NAME_KEY: str = "sourceName"
POLYGLOT_COMPACT_CONFIDENCE_KEY: str = "confidence"
POLYGLOT_COMPACT_COLUMN_KEY: str = "column"
POLYGLOT_COMPACT_INDEX_KEY: str = "index"

CLICKHOUSE_AGGREGATING_ENGINE_NAMES: frozenset[str] = frozenset(
    {"aggregatingmergetree", "summingmergetree"}
)
