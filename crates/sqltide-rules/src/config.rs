//! Strict SQL Rules selection and reviewed exception configuration from `sqltide_project.toml`.

use std::io;
use std::path::PathBuf;

use sqltide_config::models::ConfigValue;
use sqltide_config::toml::main::load_toml::load_toml;
use sqltide_sqltext::python_text::python_isspace;

use crate::RulesFailure;
use crate::engine::{SQLCLEAN_FAMILY, rule_codes};
use crate::policy::validate_policy;

const CROSS_JOIN_CODE: &str = "SQLCLEAN038";
const THRESHOLD_KEYS: [&str; 2] = ["max_literal_length", "max_ranking_order_by"];
const INVALID_PATH_PARTS: [&str; 3] = ["", ".", ".."];
const INVALID_PATH_CHARACTERS: [char; 8] = ['\\', ':', '*', '?', '[', ']', '{', '}'];
const GLOB_CHARACTERS: [char; 4] = ['*', '?', '[', '{'];

/// A reviewed deviation restricted to a path and an exact Rule.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RuleExemption {
    pub rule: String,
    pub path: String,
    pub reason: String,
    pub relation: Option<String>,
    pub cte: Option<String>,
}

/// Explicit resource or path policy, distinct from stale-checked exact exceptions.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RuleIgnore {
    pub rules: Vec<String>,
    pub paths: Vec<String>,
    pub reason: String,
    pub selectors: Vec<String>,
}

/// Omitted selection enables SQLClean's defaults; an empty selection disables Rules.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RulesConfig {
    pub enabled: bool,
    pub select: Option<Vec<String>>,
    pub ignore: Vec<String>,
    pub rule_exceptions: Vec<RuleExemption>,
    pub rule_ignores: Vec<RuleIgnore>,
    /// Threshold overrides in authored order.
    pub thresholds: Vec<(String, i64)>,
}

impl RulesConfig {
    /// The Rule codes this configuration evaluates, in catalogue order. An explicit `SQLCLEAN`
    /// family selection selects opt-in Rules as well as defaults.
    #[must_use]
    pub fn selected_rules(&self) -> Vec<String> {
        if !self.enabled {
            return Vec::new();
        }
        let starts_with_any = |code: &str, prefixes: &[String]| {
            prefixes.iter().any(|prefix| code.starts_with(prefix))
        };
        rule_codes()
            .into_iter()
            .filter(|(code, default)| {
                self.select
                    .as_ref()
                    .map_or(*default, |select| starts_with_any(code, select))
                    && !starts_with_any(code, &self.ignore)
            })
            .map(|(code, _)| code.to_owned())
            .collect()
    }
}

fn rules_error(message: impl Into<String>) -> RulesFailure {
    RulesFailure::Rules(message.into())
}

fn get<'a>(entries: &'a [(String, ConfigValue)], key: &str) -> Option<&'a ConfigValue> {
    entries
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value)
}

fn table<'a>(
    value: &'a ConfigValue,
    allowed: &[&str],
) -> Result<&'a [(String, ConfigValue)], RulesFailure> {
    let ConfigValue::Map(entries) = value else {
        return Err(rules_error("Rules configuration must be a table"));
    };
    let mut unknown: Vec<&str> = entries
        .iter()
        .map(|(key, _)| key.as_str())
        .filter(|key| !allowed.contains(key))
        .collect();
    if !unknown.is_empty() {
        unknown.sort_unstable();
        unknown.dedup();
        return Err(rules_error(format!(
            "Unsupported Rules configuration keys: {}",
            unknown.join(", ")
        )));
    }
    Ok(entries)
}

/// Selectors from a list of strings, rejecting anything but exact codes and the family.
fn selectors(items: &[ConfigValue]) -> Result<Vec<String>, RulesFailure> {
    let texts: Vec<String> = items
        .iter()
        .map(|item| match item {
            ConfigValue::String(text) => Ok(text.clone()),
            _ => Err(rules_error(
                "Rule selectors must be a list of exact codes or SQLCLEAN",
            )),
        })
        .collect::<Result<_, _>>()?;
    check_selectors(&texts)?;
    Ok(texts)
}

