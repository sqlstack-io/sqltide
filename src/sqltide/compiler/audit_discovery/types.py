"""SQL audit discovery value domains."""

from enum import StrEnum


class AuditAttachmentKind(StrEnum):
    """How an audit is attached to the compiled project."""

    STANDALONE = "standalone"
    MODEL = "model"
    COLUMN = "column"
