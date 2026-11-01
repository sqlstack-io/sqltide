//! Credential redaction for persisted run statements. Only statements that create a Kafka engine
//! carry broker credentials; their quoted sensitive settings are replaced before storage.

use std::sync::LazyLock;

use regex::Regex;

use crate::metadata::sha256_hex;

/// The patterns keep Python `re` semantics: `\s` also matches `\x1C`-`\x1F`, case-insensitive `i`
/// and `k` also match `İ`, `ı` and the Kelvin sign, and word boundaries are checked separately by
/// [`is_word_boundary`] because Python's word characters are letters, numbers and `_`, taken from
/// Unicode 15 like the pinned Python 3.12 so a newer letter never hides a setting from redaction.
static KAFKA_ENGINE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"[Ee][Nn][Gg][Ii\x{130}\x{131}][Nn][Ee][\s\x1C-\x1F]*=[\s\x1C-\x1F]*[Kk\x{212A}][Aa][Ff][Kk\x{212A}][Aa]",
    )
    .expect("valid Kafka engine pattern")
});
static QUOTED_SETTING: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?P<prefix>(?P<key>[A-Za-z_][A-Za-z0-9_]*)[\s\x1C-\x1F]*=[\s\x1C-\x1F]*)'(?P<value>(?:\\.|''|[^'])*)'",
    )
    .expect("valid quoted setting pattern")
});
static WORD_CHARACTER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(?:[\p{L}\p{N}&&\p{Age=15.0}]|_)").expect("valid word character pattern")
});
const SENSITIVE_SETTING_FRAGMENTS: [&str; 6] = [
    "credential",
    "password",
    "private_key",
    "sasl",
    "secret",
    "token",
];
const KAFKA_BROKER_LIST_SETTING: &str = "kafka_broker_list";
const REDACTED_SECRET_PLACEHOLDER: &str = "***";

/// `sql` with every credential-bearing quoted setting of a Kafka engine replaced.
#[must_use]
pub fn redact_statement_sql(sql: &str) -> String {
    if !bounded_matches(&KAFKA_ENGINE, sql)
        .any(|(start, end)| is_word_boundary(sql, end) && is_word_boundary(sql, start))
    {
        return sql.to_owned();
    }
    let mut redacted: String = String::with_capacity(sql.len());
    let mut copied: usize = 0;
    let mut position: usize = 0;
    while let Some(captures) = QUOTED_SETTING.captures_at(sql, position) {
        let whole = captures.get(0).expect("a match has a whole group");
        if !is_word_boundary(sql, whole.start()) {
            position = captures.name("key").expect("a match has a key").end();
            continue;
        }
        let key: String = captures["key"].to_ascii_lowercase();
        let sensitive: bool = SENSITIVE_SETTING_FRAGMENTS
            .iter()
            .any(|fragment| key.contains(fragment))
            || (key == KAFKA_BROKER_LIST_SETTING && captures["value"].contains('@'));
        if sensitive {
            redacted.push_str(&sql[copied..whole.start()]);
            redacted.push_str(&captures["prefix"]);
            redacted.push('\'');
            redacted.push_str(REDACTED_SECRET_PLACEHOLDER);
            redacted.push('\'');
            copied = whole.end();
        }
        position = whole.end();
    }
    redacted.push_str(&sql[copied..]);
    redacted
}

/// Every candidate match of `pattern`, trying each start position in order like Python's search.
fn bounded_matches<'a>(
    pattern: &'a Regex,
    sql: &'a str,
) -> impl Iterator<Item = (usize, usize)> + 'a {
    let mut position: usize = 0;
    std::iter::from_fn(move || {
        if position > sql.len() {
            return None;
        }
        let found = pattern.find_at(sql, position)?;
        position = next_character(sql, found.start());
        Some((found.start(), found.end()))
    })
}

fn next_character(sql: &str, position: usize) -> usize {
    sql[position..]
        .chars()
        .next()
        .map_or(sql.len() + 1, |character| position + character.len_utf8())
}

/// Whether `position` sits between a word character and a non-word character.
fn is_word_boundary(sql: &str, position: usize) -> bool {
    let before: bool = sql[..position]
        .chars()
        .next_back()
        .is_some_and(|character| WORD_CHARACTER.is_match(character.encode_utf8(&mut [0; 4])));
    let after: bool = WORD_CHARACTER.is_match(&sql[position..]);
    before != after
}

/// The redacted statements of one workflow and the hash they are stored under: the original
/// workflow hash when nothing changed, otherwise the hash of the redacted statements.
#[must_use]
pub fn redact_workflow_statements(sqls: &[String], workflow_sha256: &str) -> (Vec<String>, String) {
    let redacted: Vec<String> = sqls.iter().map(|sql| redact_statement_sql(sql)).collect();
    let digest: String = if redacted == sqls {
        workflow_sha256.to_owned()
    } else {
        sha256_hex(&redacted.join("\n"))
    };
    (redacted, digest)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn letters_newer_than_unicode_15_do_not_hide_settings() {
        assert_eq!(
            redact_statement_sql(
                "ENGINE = Kafka\u{105C0} SETTINGS kafka_sasl_password = 'p', \u{105C0}token = 't'"
            ),
            "ENGINE = Kafka\u{105C0} SETTINGS kafka_sasl_password = '***', \u{105C0}token = '***'"
        );
    }
}
