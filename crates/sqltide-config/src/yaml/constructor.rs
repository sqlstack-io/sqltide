//! PyYAML's `SafeConstructor`: a node tree to Python-shaped values, with the same failures.
//!
//! Ported from PyYAML 6.0.3 `yaml/constructor.py` (MIT, Copyright (c) 2017-2021 Ingy döt Net,
//! Copyright (c) 2006-2016 Kirill Simonov). Collections are filled the way PyYAML's generators
//! fill them: the safe loader never constructs deep, so each collection is created empty and
//! filled after every node already queued, and the first failure is the one PyYAML reports.

use std::sync::LazyLock;

use regex::Regex;
use sqltide_sqltext::python_text::python_repr;

use crate::yaml::_helpers::python::{
    ascii_encode_error, base64_decode_error, python_float, python_int,
};
use crate::yaml::composer::{MAP_TAG, Node, NodeValue, SEQ_TAG, STR_TAG, VALUE_TAG};
use crate::yaml::errors::YamlError;

/// A constructed value, as the Python type `yaml.safe_load` returns.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    None,
    Bool(bool),
    Int,
    Float,
    Str(String),
    Bytes,
    Date,
    DateTime,
    /// A `list`.
    List(Vec<Value>),
    /// A `tuple`, the items of `!!omap` and `!!pairs`.
    Tuple(Vec<Value>),
    /// A `dict` in insertion order. Equal string keys keep the first position and the last
    /// value; other keys are kept as written.
    Dict(Vec<(Value, Value)>),
    Set,
}

const NULL_TAG: &str = "tag:yaml.org,2002:null";
const BOOL_TAG: &str = "tag:yaml.org,2002:bool";
const INT_TAG: &str = "tag:yaml.org,2002:int";
const FLOAT_TAG: &str = "tag:yaml.org,2002:float";
const BINARY_TAG: &str = "tag:yaml.org,2002:binary";
const TIMESTAMP_TAG: &str = "tag:yaml.org,2002:timestamp";
const OMAP_TAG: &str = "tag:yaml.org,2002:omap";
const PAIRS_TAG: &str = "tag:yaml.org,2002:pairs";
const SET_TAG: &str = "tag:yaml.org,2002:set";

static TIMESTAMP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"\A(?P<year>[0-9][0-9][0-9][0-9])-(?P<month>[0-9][0-9]?)-(?P<day>[0-9][0-9]?)(?:(?:[Tt]|[ \t]+)(?P<hour>[0-9][0-9]?):(?P<minute>[0-9][0-9]):(?P<second>[0-9][0-9])(?:\.(?P<fraction>[0-9]*))?(?:[ \t]*(?P<tz>Z|(?P<tz_sign>[-+])(?P<tz_hour>[0-9][0-9]?)(?::(?P<tz_minute>[0-9][0-9]))?))?)?\n?\z",
    )
    .expect("the timestamp pattern is valid")
});

#[derive(Clone, Copy)]
enum Collection {
    Seq,
    Map,
    Set,
    Omap,
    Pairs,
}

enum Slot {
    Done(Value),
    List(Vec<usize>),
    Tuples(Vec<(usize, usize)>),
    Dict(Vec<(usize, usize)>),
    Set,
}

type Construct<T> = Result<T, YamlError>;

/// Values under construction, by slot, and the collections still to fill.
struct Constructor<'a> {
    slots: Vec<Slot>,
    pending: Vec<(Collection, usize, &'a Node)>,
}

/// `construct_document` of `SafeConstructor`.
pub(crate) fn construct_document(node: &Node) -> Construct<Value> {
    let mut constructor = Constructor {
        slots: Vec::new(),
        pending: Vec::new(),
    };
    let root: usize = constructor.construct_object(node)?;
    while !constructor.pending.is_empty() {
        for (collection, slot, node) in std::mem::take(&mut constructor.pending) {
            constructor.fill(collection, slot, node)?;
        }
    }
    Ok(constructor.value(root))
}

fn constructor_error(problem: String, node: &Node) -> YamlError {
    YamlError::marked(problem, node.start)
}

fn python_error(problem: impl Into<String>, node: &Node) -> YamlError {
    YamlError::internal(problem, node.start)
}