fn check_selectors(texts: &[String]) -> Result<(), RulesFailure> {
    let known = rule_codes();
    if texts
        .iter()
        .any(|text| text != SQLCLEAN_FAMILY && !known.iter().any(|(code, _)| code == text))
    {
        return Err(rules_error(
            "Unknown Rule selector; use an exact SQLCLEAN code or SQLCLEAN",
        ));
    }
    Ok(())
}

fn selector_list(value: Option<&ConfigValue>) -> Result<Vec<String>, RulesFailure> {
    match value {
        None => Ok(Vec::new()),
        Some(ConfigValue::List(items)) => selectors(items),
        Some(_) => Err(rules_error(
            "Rule selectors must be a list of exact codes or SQLCLEAN",
        )),
    }
}

/// A non-empty string after Python's whitespace strip.
fn text(value: Option<&ConfigValue>) -> Result<String, RulesFailure> {
    match value {
        Some(ConfigValue::String(text)) if text.chars().any(|c| !python_isspace(c)) => {
            Ok(text.clone())
        }
        _ => Err(rules_error(
            "Rule exception fields must be non-empty strings",
        )),
    }
}

/// `str(Path(project_dir) / path)` for a POSIX path, as `pathlib` normalizes it.
fn python_join(project_dir: &str, path: &str) -> String {
    let joined = if path.starts_with('/') || project_dir == "." {
        path.to_owned()
    } else {
        format!("{project_dir}/{path}")
    };
    let root = if joined.starts_with("//") && !joined.starts_with("///") {
        "//"
    } else if joined.starts_with('/') {
        "/"
    } else {
        ""
    };
    let parts: Vec<&str> = joined
        .split('/')
        .filter(|part| !part.is_empty() && *part != ".")
        .collect();
    if parts.is_empty() {
        return if root.is_empty() {
            ".".to_owned()
        } else {
            root.to_owned()
        };
    }
    format!("{root}{}", parts.join("/"))
}

/// Whether `stat` errors the way `pathlib` treats as \"does not exist\" rather than raising.
fn ignored_stat_error(error: &io::Error) -> bool {
    #[cfg(target_os = "macos")]
    const ELOOP: i32 = 62;
    #[cfg(not(target_os = "macos"))]
    const ELOOP: i32 = 40;
    const EBADF: i32 = 9;
    if cfg!(windows) {
        return matches!(error.kind(), io::ErrorKind::NotFound)
            || matches!(error.raw_os_error(), Some(2 | 3 | 21 | 123 | 1921));
    }
    matches!(
        error.kind(),
        io::ErrorKind::NotFound | io::ErrorKind::NotADirectory
    ) || matches!(error.raw_os_error(), Some(code) if code == ELOOP || code == EBADF)
        // An embedded NUL is Python's ValueError, which `pathlib` also reports as absent.
        || error.raw_os_error().is_none()
}

/// `Path.is_file()` (`require_file`) or `Path.exists()` with `pathlib`'s error handling.
fn probe(path: &str, require_file: bool) -> Result<bool, RulesFailure> {
    match std::fs::metadata(PathBuf::from(path)) {
        Ok(metadata) => Ok(!require_file || metadata.is_file()),
        Err(error) if ignored_stat_error(&error) => Ok(false),
        Err(error) => Err(RulesFailure::Io {
            errno: error.raw_os_error().unwrap_or(0),
            path: path.to_owned(),
        }),
    }
}

