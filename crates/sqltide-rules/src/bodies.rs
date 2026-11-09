//! Adapt retained authored bodies and compiler expansions to SQLClean placeholders. All offsets are
//! Unicode character offsets.

use sqltide_sqltext::model_header::{HeaderValue, parse_model_header};
use sqltide_sqltext::python_text::{python_isspace, python_strip};
use sqltide_sqltext::sql_scan::{extract_references, split_header_blocks};

use crate::engine::template_sites;
use crate::{RuleFile, RulesFailure, line_and_column};

/// One `(before start, before end, after start, after end)` rewrite of a reversible pass.
pub type Span = (i64, i64, i64, i64);

/// SQL body and its reversible expansion passes.
#[derive(Clone, Debug)]
pub struct LintBody {
    /// Index of the authored file in the caller's file list.
    pub file: usize,
    pub start: i64,
    pub end: i64,
    pub sql: Vec<char>,
    pub model: bool,
    pub enabled: bool,
    pub passes: Vec<Vec<Span>>,
    pub dependencies: Vec<String>,
    /// Placeholder token to relation name, in first-assignment order.
    pub relation_names: Vec<(String, String)>,
    pub external_identifiers: Vec<String>,
    pub macro_outputs: Vec<(i64, i64)>,
    pub fixture: bool,
}

fn index(value: usize) -> i64 {
    i64::try_from(value).unwrap_or(i64::MAX)
}

/// The SQL after each `keyword (...);` header in `file`. Headers stay byte-for-byte unchanged.
///
/// # Errors
///
/// Returns the scan or header syntax error, or a non-boolean `sql_analysis`.
pub fn authored_bodies(
    files: &[RuleFile],
    file: usize,
    keyword: &str,
) -> Result<Vec<LintBody>, RulesFailure> {
    let source = &files[file];
    let mut bodies = Vec::new();
    for block in split_header_blocks(&source.contents, keyword)? {
        let fields = parse_model_header(&block.header).map_err(RulesFailure::Value)?;
        check_integer_digits(&fields)?;
        let enabled = match fields.iter().rev().find(|(key, _)| key == "sql_analysis") {
            None => true,
            Some((_, HeaderValue::Bool(flag))) => *flag,
            Some(_) => {
                return Err(RulesFailure::Rules(format!(
                    "{}: sql_analysis must be a boolean",
                    source.path
                )));
            }
        };
        let raw: Vec<char> = block.body.chars().collect();
        let leading = raw
            .iter()
            .position(|character| !python_isspace(*character))
            .unwrap_or(raw.len());
        let sql: Vec<char> = python_strip(&raw).to_vec();
        let start = index(block.body_start + leading);
        bodies.push(LintBody {
            file,
            start,
            end: start + index(sql.len()),
            sql,
            model: keyword == "MODEL",
            enabled,
            passes: Vec::new(),
            dependencies: Vec::new(),
            relation_names: Vec::new(),
            external_identifiers: Vec::new(),
            macro_outputs: Vec::new(),
            fixture: keyword == "TEST",
        });
    }
    Ok(bodies)
}

/// Prepared SQL, `(token, original text)` pairs in site order, and the rewrite spans.
pub type Protected = (Vec<char>, Vec<(String, String)>, Vec<Span>);

/// CPython's default limit on the digits `int()` converts from text.
const PYTHON_INT_MAX_STR_DIGITS: usize = 4300;

/// Refuse a header integer longer than `int()` converts, as the header's Python value did.
fn check_integer_digits(entries: &[(String, HeaderValue)]) -> Result<(), RulesFailure> {
    fn visit(value: &HeaderValue) -> Result<(), RulesFailure> {
        match value {
            HeaderValue::Integer(text) => {
                let digits = text.trim_start_matches(['+', '-']).len();
                if digits > PYTHON_INT_MAX_STR_DIGITS {
                    return Err(RulesFailure::Value(format!(
                        "Exceeds the limit ({PYTHON_INT_MAX_STR_DIGITS} digits) for integer \
                         string conversion: value has {digits} digits; use \
                         sys.set_int_max_str_digits() to increase the limit"
                    )));
                }
                Ok(())
            }
            HeaderValue::List(items) | HeaderValue::Set(items) => items.iter().try_for_each(visit),
            HeaderValue::Map(entries) => check_integer_digits(entries),
            _ => Ok(()),
        }
    }
    entries.iter().try_for_each(|(_, value)| visit(value))
}

