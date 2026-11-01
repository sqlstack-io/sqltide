//! `${name}` project variables and `${ENV:NAME}` environment references in configuration.
//!
//! A string that is one whole token takes the token's value, whatever its shape. Tokens inside
//! longer text are replaced in order, each replacing every occurrence of its text in the result
//! so far, and must be scalars. Variables resolve lazily, once per call, with cycle detection.

use std::collections::{BTreeMap, HashMap};
use std::sync::LazyLock;

use regex::Regex;

static TOKEN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\$\{([^{}]+)\}").expect("the token pattern is valid"));
static WHOLE_TOKEN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A\$\{([^{}]+)\}\z").expect("the token pattern is valid"));

const ENVIRONMENT_PREFIX: &str = "ENV:";
const NAMESPACE_SEPARATOR: char = ':';

/// One configuration value.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Str(String),
    /// A list or tuple; interpolated sequences come back as tuples.
    Seq(Vec<Value>),
    /// A mapping in authored order; interpolated mappings come back sorted by key.
    Map(Vec<(String, Value)>),
    /// Any other value, passed through untouched, with the text it interpolates as.
    Other {
        handle: usize,
        text: String,
    },
}

/// Environment variables, looked up by name.
pub trait Environment {
    fn get(&self, name: &str) -> Option<String>;
}

impl Environment for HashMap<String, String> {
    fn get(&self, name: &str) -> Option<String> {
        HashMap::get(self, name).cloned()
    }
}

/// Resolve every variable, starting each in name order, returned in the order they finish:
/// a variable another one references finishes first.
///
/// # Errors
///
/// Returns the first configuration error met.
pub fn resolve_variable_values(
    values: &BTreeMap<String, Value>,
    environment: &dyn Environment,
    field_path_prefix: &str,
    defer_missing_environment: bool,
) -> Result<Vec<(String, Value)>, String> {
    let mut resolver: Resolver<'_> = Resolver {
        values,
        environment,
        resolved: HashMap::new(),
        finished: Vec::new(),
        field_path_prefix,
        defer_missing_environment,
    };
    for name in values.keys() {
        resolver.variable(name, &[])?;
    }
    Ok(resolver.finished)
}

/// Interpolate one configuration value; missing environment variables are errors.
///
/// # Errors
///
/// Returns the first configuration error met.
pub fn interpolate_config_value(
    value: &Value,
    variables: &BTreeMap<String, Value>,
    environment: &dyn Environment,
    field_path: &str,
) -> Result<Value, String> {
    Resolver {
        values: variables,
        environment,
        resolved: HashMap::new(),
        finished: Vec::new(),
        field_path_prefix: field_path,
        defer_missing_environment: false,
    }
    .value(value, &[], field_path)
}

struct Resolver<'a> {
    values: &'a BTreeMap<String, Value>,
    environment: &'a dyn Environment,
    resolved: HashMap<String, Value>,
    /// Resolved variables in the order they finished.
    finished: Vec<(String, Value)>,
    field_path_prefix: &'a str,
    defer_missing_environment: bool,
}