impl<'a> Constructor<'a> {
    fn push(&mut self, slot: Slot) -> usize {
        self.slots.push(slot);
        self.slots.len() - 1
    }

    fn construct_object(&mut self, node: &'a Node) -> Construct<usize> {
        self.construct_tagged(node, &node.tag)
    }

    /// `construct_object` with the tag `flatten_mapping` may have rewritten.
    fn construct_tagged(&mut self, node: &'a Node, tag: &str) -> Construct<usize> {
        let (collection, slot): (Collection, Slot) = match tag {
            NULL_TAG => {
                scalar_text(node)?;
                return Ok(self.push(Slot::Done(Value::None)));
            }
            BOOL_TAG => return Ok(self.push(Slot::Done(construct_bool(node)?))),
            INT_TAG => return Ok(self.push(Slot::Done(construct_int(node)?))),
            FLOAT_TAG => return Ok(self.push(Slot::Done(construct_float(node)?))),
            BINARY_TAG => return Ok(self.push(Slot::Done(construct_binary(node)?))),
            TIMESTAMP_TAG => return Ok(self.push(Slot::Done(construct_timestamp(node)?))),
            STR_TAG => {
                let text: String = scalar_text(node)?.to_owned();
                return Ok(self.push(Slot::Done(Value::Str(text))));
            }
            SEQ_TAG => (Collection::Seq, Slot::List(Vec::new())),
            MAP_TAG => (Collection::Map, Slot::Dict(Vec::new())),
            SET_TAG => (Collection::Set, Slot::Set),
            OMAP_TAG => (Collection::Omap, Slot::Tuples(Vec::new())),
            PAIRS_TAG => (Collection::Pairs, Slot::Tuples(Vec::new())),
            _ => {
                return Err(constructor_error(
                    format!(
                        "could not determine a constructor for the tag {}",
                        python_repr(tag)
                    ),
                    node,
                ));
            }
        };
        let slot: usize = self.push(slot);
        self.pending.push((collection, slot, node));
        Ok(slot)
    }

    /// The rest of a collection constructor's generator, after it yielded the empty value.
    fn fill(&mut self, collection: Collection, slot: usize, node: &'a Node) -> Construct<()> {
        match collection {
            Collection::Seq => {
                let NodeValue::Sequence(items) = &node.value else {
                    return Err(constructor_error(
                        format!("expected a sequence node, but found {}", node.id()),
                        node,
                    ));
                };
                let mut constructed: Vec<usize> = Vec::with_capacity(items.len());
                for item in items {
                    constructed.push(self.construct_object(item)?);
                }
                self.slots[slot] = Slot::List(constructed);
            }
            Collection::Map | Collection::Set => {
                let pairs: Vec<(usize, usize)> = self.construct_mapping(node)?;
                self.slots[slot] = match collection {
                    Collection::Map => Slot::Dict(pairs),
                    _ => Slot::Set,
                };
            }
            Collection::Omap | Collection::Pairs => {
                let pairs: Vec<(usize, usize)> = self.construct_pairs(node)?;
                self.slots[slot] = Slot::Tuples(pairs);
            }
        }
        Ok(())
    }

    /// `SafeConstructor.construct_mapping`: `=` keys become strings.
    fn construct_mapping(&mut self, node: &'a Node) -> Construct<Vec<(usize, usize)>> {
        let NodeValue::Mapping(pairs) = &node.value else {
            return Err(constructor_error(
                format!("expected a mapping node, but found {}", node.id()),
                node,
            ));
        };
        let mut constructed: Vec<(usize, usize)> = Vec::with_capacity(pairs.len());
        for (key_node, value_node) in pairs {
            let key_tag: &str = if key_node.tag == VALUE_TAG {
                STR_TAG
            } else {
                &key_node.tag
            };
            let key: usize = self.construct_tagged(key_node, key_tag)?;
            if matches!(
                self.slots[key],
                Slot::List(_) | Slot::Tuples(_) | Slot::Dict(_) | Slot::Set
            ) {
                return Err(YamlError::marked("found unhashable key", key_node.start));
            }
            let value: usize = self.construct_object(value_node)?;
            constructed.push((key, value));
        }
        Ok(constructed)
    }

