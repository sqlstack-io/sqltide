use super::*;

fn chars(sql: &str) -> Vec<char> {
    sql.chars().collect()
}

#[test]
fn references_skip_comments_and_quotes() {
    let sql = chars("SELECT 1 -- __ref(a)\nFROM __ref('b') JOIN '__ref(c)' JOIN __source(d)");
    let references = extract_references(&sql).expect("valid");
    let names: Vec<&str> = references.iter().map(|r| r.name.as_str()).collect();
    assert_eq!(names, ["b", "d"]);
    assert_eq!(references[1].relation_type, "source");
    assert_eq!(references[0].span.line, 2);
}

#[test]
fn ref_type_is_parsed_and_rejected_on_source() {
    let sql = chars("__ref(a, ref_type='mutable')");
    assert_eq!(
        extract_references(&sql).expect("valid")[0].ref_type,
        Some("mutable")
    );
    let error = extract_references(&chars("__source(a, ref_type='mutable')")).expect_err("bad");
    assert_eq!(error.message, "__source(...) must not declare ref_type");
}

#[test]
fn header_blocks_require_a_semicolon() {
    let blocks = split_header_blocks(&chars("MODEL (a 1);\nSELECT 1"), "MODEL").expect("valid");
    assert_eq!(blocks[0].header, "a 1");
    assert_eq!(blocks[0].body, "\nSELECT 1");
    let error = split_header_blocks(&chars("MODEL (a 1)\nSELECT 1"), "MODEL").expect_err("bad");
    assert_eq!(error.message, "MODEL header must end with a semicolon");
}

#[test]
fn ctes_report_missing_with_and_duplicates() {
    let ctes = extract_top_level_ctes(
        &chars("WITH a AS (SELECT 1), b (x) AS (SELECT 2) SELECT 1"),
        "SQL test",
    )
    .expect("valid");
    assert_eq!(ctes.ctes.len(), 2);
    assert_eq!(ctes.trailing_sql, "SELECT 1");
    let missing = extract_top_level_ctes(&chars("SELECT 1"), "SQL test").expect_err("bad");
    assert_eq!(missing.kind, ScanErrorKind::MissingWithClause);
    let duplicate = extract_top_level_ctes(&chars("WITH a AS (1), a AS (2) SELECT 1"), "SQL test")
        .expect_err("bad");
    assert_eq!(duplicate.message, "SQL test defines duplicate CTE 'a'");
}

#[test]
fn projections_split_at_outer_commas_only() {
    let sql = chars("SELECT f(a, b)::Int32 AS x, 'p,q' AS y, [1, 2]::Array(UInt8) AS z FROM t");
    let spans = outer_projection_spans(&sql).expect("valid");
    let texts: Vec<String> = spans
        .iter()
        .map(|s| sql[s.start..s.end].iter().collect())
        .collect();
    assert_eq!(
        texts,
        [
            "f(a, b)::Int32 AS x",
            "'p,q' AS y",
            "[1, 2]::Array(UInt8) AS z"
        ]
    );
    let cast = outer_double_colon_type(&sql, spans[0].start, spans[0].end).expect("valid");
    assert_eq!(cast.as_deref(), Some("Int32"));
}

#[test]
fn macro_starts_skip_emails_quotes_and_bare_names() {
    let sql = chars("x@b(2), '@c(3)', @d, @e (5)");
    assert_eq!(find_next_macro_start(&sql, 0).expect("valid"), Some(21));
    assert_eq!(find_next_macro_start(&sql, 22).expect("valid"), None);
}

#[test]
fn template_predicate_is_conjoined_before_boundary_keywords() {
    let out = append_template_predicate(&chars("SELECT 1 FROM t WHERE a = 1 GROUP BY b"), "c")
        .expect("valid");
    assert_eq!(out, "SELECT 1 FROM t WHERE (a = 1) AND (c)\nGROUP BY b");
}

#[test]
fn unicode_identifier_predicates_follow_python() {
    // U+0345 is alphabetic in Rust but not in Python, so it is not an identifier character.
    let sql = chars("\u{345}__ref(a)");
    assert_eq!(extract_references(&sql).expect("valid").len(), 1);
    let sql = chars("é__ref(a)");
    assert!(extract_references(&sql).expect("valid").is_empty());
}