/// SQL with each macro or relation call replaced by an opaque token, the token to original text
/// pairs in site order, and the rewrite spans.
///
/// # Errors
///
/// Returns the scan failure for unclosed syntax.
pub fn protected_sql(
    sql: &[char],
    relation_sites: &[(usize, usize)],
) -> Result<Protected, RulesFailure> {
    let sites = template_sites(sql)?;
    let text: String = sql.iter().collect();
    let mut prefix = String::from("__sqlclean_interpolation_");
    while text.contains(&prefix) {
        prefix.push('_');
    }
    let mut replacements: Vec<(String, String)> = Vec::with_capacity(sites.len());
    let mut spans: Vec<Span> = Vec::with_capacity(sites.len());
    let mut prepared: Vec<char> = Vec::with_capacity(sql.len());
    let mut cursor = 0;
    for (position, (start, end)) in sites.into_iter().enumerate() {
        let token = if relation_sites.contains(&(start, end)) {
            format!("__sqlclean_relation_parameter_{position}__")
        } else {
            format!("{prefix}{position}__")
        };
        prepared.extend_from_slice(&sql[cursor..start]);
        let output = index(prepared.len());
        let width = index(token.chars().count());
        spans.push((index(start), index(end), output, output + width));
        prepared.extend(token.chars());
        replacements.push((token, sql[start..end].iter().collect()));
        cursor = end;
    }
    prepared.extend_from_slice(&sql[cursor..]);
    Ok((prepared, replacements, spans))
}

/// Map a range in the prepared SQL back to the authored file. Diagnostics map onto macro calls,
/// but an edit that intersects generated text returns `None`.
#[must_use]
pub fn source_range(body: &LintBody, start: i64, end: i64, edit: bool) -> Option<(i64, i64)> {
    let (mut start, mut end) = (start, end);
    for spans in body.passes.iter().rev() {
        let mut shift: i64 = 0;
        let mut mapped_start = start;
        let mut mapped_end = end;
        for &(before_start, before_end, after_start, after_end) in spans {
            let intersects = if start == end {
                after_start <= start && start < after_end
            } else {
                start < after_end && end > after_start
            };
            if intersects && edit {
                return None;
            }
            let delta = before_end - before_start - (after_end - after_start);
            if after_start <= start && start < after_end {
                mapped_start = before_start;
            } else if start >= after_end {
                mapped_start = start + shift + delta;
            }
            if after_start < end && end <= after_end {
                mapped_end = before_end;
            } else if end >= after_end {
                mapped_end = end + shift + delta;
            }
            shift += delta;
        }
        start = mapped_start;
        end = mapped_end;
    }
    Some((body.start + start, body.start + end))
}

fn forward(position: i64, spans: &[Span], right: bool) -> i64 {
    let mut shift: i64 = 0;
    for &(before_start, before_end, after_start, after_end) in spans {
        if position < before_start {
            break;
        }
        if position < before_end {
            return if right { after_end } else { after_start };
        }
        shift += after_end - after_start - (before_end - before_start);
    }
    position + shift
}