    /// `construct_yaml_omap` and `construct_yaml_pairs`, which share their checks.
    fn construct_pairs(&mut self, node: &'a Node) -> Construct<Vec<(usize, usize)>> {
        let NodeValue::Sequence(items) = &node.value else {
            return Err(constructor_error(
                format!("expected a sequence, but found {}", node.id()),
                node,
            ));
        };
        let mut constructed: Vec<(usize, usize)> = Vec::with_capacity(items.len());
        for item in items {
            let NodeValue::Mapping(pairs) = &item.value else {
                return Err(constructor_error(
                    format!("expected a mapping of length 1, but found {}", item.id()),
                    item,
                ));
            };
            let [(key_node, value_node)] = pairs.as_slice() else {
                return Err(constructor_error(
                    format!(
                        "expected a single mapping item, but found {} items",
                        pairs.len()
                    ),
                    item,
                ));
            };
            let key: usize = self.construct_object(key_node)?;
            let value: usize = self.construct_object(value_node)?;
            constructed.push((key, value));
        }
        Ok(constructed)
    }

    fn value(&self, slot: usize) -> Value {
        match &self.slots[slot] {
            Slot::Done(value) => value.clone(),
            Slot::List(items) => Value::List(items.iter().map(|item| self.value(*item)).collect()),
            Slot::Tuples(pairs) => Value::List(
                pairs
                    .iter()
                    .map(|(key, value)| Value::Tuple(vec![self.value(*key), self.value(*value)]))
                    .collect(),
            ),
            Slot::Dict(pairs) => {
                let mut entries: Vec<(Value, Value)> = Vec::with_capacity(pairs.len());
                for (key, value) in pairs {
                    let key: Value = self.value(*key);
                    let value: Value = self.value(*value);
                    match entries
                        .iter_mut()
                        .find(|(existing, _)| matches!(existing, Value::Str(_)) && *existing == key)
                    {
                        Some(entry) => entry.1 = value,
                        None => entries.push((key, value)),
                    }
                }
                Value::Dict(entries)
            }
            Slot::Set => Value::Set,
        }
    }
}

/// `SafeConstructor.construct_scalar`: a mapping stands for the value of its `=` key.
fn scalar_text(node: &Node) -> Construct<&str> {
    match &node.value {
        NodeValue::Scalar(text) => Ok(text),
        NodeValue::Mapping(pairs) => match pairs.iter().find(|(key, _)| key.tag == VALUE_TAG) {
            Some((_, value)) => scalar_text(value),
            None => Err(constructor_error(
                "expected a scalar node, but found mapping".to_owned(),
                node,
            )),
        },
        NodeValue::Sequence(_) => Err(constructor_error(
            "expected a scalar node, but found sequence".to_owned(),
            node,
        )),
    }
}

fn construct_bool(node: &Node) -> Construct<Value> {
    let lowered: String = scalar_text(node)?.to_lowercase();
    match lowered.as_str() {
        "yes" | "true" | "on" => Ok(Value::Bool(true)),
        "no" | "false" | "off" => Ok(Value::Bool(false)),
        _ => Err(python_error(python_repr(&lowered), node)),
    }
}

/// The sign-free text after `value[0]` was read for a sign, as both number constructors do.
fn unsigned(value: &str, node: &Node) -> Construct<String> {
    match value.chars().next() {
        None => Err(python_error("string index out of range", node)),
        Some('+' | '-') => Ok(value[1..].to_owned()),
        Some(_) => Ok(value.to_owned()),
    }
}

fn construct_int(node: &Node) -> Construct<Value> {
    let value: String = unsigned(&scalar_text(node)?.replace('_', ""), node)?;
    let checked: Result<(), String> = if value == "0" {
        Ok(())
    } else if let Some(digits) = value.strip_prefix("0b") {
        python_int(digits, 2)
    } else if let Some(digits) = value.strip_prefix("0x") {
        python_int(digits, 16)
    } else if value.starts_with('0') {
        python_int(&value, 8)
    } else if value.is_empty() {
        return Err(python_error("string index out of range", node));
    } else if value.contains(':') {
        value.split(':').try_for_each(|part| python_int(part, 10))
    } else {
        python_int(&value, 10)
    };
    checked.map_err(|message| python_error(message, node))?;
    Ok(Value::Int)
}