fn exemption(value: &ConfigValue, project_dir: &str) -> Result<RuleExemption, RulesFailure> {
    let entries = table(value, &["rule", "path", "reason", "relation", "cte"])?;
    let rule = text(get(entries, "rule"))?;
    let path = text(get(entries, "path"))?;
    let reason = text(get(entries, "reason"))?;
    let relation = get(entries, "relation")
        .map(|value| text(Some(value)))
        .transpose()?;
    let cte = get(entries, "cte")
        .map(|value| text(Some(value)))
        .transpose()?;
    check_selectors(std::slice::from_ref(&rule))?;
    if rule == SQLCLEAN_FAMILY {
        return Err(rules_error("Rule exceptions require an exact Rule code"));
    }
    if path
        .split('/')
        .any(|part| INVALID_PATH_PARTS.contains(&part))
        || path.contains(INVALID_PATH_CHARACTERS)
    {
        return Err(rules_error(
            "Rule exception paths must be exact normalized project-relative files",
        ));
    }
    if !probe(&python_join(project_dir, &path), true)? {
        return Err(rules_error(format!(
            "Rule exception path does not exist: {path}"
        )));
    }
    if cte.is_some() && relation.is_none() {
        return Err(rules_error("Rule exception cte requires relation"));
    }
    if relation.is_some() && rule != CROSS_JOIN_CODE {
        return Err(rules_error("Only SQLCLEAN038 supports relation exceptions"));
    }
    Ok(RuleExemption {
        rule,
        path,
        reason,
        relation,
        cte,
    })
}

fn ignore_entry(value: &ConfigValue, project_dir: &str) -> Result<RuleIgnore, RulesFailure> {
    let entries = table(value, &["rules", "paths", "selectors", "reason"])?;
    let rules = selector_list(get(entries, "rules"))?;
    let selector_values: &[ConfigValue] = match get(entries, "selectors") {
        None => &[],
        Some(ConfigValue::List(items)) => items,
        Some(_) => {
            return Err(rules_error(
                "Rule ignore selectors must be a list of non-empty strings",
            ));
        }
    };
    let scopes: Vec<String> = selector_values
        .iter()
        .map(|selector| text(Some(selector)))
        .collect::<Result<_, _>>()?;
    let paths: Option<&[ConfigValue]> = match get(entries, "paths") {
        None => Some(&[]),
        Some(ConfigValue::List(items)) => Some(items),
        Some(_) => None,
    };
    let Some(paths) =
        paths.filter(|paths| !rules.is_empty() && !(paths.is_empty() && scopes.is_empty()))
    else {
        return Err(rules_error(
            "Rule ignores require rules, paths or selectors, and reason",
        ));
    };
    let normalized: Vec<String> = paths
        .iter()
        .map(|path| text(Some(path)))
        .collect::<Result<_, _>>()?;
    for path in &normalized {
        if !path.contains(GLOB_CHARACTERS) && !probe(&python_join(project_dir, path), false)? {
            return Err(rules_error(format!(
                "Rule ignore path does not exist: {path}"
            )));
        }
    }
    Ok(RuleIgnore {
        rules,
        paths: normalized,
        reason: text(get(entries, "reason"))?,
        selectors: scopes,
    })
}

