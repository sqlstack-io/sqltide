//! The outer `SELECT` projection list, located without interpreting SQL semantics.

use super::primitives::{
    ScanError, ScanResult, SourceSpan, is_identifier_character, skip_non_code, source_span,
    starts_with, text,
};
use crate::python_text::{python_isspace, python_strip};

const SELECT_KEYWORD: &str = "select";
const FROM_KEYWORD: &str = "from";

/// The trimmed span of each projection in the outer `SELECT` list.
///
/// # Errors
///
/// Returns an error when there is no outer `SELECT`, or for unclosed quotes and comments.
pub fn outer_projection_spans(sql: &[char]) -> ScanResult<Vec<SourceSpan>> {
    let mut projection_start: Option<usize> = None;
    let mut projection_end: usize = sql.len();
    let mut depth: i64 = 0;
    let mut index: usize = 0;
    while index < sql.len() {
        if let Some(keyword_end) = keyword_match(sql, index) {
            if depth == 0 {
                if keyword_end - index == SELECT_KEYWORD.len() {
                    projection_start = Some(keyword_end);
                    index = keyword_end;
                    continue;
                }
                if projection_start.is_some() {
                    projection_end = index;
                    break;
                }
            }
            index = keyword_end;
            continue;
        }
        if let Some(next) = skip_non_code(sql, index)? {
            index = next;
            continue;
        }
        match sql[index] {
            '(' => depth += 1,
            ')' => depth -= 1,
            _ => {}
        }
        index += 1;
    }
    let Some(projection_start) = projection_start else {
        return Err(ScanError::unlocated(
            "Outer SELECT projection list could not be located",
        ));
    };
    split_projection_spans(sql, projection_start, projection_end)
}

/// The end of a whole-word, case-insensitive `select` or `from` at `index`, matching Python's
/// `\bselect\b|\bfrom\b` under `re.IGNORECASE`, which also folds `ſ` to `s`.
fn keyword_match(sql: &[char], index: usize) -> Option<usize> {
    if index > 0 && is_identifier_character(sql[index - 1]) {
        return None;
    }
    [SELECT_KEYWORD, FROM_KEYWORD]
        .into_iter()
        .find_map(|keyword| {
            let end: usize = index + keyword.len();
            let matches: bool = end <= sql.len()
                && keyword
                    .chars()
                    .zip(&sql[index..end])
                    .all(|(expected, actual)| {
                        actual.to_ascii_lowercase() == expected
                            || (expected == 's' && *actual == 'ſ')
                    });
            let bounded: bool = sql
                .get(end)
                .is_none_or(|character| !is_identifier_character(*character));
            (matches && bounded).then_some(end)
        })
}

fn split_projection_spans(sql: &[char], start: usize, end: usize) -> ScanResult<Vec<SourceSpan>> {
    let mut spans: Vec<SourceSpan> = Vec::new();
    let mut depth: i64 = 0;
    let mut projection_start: usize = start;
    let mut index: usize = start;
    while index < end {
        if let Some(next) = skip_non_code(sql, index)? {
            index = next;
            continue;
        }
        match sql[index] {
            '(' | '[' => depth += 1,
            ')' | ']' => depth -= 1,
            ',' if depth == 0 => {
                spans.push(trimmed_span(sql, projection_start, index));
                projection_start = index + 1;
            }
            _ => {}
        }
        index += 1;
    }
    spans.push(trimmed_span(sql, projection_start, end));
    Ok(spans)
}

/// The span of the outer `*` within `start..end`, if there is one.
///
/// # Errors
///
/// Returns an error for unclosed quotes and comments.
pub fn outer_star_span(sql: &[char], start: usize, end: usize) -> ScanResult<Option<SourceSpan>> {
    let mut depth: i64 = 0;
    let mut index: usize = start;
    while index < end {
        if let Some(next) = skip_non_code(sql, index)? {
            index = next;
            continue;
        }
        match sql[index] {
            '(' => depth += 1,
            ')' => depth -= 1,
            '*' if depth == 0 => return Ok(Some(source_span(sql, index, index + 1))),
            _ => {}
        }
        index += 1;
    }
    Ok(None)
}

/// The authored type of the outer `::` cast within `start..end`, if there is one.
///
/// # Errors
///
/// Returns an error when the cast has no type, or for unclosed quotes and comments.
pub fn outer_double_colon_type(
    sql: &[char],
    start: usize,
    end: usize,
) -> ScanResult<Option<String>> {
    let mut depth: i64 = 0;
    let mut cast_index: Option<usize> = None;
    let mut alias_index: Option<usize> = None;
    let mut index: usize = start;
    while index < end {
        if let Some(next) = skip_non_code(sql, index)? {
            index = next;
            continue;
        }
        let character: char = sql[index];
        if character == '(' {
            depth += 1;
        } else if character == ')' {
            depth -= 1;
        } else if depth == 0 && starts_with(sql, index, "::") {
            cast_index = Some(index);
            index += 2;
            continue;
        } else if depth == 0 && cast_index.is_some() && as_keyword_at(sql, index) {
            alias_index = Some(index);
            break;
        }
        index += 1;
    }
    let Some(cast_index) = cast_index else {
        return Ok(None);
    };
    let type_end: usize = alias_index.unwrap_or(end);
    let type_start: usize = (cast_index + 2).min(type_end);
    let authored_type: &[char] = python_strip(&sql[type_start..type_end]);
    if authored_type.is_empty() {
        return Err(ScanError::unlocated(
            "Outer double-colon cast type could not be located",
        ));
    }
    Ok(Some(text(authored_type)))
}

/// Whether a whole-word, case-insensitive `as` starts at `index`. Only ASCII `a` and `s`
/// lowercase to `a` and `s`, so an ASCII comparison matches Python's `str.lower()`.
fn as_keyword_at(sql: &[char], index: usize) -> bool {
    let is_as: bool = sql.get(index..index + 2).is_some_and(|candidate| {
        candidate[0].eq_ignore_ascii_case(&'a') && candidate[1].eq_ignore_ascii_case(&'s')
    });
    let bounded_before: bool = index == 0 || !is_identifier_character(sql[index - 1]);
    let bounded_after: bool = sql
        .get(index + 2)
        .is_none_or(|character| !is_identifier_character(*character));
    is_as && bounded_before && bounded_after
}

fn trimmed_span(sql: &[char], mut start: usize, mut end: usize) -> SourceSpan {
    while start < end && python_isspace(sql[start]) {
        start += 1;
    }
    while end > start && python_isspace(sql[end - 1]) {
        end -= 1;
    }
    source_span(sql, start, end)
}