fn construct_float(node: &Node) -> Construct<Value> {
    let value: String = unsigned(&scalar_text(node)?.replace('_', "").to_lowercase(), node)?;
    let checked: Result<(), String> = if value == ".inf" || value == ".nan" {
        Ok(())
    } else if value.contains(':') {
        value.split(':').try_for_each(python_float)
    } else {
        python_float(&value)
    };
    checked.map_err(|message| python_error(message, node))?;
    Ok(Value::Float)
}

fn construct_binary(node: &Node) -> Construct<Value> {
    let text: &str = scalar_text(node)?;
    if let Some(error) = ascii_encode_error(text) {
        return Err(constructor_error(
            format!("failed to convert base64 data into ascii: {error}"),
            node,
        ));
    }
    if let Some(error) = base64_decode_error(text) {
        return Err(constructor_error(
            format!("failed to decode base64 data: {error}"),
            node,
        ));
    }
    Ok(Value::Bytes)
}

fn days_in_month(year: u32, month: u32) -> u32 {
    match month {
        2 if year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400)) => {
            29
        }
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// `datetime.date`'s argument checks.
fn check_date(year: u32, month: u32, day: u32) -> Result<(), String> {
    if !(1..=9999).contains(&year) {
        return Err(format!("year {year} is out of range"));
    }
    if !(1..=12).contains(&month) {
        return Err("month must be in 1..12".to_owned());
    }
    if day < 1 || day > days_in_month(year, month) {
        return Err("day is out of range for month".to_owned());
    }
    Ok(())
}

/// `repr` of a `datetime.timedelta` of whole seconds.
fn timedelta_repr(seconds: i64) -> String {
    let days: i64 = seconds.div_euclid(86_400);
    let remainder: i64 = seconds.rem_euclid(86_400);
    let fields: Vec<String> = [("days", days), ("seconds", remainder)]
        .iter()
        .filter(|(_, amount)| *amount != 0)
        .map(|(name, amount)| format!("{name}={amount}"))
        .collect();
    if fields.is_empty() {
        "datetime.timedelta(0)".to_owned()
    } else {
        format!("datetime.timedelta({})", fields.join(", "))
    }
}

fn construct_timestamp(node: &Node) -> Construct<Value> {
    scalar_text(node)?;
    let NodeValue::Scalar(text) = &node.value else {
        return Err(python_error(
            "expected string or bytes-like object, got 'list'",
            node,
        ));
    };
    let Some(captures) = TIMESTAMP.captures(text) else {
        return Err(python_error(
            "'NoneType' object has no attribute 'groupdict'",
            node,
        ));
    };
    let number = |name: &str| -> u32 {
        captures.name(name).map_or(0, |found| {
            found.as_str().parse().expect("the group holds digits")
        })
    };
    let (year, month, day) = (number("year"), number("month"), number("day"));
    if captures.name("hour").is_none() {
        check_date(year, month, day).map_err(|message| python_error(message, node))?;
        return Ok(Value::Date);
    }
    if let Some(sign) = captures.name("tz_sign") {
        let mut offset: i64 =
            i64::from(number("tz_hour")) * 3_600 + i64::from(number("tz_minute")) * 60;
        if sign.as_str() == "-" {
            offset = -offset;
        }
        if offset.abs() >= 86_400 {
            return Err(python_error(
                format!(
                    "offset must be a timedelta strictly between -timedelta(hours=24) and \
                     timedelta(hours=24), not {}.",
                    timedelta_repr(offset)
                ),
                node,
            ));
        }
    }
    check_date(year, month, day).map_err(|message| python_error(message, node))?;
    let (hour, minute, second) = (number("hour"), number("minute"), number("second"));
    let time_error: Option<&str> = if hour > 23 {
        Some("hour must be in 0..23")
    } else if minute > 59 {
        Some("minute must be in 0..59")
    } else if second > 59 {
        Some("second must be in 0..59")
    } else {
        None
    };
    if let Some(message) = time_error {
        return Err(python_error(message, node));
    }
    Ok(Value::DateTime)
}