/// Replace relation and macro calls with SQLClean placeholders and record what each one stands
/// for. `body.passes` must already hold the macro expansion pass.
///
/// # Errors
///
/// Returns the scan failure for unclosed syntax.
pub fn neutralized(mut body: LintBody) -> Result<LintBody, RulesFailure> {
    let references = extract_references(&body.sql)?;
    let relation_sites: Vec<(usize, usize)> = references
        .iter()
        .filter(|reference| reference.name.starts_with('@'))
        .map(|reference| (reference.span.start, reference.span.end))
        .collect();
    let (prepared, replacements, spans) = protected_sql(&body.sql, &relation_sites)?;
    let mut names: Vec<(String, String)> = Vec::new();
    for reference in &references {
        let text: String = slice(&body.sql, reference.span.start, reference.span.end);
        for (token, original) in &replacements {
            if text == *original {
                match names.iter_mut().find(|(existing, _)| existing == token) {
                    Some(entry) => entry.1.clone_from(&reference.name),
                    None => names.push((token.clone(), reference.name.clone())),
                }
            }
        }
    }
    body.macro_outputs = body.passes[0]
        .iter()
        .map(|&(_, _, start, end)| (forward(start, &spans, false), forward(end, &spans, true)))
        .collect();
    body.dependencies = names.iter().map(|(token, _)| token.clone()).collect();
    body.external_identifiers = replacements
        .iter()
        .map(|(token, _)| token)
        .filter(|token| !names.iter().any(|(name, _)| name == *token))
        .cloned()
        .collect();
    body.relation_names = names;
    body.passes.push(spans);
    body.sql = prepared;
    Ok(body)
}

/// Python's `sql[start:end]` for in-range offsets.
fn slice(sql: &[char], start: usize, end: usize) -> String {
    let end = end.min(sql.len());
    sql[start.min(end)..end].iter().collect()
}

/// The bodies of every authored file, with this compile's macro expansion of each one, ready for
/// SQLClean. `files` pairs each file index with its header keyword, in compile order, and
/// `expansion(body, line, column)` returns the expanded SQL and spans the compile recorded for
/// the body at that line and column.
///
/// # Errors
///
/// Returns the first scan, header or lookup failure.
pub fn lint_bodies<E: From<RulesFailure>>(
    files: &[RuleFile],
    order: &[(usize, &str)],
    mut expansion: impl FnMut(&LintBody, i64, i64) -> Result<Option<(Vec<char>, Vec<Span>)>, E>,
) -> Result<Vec<LintBody>, E> {
    let mut bodies = Vec::new();
    for &(file, keyword) in order {
        for mut body in authored_bodies(files, file, keyword)? {
            let (line, column) = line_and_column(&files[file].contents, body.start);
            let (sql, spans) =
                expansion(&body, line, column)?.unwrap_or((body.sql.clone(), Vec::new()));
            body.sql = sql;
            body.passes = vec![spans];
            bodies.push(neutralized(body)?);
        }
    }
    Ok(bodies)
}

#[cfg(test)]
mod tests {
    use super::{LintBody, forward, source_range};

    fn body(passes: Vec<Vec<super::Span>>) -> LintBody {
        LintBody {
            file: 0,
            start: 100,
            end: 200,
            sql: Vec::new(),
            model: true,
            enabled: true,
            passes,
            dependencies: Vec::new(),
            relation_names: Vec::new(),
            external_identifiers: Vec::new(),
            macro_outputs: Vec::new(),
            fixture: false,
        }
    }

    #[test]
    fn maps_ranges_through_passes_and_refuses_edits_over_generated_text() {
        // `@m()` (10..14) expanded to 20 characters (10..30).
        let expanded = body(vec![vec![(10, 14, 10, 30)]]);
        assert_eq!(source_range(&expanded, 35, 40, false), Some((119, 124)));
        assert_eq!(source_range(&expanded, 12, 32, false), Some((110, 116)));
        assert_eq!(source_range(&expanded, 12, 32, true), None);
        assert_eq!(source_range(&expanded, 30, 30, true), Some((114, 114)));
        assert_eq!(source_range(&expanded, 5, 8, true), Some((105, 108)));
    }

    #[test]
    fn forwards_positions_past_rewritten_spans() {
        let spans = [(2, 5, 2, 10), (8, 9, 13, 20)];
        assert_eq!(forward(1, &spans, false), 1);
        assert_eq!(forward(3, &spans, false), 2);
        assert_eq!(forward(3, &spans, true), 10);
        assert_eq!(forward(6, &spans, false), 11);
        assert_eq!(forward(12, &spans, false), 23);
    }
}
