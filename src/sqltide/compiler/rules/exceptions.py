"""A configured Rule failure is a compile failure, not a source rewrite."""

import sqltide._native as _native


class SqlRulesError(ValueError):
    """Invalid Rules configuration, SQL adaptation or unsafe formatting candidate."""


class RulesError(ValueError):
    """All findings from one compile, with the default-selection migration notice."""

    def __init__(
        self, *, findings: tuple[_native.RuleFinding, ...], defaults: bool = False
    ) -> None:
        self.findings = findings
        message: str = "\n\n".join(
            _native.render_diagnostic(diagnostic=finding.diagnostic) for finding in findings
        )
        if defaults:
            message += (
                "\nSQLClean default SQL Rules are enabled. Configure [rules] select = [] "
                "to disable configurable Rules, or ignore selected codes in sqltide_project.toml. "
                "Mandatory compiler validation still applies."
            )
        super().__init__(message)
