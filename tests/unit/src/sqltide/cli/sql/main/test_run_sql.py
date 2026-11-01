"""Public CLI regressions for SQLClean defaults, opt-outs, templates and conservative writes."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sqltide.cli.entry.main.main import main
from tests.unit.src.sqltide.cli.sql.main._test_types import FormatTestCase, RulesTestCase
from tests.unit.src.sqltide.cli.sql.main.helpers import (
    write_selector_rules_project,
    write_sql_project,
)


@pytest.mark.parametrize(
    "test_case",
    [
        RulesTestCase(
            description="omitted selection enables defaults",
            expected_exit=1,
            expected_codes=("SQLCLEAN004",),
        ),
        RulesTestCase(
            description="empty selection disables configurable Rules",
            config="\n[rules]\nselect = []\n",
        ),
        RulesTestCase(
            description="family ignore excludes SQLClean",
            config='\n[rules]\nignore = ["SQLCLEAN"]\n',
        ),
        RulesTestCase(
            description="explicit selection is enforced",
            config='\n[rules]\nselect = ["SQLCLEAN004"]\n',
            expected_exit=1,
            expected_codes=("SQLCLEAN004",),
        ),
        RulesTestCase(
            description="header opt-out preserves compilation",
            header="kind view, sql_analysis false",
        ),
        RulesTestCase(
            description="CLI analysis opt-out preserves compilation", flags=("--no-sql-analysis",)
        ),
        RulesTestCase(
            description="project analysis opt-out preserves compilation",
            config="\n[settings]\nsql_analysis = false\n",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_unordered_query_when_compiling_then_configured_rules_are_enforced(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: RulesTestCase,
) -> None:
    model: Path = write_sql_project(
        root=tmp_path,
        sql='SELECT CAST(order_id AS UInt64) AS order_id FROM __source("orders") LIMIT 1',
        config=test_case.config,
        header=test_case.header,
    )
    original: str = model.read_text()
    assert (
        main(["sqt", "compile", "--project-dir", str(tmp_path), *test_case.flags])
        == test_case.expected_exit
    )
    assert model.read_text() == original
    output: str = capsys.readouterr().err
    assert ("SQLCLEAN004" in output) == bool(test_case.expected_codes)
    assert (tmp_path / "target").exists() == (test_case.expected_exit == 0)


@pytest.mark.parametrize(
    "test_case",
    [
        RulesTestCase(
            description="reviewed exception exempts only its owned path",
            config='\n[[rules.rule_exceptions]]\nrule = "SQLCLEAN004"\n'
            'path = "pipelines/pl__orders/orders_view.sql"\nreason = "Intentional preview"\n',
            expected_exit=1,
            expected_codes=("SQLCLEAN004",),
        ),
        RulesTestCase(
            description="reasoned path ignore does not exempt neighboring files",
            config='\n[[rules.rule_ignores]]\nrules = ["SQLCLEAN"]\n'
            'paths = ["**/orders_view.sql"]\nreason = "Intentional preview"\n',
            expected_exit=1,
            expected_codes=("SQLCLEAN004",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_reviewed_exception_when_linting_then_only_matching_file_is_exempt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: RulesTestCase,
) -> None:
    model: Path = write_sql_project(
        root=tmp_path,
        sql='SELECT CAST(order_id AS UInt64) AS order_id FROM __source("orders") LIMIT 1',
        config=test_case.config,
    )
    other: Path = model.with_name("other_orders.sql")
    other.write_text(model.read_text())
    assert (
        main(["sqt", "lint", "--project-dir", str(tmp_path), "--json"]) == test_case.expected_exit
    )
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert [item["location"]["path"] for item in payload["diagnostics"]] == [str(other)]
    assert tuple(item["code"] for item in payload["diagnostics"]) == test_case.expected_codes


@pytest.mark.parametrize(
    "test_case",
    [
        FormatTestCase(
            description="opaque nested Unicode call and literal retain exact bytes",
            sql='select @amount( "é", nested=[1, 2] ) as amount, '
            "'sqt: ignore SQLCLEAN004' as note from __source(\"orders\")",
            expected_fragments=(
                '@amount( "é", nested=[1, 2] )',
                '__source("orders")',
                "'sqt: ignore SQLCLEAN004'",
            ),
        ),
        FormatTestCase(
            description="CRLF headers and multiline Unicode templates retain exact bytes",
            sql='select @amount(\n "é", nested=[1, 2]\n) as amount from __source("orders")',
            expected_fragments=('@amount(\n "é", nested=[1, 2]\n)', '__source("orders")'),
            newline="\r\n",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_templates_when_formatting_then_templates_are_preserved_and_check_is_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: FormatTestCase,
) -> None:
    model: Path = write_sql_project(root=tmp_path, sql=test_case.sql)
    model.write_bytes(model.read_bytes().replace(b"\n", test_case.newline.encode()))
    model.chmod(0o640)
    original: bytes = model.read_bytes()
    mode: int = stat.S_IMODE(model.stat().st_mode)
    assert main(["sqt", "format", "--project-dir", str(tmp_path), "--check"]) == 1
    assert model.read_bytes() == original
    assert main(["sqt", "format", "--project-dir", str(tmp_path)]) == 0
    formatted: str = model.read_bytes().decode()
    assert all(
        fragment.replace("\n", test_case.newline) in formatted
        for fragment in test_case.expected_fragments
    )
    assert formatted.startswith(f"MODEL (kind view);{test_case.newline}{test_case.newline}SELECT")
    assert "\n" not in formatted.replace(test_case.newline, "")
    assert stat.S_IMODE(model.stat().st_mode) == mode
    assert main(["sqt", "format", "--project-dir", str(tmp_path), "--check"]) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "test_case",
    [
        FormatTestCase(
            description="count literal is fixed only with explicit proof",
            sql='SELECT CAST(COUNT(1) AS UInt64) AS total FROM __source("orders")',
            expected_fragments=("COUNT(*)",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_safe_count_fix_when_explicitly_fixing_then_verified_output_is_written(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: FormatTestCase,
) -> None:
    model: Path = write_sql_project(
        root=tmp_path, sql=test_case.sql, config='\n[rules]\nselect = ["SQLCLEAN017"]\n'
    )
    original: str = model.read_text()
    assert main(["sqt", "format", "--project-dir", str(tmp_path)]) == 0
    assert "COUNT(1)" in model.read_text()
    original = model.read_text()
    assert main(["sqt", "format", "--project-dir", str(tmp_path), "--fix", "--check"]) == 1
    assert model.read_text() == original
    assert main(["sqt", "format", "--project-dir", str(tmp_path), "--fix", "--diff"]) == 0
    assert model.read_text() == original
    assert main(["sqt", "format", "--project-dir", str(tmp_path), "--fix"]) == 0
    assert all(fragment in model.read_text() for fragment in test_case.expected_fragments)
    assert main(["sqt", "compile", "--project-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "test_case",
    [
        RulesTestCase(
            description="mandatory inference is not an optional SQL Rule", expected_exit=1
        ),
        RulesTestCase(
            description="selector ignore and opt-out cannot bypass mandatory inference",
            config='\n[[rules.rule_ignores]]\nrules = ["SQLCLEAN"]\n'
            'selectors = ["model:orders_view"]\nreason = "Reviewed preview"\n',
            expected_exit=1,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_analysis_opt_out_when_sql_is_invalid_then_mandatory_compiler_check_still_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: RulesTestCase,
) -> None:
    _ = write_sql_project(
        root=tmp_path, sql='SELECT unknown_column FROM __source("orders")', config=test_case.config
    )
    assert (
        main(["sqt", "compile", "--project-dir", str(tmp_path), "--no-sql-analysis"])
        == test_case.expected_exit
    )
    assert not (tmp_path / "target").exists()
    assert capsys.readouterr().err


@pytest.mark.parametrize(
    "test_case",
    [
        RulesTestCase(
            description="unknown selection is not silently ignored by layout formatting",
            config='\n[rules]\nselect = ["SQLCLEAN999"]\n',
            expected_exit=1,
        ),
        RulesTestCase(
            description="non boolean project opt-out is rejected",
            config='\n[settings]\nsql_analysis = "false"\n',
            expected_exit=1,
        ),
        RulesTestCase(
            description="relation exceptions do not broaden other Rules",
            config='\n[[rules.rule_exceptions]]\nrule = "SQLCLEAN004"\n'
            'path = "pipelines/pl__orders/orders_view.sql"\n'
            'reason = "Reviewed preview"\nrelation = "orders"\n',
            expected_exit=1,
        ),
        RulesTestCase(
            description="exact exceptions cannot silently become glob ignores",
            config='\n[[rules.rule_exceptions]]\nrule = "SQLCLEAN004"\n'
            'path = "pipelines/**/*.sql"\nreason = "Reviewed preview"\n',
            expected_exit=1,
        ),
        RulesTestCase(
            description="missing exception files are rejected",
            config='\n[[rules.rule_exceptions]]\nrule = "SQLCLEAN004"\n'
            'path = "pipelines/missing.sql"\nreason = "Reviewed preview"\n',
            expected_exit=1,
        ),
        RulesTestCase(
            description="malformed ignore globs are rejected even during layout-only format",
            config='\n[[rules.rule_ignores]]\nrules = ["SQLCLEAN"]\n'
            'paths = ["pipelines/["]\nreason = "Reviewed preview"\n',
            expected_exit=1,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_configuration_when_formatting_then_source_is_not_changed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: RulesTestCase,
) -> None:
    model: Path = write_sql_project(
        root=tmp_path,
        sql='select CAST(order_id AS UInt64) as order_id from __source("orders")',
        config=test_case.config,
    )
    original: str = model.read_text()
    assert main(["sqt", "format", "--project-dir", str(tmp_path)]) == test_case.expected_exit
    assert model.read_text() == original
    assert capsys.readouterr().err


@pytest.mark.parametrize(
    "test_case",
    [
        FormatTestCase(
            description="generated count is diagnosed at the macro but never edited",
            sql='SELECT CAST(@total("é") AS UInt64) AS total FROM __source("orders")',
            expected_fragments=('@total("é")',),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_macro_generated_fix_when_formatting_then_generated_text_is_not_owned(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: FormatTestCase,
) -> None:
    model: Path = write_sql_project(
        root=tmp_path, sql=test_case.sql, config='\n[rules]\nselect = ["SQLCLEAN017"]\n'
    )
    macros: Path = tmp_path / "macros"
    macros.mkdir()
    macro: Path = macros / "counts.py"
    macro.write_text('def total(label):\n    return "COUNT(1)"\n')
    macro_contents: str = macro.read_text()
    assert main(["sqt", "lint", "--project-dir", str(tmp_path), "--json"]) == 1
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    diagnostic: dict[str, Any] = payload["diagnostics"][0]
    assert diagnostic["code"] == "SQLCLEAN017"
    assert diagnostic["location"]["line"] == 3
    assert diagnostic["location"]["column"] == 13
    assert main(["sqt", "format", "--project-dir", str(tmp_path), "--fix"]) == 1
    assert all(fragment in model.read_text() for fragment in test_case.expected_fragments)
    assert "COUNT(*)" not in model.read_text()
    assert macro.read_text() == macro_contents


@pytest.mark.parametrize(
    "test_case",
    [
        RulesTestCase(
            description="effective target and CLI variables drive lint expansion once",
            expected_exit=1,
            expected_codes=("SQLCLEAN004",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_target_sensitive_macro_when_linting_then_effective_compile_context_is_reused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: RulesTestCase,
) -> None:
    _ = write_sql_project(
        root=tmp_path,
        sql='SELECT CAST(order_id AS UInt64) AS order_id FROM __source("orders") @preview()',
        config='\n[targets.preview]\ndatabase = "preview_analytics"\n',
    )
    macros: Path = tmp_path / "macros"
    macros.mkdir()
    (macros / "preview.py").write_text(
        "from sqltide.compiler.macros.models import MacroContext\n"
        "calls = 0\n"
        "def preview(ctx: MacroContext):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    assert calls == 1, 'macro expanded twice'\n"
        "    return 'LIMIT 1' if ctx.target_name == 'preview' and "
        "ctx.variables.get('preview') else ''\n"
    )
    assert main(["sqt", "lint", "--project-dir", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["diagnostics"] == []
    assert (
        main(
            [
                "sqt",
                "lint",
                "--project-dir",
                str(tmp_path),
                "--json",
                "--target",
                "preview",
                "--vars",
                '{"preview":true}',
            ]
        )
        == test_case.expected_exit
    )
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert tuple(item["code"] for item in payload["diagnostics"]) == test_case.expected_codes


@pytest.mark.parametrize(
    "test_case",
    [
        RulesTestCase(
            description="only actual comments can request inline suppression", expected_exit=1
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unicode_multi_audit_file_when_linting_then_locations_and_suppressions_are_authored(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: RulesTestCase,
) -> None:
    _ = write_sql_project(
        root=tmp_path, sql='SELECT CAST(order_id AS UInt64) AS order_id FROM __source("orders")'
    )
    audits: Path = tmp_path / "audits"
    audits.mkdir()
    audit: Path = audits / "previews.sql"
    audit.write_text(
        "AUDIT (name first);\nSELECT 'é sqt: ignore' AS note FROM __ref(\"orders_view\");\n\n"
        "AUDIT (name second);\n-- sqt: ignore SQLCLEAN004\n"
        "SELECT order_id, 'é' AS note FROM __ref(\"orders_view\") LIMIT 1;\n"
    )
    assert (
        main(["sqt", "lint", "--project-dir", str(tmp_path), "--json"]) == test_case.expected_exit
    )
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert [(item["code"], item["location"]["line"]) for item in payload["diagnostics"]] == [
        ("SQTSUPPRESSION000", 5),
        ("SQLCLEAN004", 6),
    ]
    assert payload["diagnostics"][1]["location"]["column"] == 56


@pytest.mark.parametrize(
    "test_case",
    [
        FormatTestCase(
            description="audit bodies are diagnosed but not semantically auto-fixed",
            sql='SELECT COUNT(1) AS total FROM __ref("orders_view")',
            expected_fragments=("COUNT(1)",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_audit_rule_fix_when_formatting_then_only_model_bodies_are_fix_eligible(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: FormatTestCase,
) -> None:
    model: Path = write_sql_project(
        root=tmp_path,
        sql='SELECT CAST(order_id AS UInt64) AS order_id FROM __source("orders")',
        config='\n[rules]\nselect = ["SQLCLEAN017"]\n',
    )
    audits: Path = tmp_path / "audits"
    audits.mkdir()
    audit: Path = audits / "totals.sql"
    audit.write_text(f"AUDIT ();\n\n{test_case.sql}\n")
    model_contents: str = model.read_text()
    assert main(["sqt", "format", "--project-dir", str(tmp_path), "--fix", "--json", "audits"]) == 1
    assert all(fragment in audit.read_text() for fragment in test_case.expected_fragments)
    assert model.read_text() == model_contents
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert [item["code"] for item in payload["diagnostics"]] == ["SQLCLEAN017"]


@pytest.mark.parametrize(
    "test_case",
    [RulesTestCase(description="reviewed CTE relation exception does not exempt another join")],
    ids=lambda case: case.description,
)
def test_given_scoped_exception_when_selecting_opt_in_rule_then_only_named_join_is_exempt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: RulesTestCase,
) -> None:
    _ = write_sql_project(
        root=tmp_path,
        sql="WITH paired AS (SELECT CAST(a.order_id AS UInt64) AS order_id "
        'FROM __source("orders") AS a CROSS JOIN __source("orders") AS b '
        'CROSS JOIN __ref("landing_orders") AS c) '
        "SELECT CAST(order_id AS UInt64) AS order_id FROM paired",
        config='\n[rules]\nselect = []\n[[rules.rule_exceptions]]\nrule = "SQLCLEAN038"\n'
        'path = "pipelines/pl__orders/orders_view.sql"\nreason = "Reviewed grid"\n'
        'relation = "Orders"\ncte = "PAIRED"\n',
    )
    assert (
        main(["sqt", "lint", "--project-dir", str(tmp_path), "--json"]) == test_case.expected_exit
    )
    assert json.loads(capsys.readouterr().out)["diagnostics"] == []
    assert (
        main(["sqt", "lint", "--project-dir", str(tmp_path), "--json", "--select", "SQLCLEAN038"])
        == 1
    )
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert len(payload["diagnostics"]) == 1
    assert payload["diagnostics"][0]["code"] == "SQLCLEAN038"
    assert "`landing_orders`" in payload["diagnostics"][0]["message"]


@pytest.mark.parametrize(
    "test_case",
    [
        RulesTestCase(
            description="identical audit text retains distinct macro captures", expected_exit=1
        )
    ],
    ids=lambda case: case.description,
)
def test_given_identical_audit_bodies_when_linting_then_each_actual_expansion_is_diagnosed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], test_case: RulesTestCase
) -> None:
    _ = write_sql_project(
        root=tmp_path,
        sql='SELECT CAST(order_id AS UInt64) AS order_id FROM __source("orders")',
        config='\n[rules]\nselect = ["SQLCLEAN017"]\n',
    )
    macros: Path = tmp_path / "macros"
    macros.mkdir()
    (macros / "counts.py").write_text(
        "calls = 0\ndef total():\n    global calls\n    calls += 1\n"
        "    return 'COUNT(1)' if calls == 1 else 'COUNT(*)'\n"
    )
    audits: Path = tmp_path / "audits"
    audits.mkdir()
    (audits / "totals.sql").write_text(
        'AUDIT (name first);\nSELECT @total() AS total FROM __ref("orders_view");\n\n'
        'AUDIT (name second);\nSELECT @total() AS total FROM __ref("orders_view");\n'
    )
    assert (
        main(["sqt", "lint", "--project-dir", str(tmp_path), "--json"]) == test_case.expected_exit
    )
    first: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert [(item["code"], item["location"]["line"]) for item in first["diagnostics"]] == [
        ("SQLCLEAN017", 2)
    ]
    assert (
        main(["sqt", "lint", "--project-dir", str(tmp_path), "--json"]) == test_case.expected_exit
    )
    assert json.loads(capsys.readouterr().out) == first


@pytest.mark.skipif(os.name != "posix", reason="Fault injection uses POSIX file-size limits")
@pytest.mark.parametrize(
    "test_case",
    [
        FormatTestCase(
            description="layout staging fails without truncating SQL",
            sql='select CAST(order_id AS UInt64) as order_id from __source("orders")',
            expected_fragments=("original source preserved",),
        ),
        FormatTestCase(
            description="verified fix staging fails without truncating SQL",
            sql='SELECT CAST(COUNT(1) AS UInt64) AS total FROM __source("orders")',
            expected_fragments=("original source preserved",),
            flags=("--fix",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_failed_staging_write_when_formatting_then_original_bytes_and_permissions_survive(
    tmp_path: Path, test_case: FormatTestCase
) -> None:
    model: Path = write_sql_project(root=tmp_path, sql=test_case.sql)
    model.chmod(0o640)
    original: bytes = model.read_bytes()
    mode: int = stat.S_IMODE(model.stat().st_mode)
    siblings: set[Path] = set(model.parent.iterdir())
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import resource, signal, sys; from sqltide.cli.entry.main.main import main; "
            "signal.signal(signal.SIGXFSZ, signal.SIG_IGN); "
            "resource.setrlimit(resource.RLIMIT_FSIZE, (16, 16)); sys.exit(main(sys.argv[1:]))",
            "sqt",
            "format",
            "--project-dir",
            str(tmp_path),
            *test_case.flags,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert all(fragment in result.stdout for fragment in test_case.expected_fragments)
    assert model.read_bytes() == original
    assert stat.S_IMODE(model.stat().st_mode) == mode
    assert set(model.parent.iterdir()) == siblings


@pytest.mark.parametrize(
    "test_case",
    [
        RulesTestCase(
            description="stale exact exception blocks lint",
            config='\n[[rules.rule_exceptions]]\nrule = "SQLCLEAN004"\n'
            'path = "pipelines/pl__orders/orders_view.sql"\nreason = "Preview removed"\n',
            expected_exit=1,
            expected_error="no longer matches",
        ),
        RulesTestCase(
            description="unselected exception is not stale",
            config='\n[rules]\nselect = []\n[[rules.rule_exceptions]]\nrule = "SQLCLEAN004"\n'
            'path = "pipelines/pl__orders/orders_view.sql"\nreason = "Preview removed"\n',
        ),
        RulesTestCase(
            description="deliberate scoped ignore need not match a finding",
            config='\n[[rules.rule_ignores]]\nrules = ["SQLCLEAN004"]\n'
            'paths = ["**/orders_view.sql"]\nreason = "Deliberate preview policy"\n',
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_clean_query_when_linting_then_only_evaluated_exact_exceptions_are_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], test_case: RulesTestCase
) -> None:
    _ = write_sql_project(
        root=tmp_path,
        sql='SELECT CAST(order_id AS UInt64) AS order_id FROM __source("orders")',
        config=test_case.config,
    )
    assert (
        main(["sqt", "lint", "--project-dir", str(tmp_path), "--json"]) == test_case.expected_exit
    )
    assert test_case.expected_error in capsys.readouterr().err


@pytest.mark.parametrize(
    "test_case",
    [
        RulesTestCase(
            description="model ignore does not exempt downstream models in either pipeline",
            config='selectors = ["model:orders_view"]',
            expected_paths=(
                "pipelines/pl__orders/orders_next.sql",
                "pipelines/pl__reports/orders_report.sql",
            ),
        ),
        RulesTestCase(
            description="pipeline ignore covers its own models but not dependent pipelines",
            config='selectors = ["pipeline:pl__orders"]',
            expected_paths=("pipelines/pl__reports/orders_report.sql",),
        ),
        RulesTestCase(
            description="selecting the dependent pipeline does not exempt its upstream pipeline",
            config='selectors = ["pipeline:pl__reports"]',
            expected_paths=(
                "pipelines/pl__orders/orders_next.sql",
                "pipelines/pl__orders/orders_view.sql",
            ),
        ),
        RulesTestCase(
            description="bare model names retain existing selector semantics",
            config='selectors = ["orders_view"]',
            expected_paths=(
                "pipelines/pl__orders/orders_next.sql",
                "pipelines/pl__reports/orders_report.sql",
            ),
        ),
        RulesTestCase(
            description="explicit selector union never adds implicit downstream closure",
            config='selectors = ["model:orders_view model:orders_next"]',
            expected_paths=("pipelines/pl__reports/orders_report.sql",),
        ),
        RulesTestCase(
            description="paths and selectors union only explicitly owned files",
            config='selectors = ["model:orders_view"]\npaths = ["**/orders_next.sql"]',
            expected_paths=("pipelines/pl__reports/orders_report.sql",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_selector_scoped_ignore_when_linting_then_only_explicit_resources_are_exempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], test_case: RulesTestCase
) -> None:
    _ = write_selector_rules_project(
        root=tmp_path,
        config='\n[[rules.rule_ignores]]\nrules = ["SQLCLEAN004"]\n'
        'reason = "Explicitly reviewed previews"\n' + test_case.config + "\n",
    )
    assert main(["sqt", "lint", "--project-dir", str(tmp_path), "--json"]) == 1
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert (
        tuple(
            Path(item["location"]["path"]).relative_to(tmp_path).as_posix()
            for item in payload["diagnostics"]
        )
        == test_case.expected_paths
    )
    assert all(item["code"] == "SQLCLEAN004" for item in payload["diagnostics"])


@pytest.mark.parametrize(
    "test_case",
    [
        RulesTestCase(
            description="missing model scope",
            config='["model:missing"]',
            expected_error="Unknown selected model",
        ),
        RulesTestCase(
            description="missing pipeline scope",
            config='["pipeline:missing"]',
            expected_error="Unknown selected pipeline",
        ),
        RulesTestCase(
            description="plus syntax cannot authorize downstream ignores",
            config='["orders_view+"]',
            expected_error="is not supported",
        ),
        RulesTestCase(
            description="unknown namespace",
            config='["tag:orders"]',
            expected_error="Unsupported selector namespace",
        ),
        RulesTestCase(
            description="empty selector cannot become a match all scope",
            config='["   "]',
            expected_error="non-empty strings",
        ),
        RulesTestCase(
            description="empty scopes cannot become a match all ignore",
            config="[]",
            expected_error="Rule ignores require rules, paths or selectors, and reason",
        ),
        RulesTestCase(
            description="scope must be a list rather than a string",
            config='"model:orders_view"',
            expected_error="Rule ignore selectors must be a list",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_ignore_scope_when_running_sql_commands_then_no_source_or_artifact_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], test_case: RulesTestCase
) -> None:
    model: Path = write_sql_project(
        root=tmp_path,
        sql='select CAST(order_id AS UInt64) as order_id from __source("orders")',
        config='\n[[rules.rule_ignores]]\nrules = ["SQLCLEAN004"]\n'
        'reason = "Reviewed previews"\nselectors = ' + test_case.config + "\n",
    )
    original: bytes = model.read_bytes()
    assert main(["sqt", "lint", "--project-dir", str(tmp_path)]) == 1
    assert test_case.expected_error in capsys.readouterr().err
    assert main(["sqt", "format", "--project-dir", str(tmp_path)]) == 1
    assert test_case.expected_error in capsys.readouterr().err
    assert main(["sqt", "compile", "--project-dir", str(tmp_path), "--no-sql-analysis"]) == 1
    assert test_case.expected_error in capsys.readouterr().err
    assert model.read_bytes() == original
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize(
    "test_case",
    [
        RulesTestCase(
            description="deleted selected model makes the reviewed scope stale",
            expected_error="Unknown selected model 'orders_view'",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_removed_model_when_linting_again_then_selector_ignore_is_not_silently_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], test_case: RulesTestCase
) -> None:
    model: Path = write_sql_project(
        root=tmp_path,
        sql='SELECT CAST(order_id AS UInt64) AS order_id FROM __source("orders") LIMIT 1',
        config='\n[[rules.rule_ignores]]\nrules = ["SQLCLEAN004"]\n'
        'reason = "Reviewed preview"\nselectors = ["model:orders_view"]\n',
    )
    assert main(["sqt", "lint", "--project-dir", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["diagnostics"] == []
    model.unlink()
    assert main(["sqt", "lint", "--project-dir", str(tmp_path), "--json"]) == 1
    assert test_case.expected_error in capsys.readouterr().err


@pytest.mark.parametrize(
    "test_case",
    [
        RulesTestCase(
            description="model ignore excludes owned semantic fixes", config='["model:orders_view"]'
        ),
        RulesTestCase(
            description="pipeline ignore excludes its own semantic fixes",
            config='["pipeline:pl__orders"]',
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_ignored_count_when_fixing_then_only_layout_can_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], test_case: RulesTestCase
) -> None:
    model: Path = write_sql_project(
        root=tmp_path,
        sql='select CAST(COUNT(1) AS UInt64) as total from __source("orders")',
        config='\n[rules]\nselect = ["SQLCLEAN017"]\n'
        '[[rules.rule_ignores]]\nrules = ["SQLCLEAN017"]\n'
        'reason = "Reviewed count style"\nselectors = ' + test_case.config + "\n",
    )
    neighbor: Path = tmp_path / "pipelines" / "pl__reports" / "orders_total.sql"
    neighbor.parent.mkdir()
    neighbor.write_text(
        "MODEL (kind view);\n\n"
        'SELECT CAST(COUNT(1) AS UInt64) AS total FROM __ref("landing_orders")\n'
    )
    assert (
        main(["sqt", "format", "--project-dir", str(tmp_path), "--fix"]) == test_case.expected_exit
    )
    assert "COUNT(1)" in model.read_text()
    assert "COUNT(*)" not in model.read_text()
    assert "SELECT" in model.read_text()
    assert "COUNT(*)" in neighbor.read_text()
    assert "COUNT(1)" not in neighbor.read_text()
    assert not capsys.readouterr().err
