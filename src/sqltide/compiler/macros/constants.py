"""Constants for authored Python SQL macros."""

MACRO_SIGIL: str = "@"
MACRO_CONTEXT_PARAMETER_NAME: str = "ctx"
MACRO_CONTEXT_ANNOTATION_NAME: str = "MacroContext"
PIPELINE_NAMING_CONTEXT_ANNOTATION_NAME: str = "PipelineNamingContext"
RESERVED_MACRO_NAMES: frozenset[str] = frozenset({"const", "enum"})
CONSTANTS_DIRECTORY_NAME: str = "constants"
ENUMS_DIRECTORY_NAME: str = "enums"