impl Resolver<'_> {
    fn variable(&mut self, name: &str, stack: &[String]) -> Result<Value, String> {
        if let Some(value) = self.resolved.get(name) {
            return Ok(value.clone());
        }
        let prefix: &str = self.field_path_prefix;
        if stack.iter().any(|entry| entry == name) {
            let cycle: String = stack
                .iter()
                .map(String::as_str)
                .chain([name])
                .collect::<Vec<&str>>()
                .join(" -> ");
            return Err(format!("{prefix} variable interpolation cycle: {cycle}"));
        }
        let Some(raw) = self.values.get(name) else {
            return Err(format!("{prefix} unknown project variable '{name}'"));
        };
        let mut nested: Vec<String> = stack.to_vec();
        nested.push(name.to_owned());
        let value: Value = self.value(raw, &nested, &format!("{prefix} vars.{name}"))?;
        self.resolved.insert(name.to_owned(), value.clone());
        self.finished.push((name.to_owned(), value.clone()));
        Ok(value)
    }

    fn value(
        &mut self,
        value: &Value,
        stack: &[String],
        field_path: &str,
    ) -> Result<Value, String> {
        match value {
            Value::Str(text) => self.string(text, stack, field_path),
            Value::Seq(items) => items
                .iter()
                .enumerate()
                .map(|(index, item)| self.value(item, stack, &format!("{field_path}[{index}]")))
                .collect::<Result<_, _>>()
                .map(Value::Seq),
            Value::Map(entries) => {
                let mut sorted: Vec<&(String, Value)> = entries.iter().collect();
                sorted.sort_by(|left, right| left.0.cmp(&right.0));
                sorted
                    .into_iter()
                    .map(|(key, item)| {
                        Ok((
                            key.clone(),
                            self.value(item, stack, &format!("{field_path}.{key}"))?,
                        ))
                    })
                    .collect::<Result<_, _>>()
                    .map(Value::Map)
            }
            Value::Other { .. } => Ok(value.clone()),
        }
    }

    fn string(&mut self, text: &str, stack: &[String], field_path: &str) -> Result<Value, String> {
        if let Some(whole) = WHOLE_TOKEN.captures(text) {
            return self.token(&whole[1], stack, field_path);
        }
        let mut rendered: String = text.to_owned();
        for found in TOKEN.captures_iter(text) {
            let replacement: String = match self.token(&found[1], stack, field_path)? {
                Value::Str(text) | Value::Other { text, .. } => text,
                Value::Seq(_) | Value::Map(_) => {
                    return Err(format!(
                        "{field_path} cannot interpolate an object or array into text; \
                         consume the value through a macro"
                    ));
                }
            };
            rendered = rendered.replace(&found[0], &replacement);
        }
        Ok(Value::Str(rendered))
    }

    fn token(&mut self, token: &str, stack: &[String], field_path: &str) -> Result<Value, String> {
        if let Some(name) = token.strip_prefix(ENVIRONMENT_PREFIX) {
            if !name.is_empty() {
                if let Some(value) = self.environment.get(name) {
                    return Ok(Value::Str(value));
                }
                if self.defer_missing_environment {
                    return Ok(Value::Str(format!("${{{ENVIRONMENT_PREFIX}{name}}}")));
                }
            }
            return Err(format!(
                "{field_path} references missing environment variable '{name}'"
            ));
        }
        if let Some((namespace, _)) = token.split_once(NAMESPACE_SEPARATOR) {
            return Err(format!(
                "{field_path} uses unsupported interpolation namespace '{namespace}'"
            ));
        }
        if !self.values.contains_key(token) {
            return Err(format!(
                "{field_path} references unknown project variable '{token}'"
            ));
        }
        self.variable(token, stack)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(value: &str) -> Value {
        Value::Str(value.to_owned())
    }

    fn variables(entries: &[(&str, Value)]) -> BTreeMap<String, Value> {
        entries
            .iter()
            .map(|(name, value)| ((*name).to_owned(), value.clone()))
            .collect()
    }

    fn environment() -> HashMap<String, String> {
        HashMap::from([("HOST".to_owned(), "db.example.com".to_owned())])
    }

    #[test]
    fn whole_tokens_keep_their_shape_and_text_tokens_render() {
        let vars = variables(&[
            ("topics", Value::Seq(vec![text("orders")])),
            ("host", text("${ENV:HOST}")),
            (
                "port",
                Value::Other {
                    handle: 0,
                    text: "9000".to_owned(),
                },
            ),
        ]);
        let env = environment();
        let interpolate = |value: Value| interpolate_config_value(&value, &vars, &env, "f");
        assert_eq!(
            interpolate(text("${topics}")),
            Ok(Value::Seq(vec![text("orders")]))
        );
        assert_eq!(
            interpolate(text("${host}:${port}")),
            Ok(text("db.example.com:9000"))
        );
        assert_eq!(
            interpolate(text("x ${topics}")),
            Err("f cannot interpolate an object or array into text; consume the value through a macro".to_owned())
        );
        assert_eq!(
            interpolate(text("${ENV:MISSING}")),
            Err("f references missing environment variable 'MISSING'".to_owned())
        );
        assert_eq!(
            interpolate(text("${env:x}")),
            Err("f uses unsupported interpolation namespace 'env'".to_owned())
        );
    }

    #[test]
    fn replacement_rewrites_every_occurrence_in_the_result_so_far() {
        let vars = variables(&[("a", text("${b}")), ("b", text("B"))]);
        assert_eq!(
            interpolate_config_value(&text("${a} ${b}"), &vars, &environment(), "f"),
            Ok(text("B B"))
        );
    }

    #[test]
    fn resolves_in_name_order_and_reports_cycles() {
        let env = environment();
        let vars = variables(&[("b", text("${a}")), ("a", text("${b}"))]);
        assert_eq!(
            resolve_variable_values(&vars, &env, "p", false),
            Err("p variable interpolation cycle: a -> b -> a".to_owned())
        );
        let vars = variables(&[("a", text("${z}")), ("z", text("${ENV:MISSING}"))]);
        assert_eq!(
            resolve_variable_values(&vars, &env, "p", true),
            Ok(vec![
                ("z".to_owned(), text("${ENV:MISSING}")),
                ("a".to_owned(), text("${ENV:MISSING}")),
            ])
        );
    }
}