/// Read the Rules configuration from the retained project text, or the defaults without a
/// project. `project_dir` is the directory relative exception paths resolve against, as
/// `str(Path)` prints it, and `selection` overrides `rules.select` from the command line.
///
/// # Errors
///
/// Returns the first invalid setting, missing path or policy refusal.
pub fn rules_config(
    project_contents: Option<&str>,
    project_dir: &str,
    selection: Option<&[String]>,
) -> Result<RulesConfig, RulesFailure> {
    let project = match project_contents {
        None => ConfigValue::Map(Vec::new()),
        Some(contents) => load_toml(contents).map_err(RulesFailure::Toml)?,
    };
    let ConfigValue::Map(root) = &project else {
        return Err(rules_error("Rules configuration must be a table"));
    };
    let empty = ConfigValue::Map(Vec::new());
    let settings = table(get(root, "settings").unwrap_or(&empty), &["sql_analysis"])?;
    let enabled = match get(settings, "sql_analysis") {
        None => true,
        Some(ConfigValue::Bool(flag)) => *flag,
        Some(_) => return Err(rules_error("settings.sql_analysis must be a boolean")),
    };
    let payload = table(
        get(root, "rules").unwrap_or(&empty),
        &[
            "select",
            "ignore",
            "rule_exceptions",
            "rule_ignores",
            "thresholds",
        ],
    )?;
    let select = match selection {
        Some(selection) => {
            check_selectors(selection)?;
            Some(selection.to_vec())
        }
        None => match get(payload, "select") {
            None => None,
            Some(value) => Some(match value {
                ConfigValue::List(items) => selectors(items)?,
                _ => {
                    return Err(rules_error(
                        "Rule selectors must be a list of exact codes or SQLCLEAN",
                    ));
                }
            }),
        },
    };
    let ignore = selector_list(get(payload, "ignore"))?;
    let exceptions: &[ConfigValue] = match get(payload, "rule_exceptions") {
        None => &[],
        Some(ConfigValue::List(items)) => items,
        Some(_) => {
            return Err(rules_error(
                "rules.rule_exceptions must be an array of tables",
            ));
        }
    };
    let ignores: &[ConfigValue] = match get(payload, "rule_ignores") {
        None => &[],
        Some(ConfigValue::List(items)) => items,
        Some(_) => return Err(rules_error("rules.rule_ignores must be an array of tables")),
    };
    let thresholds: Vec<(String, i64)> = match get(payload, "thresholds") {
        None => Vec::new(),
        Some(ConfigValue::Map(entries)) => entries
            .iter()
            .map(|(key, value)| match value {
                ConfigValue::Integer(number)
                    if THRESHOLD_KEYS.contains(&key.as_str()) && *number >= 1 =>
                {
                    Ok((key.clone(), *number))
                }
                _ => Err(()),
            })
            .collect::<Result<_, _>>()
            .map_err(|()| {
                rules_error(
                    "rules.thresholds requires known thresholds with positive integer values",
                )
            })?,
        Some(_) => {
            return Err(rules_error(
                "rules.thresholds requires known thresholds with positive integer values",
            ));
        }
    };
    let config = RulesConfig {
        enabled,
        select,
        ignore,
        rule_exceptions: exceptions
            .iter()
            .map(|item| exemption(item, project_dir))
            .collect::<Result<_, _>>()?,
        rule_ignores: ignores
            .iter()
            .map(|item| ignore_entry(item, project_dir))
            .collect::<Result<_, _>>()?,
        thresholds,
    };
    validate_policy(&config)?;
    Ok(config)
}

#[cfg(test)]
mod tests {
    use super::{python_join, rules_config};
    use crate::RulesFailure;

    #[test]
    fn joins_paths_as_pathlib_prints_them() {
        assert_eq!(python_join("/p", "a//b/./c/"), "/p/a/b/c");
        assert_eq!(python_join(".", "a"), "a");
        assert_eq!(python_join("/p", "/etc"), "/etc");
        assert_eq!(python_join("/p", "//x/y"), "//x/y");
        assert_eq!(python_join("/p", "///x"), "/x");
        assert_eq!(python_join("/p", "../q"), "/p/../q");
    }

    #[test]
    fn checks_settings_before_rules_and_reports_unknown_keys_sorted() {
        let error = rules_config(
            Some("[rules]\nzeta = 1\nalpha = 2\n[settings]\nsql_analysis = 1\n"),
            "/nowhere",
            None,
        )
        .unwrap_err();
        assert!(
            matches!(&error, RulesFailure::Rules(message) if message == "settings.sql_analysis must be a boolean")
        );
        let error =
            rules_config(Some("[rules]\nzeta = 1\nalpha = 2\n"), "/nowhere", None).unwrap_err();
        assert!(
            matches!(&error, RulesFailure::Rules(message) if message == "Unsupported Rules configuration keys: alpha, zeta")
        );
    }

    #[test]
    fn an_empty_selection_selects_nothing_and_the_family_selects_opt_in_rules() {
        let none = rules_config(None, "/nowhere", Some(&[])).unwrap();
        assert!(none.selected_rules().is_empty());
        let family = rules_config(None, "/nowhere", Some(&["SQLCLEAN".to_owned()])).unwrap();
        let defaults = rules_config(None, "/nowhere", None).unwrap();
        assert!(family.selected_rules().len() > defaults.selected_rules().len());
    }
}
