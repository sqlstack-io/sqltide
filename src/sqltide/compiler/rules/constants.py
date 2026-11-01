"""SQLClean host policy: selection, source roles and conservative fix eligibility."""

SQLCLEAN_FAMILY: str = "SQLCLEAN"
SUPPRESSION_CODE: str = "SQTSUPPRESSION000"
CROSS_JOIN_CODE: str = "SQLCLEAN038"
QUALIFY_CODE: str = "SQLCLEAN003"
SNOWFLAKE_DIALECT: str = "snowflake"
MODEL_HEADER: str = "MODEL"
TEST_HEADER: str = "TEST"
SELECT_KEY: str = "select"
RELATION_KEY: str = "relation"
CTE_KEY: str = "cte"
ERROR_KEY: str = "Err"
CRLF: str = "\r\n"
LF: str = "\n"
THRESHOLD_KEYS: frozenset[str] = frozenset({"max_literal_length", "max_ranking_order_by"})
INVALID_PATH_PARTS: frozenset[str] = frozenset({"", ".", ".."})
INVALID_PATH_CHARACTERS: tuple[str, ...] = ("\\", ":", "*", "?", "[", "]", "{", "}")
SAFE_FIX_CODES: frozenset[str] = frozenset(
    {
        "SQLCLEAN002",
        "SQLCLEAN003",
        "SQLCLEAN005",
        "SQLCLEAN006",
        "SQLCLEAN008",
        "SQLCLEAN017",
        "SQLCLEAN025",
        "SQLCLEAN042",
    }
)
