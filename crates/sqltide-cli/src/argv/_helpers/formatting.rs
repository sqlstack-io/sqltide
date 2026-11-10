//! `argparse.HelpFormatter`'s help, usage and version text in the Python 3.12 layout.

use crate::argv::_helpers::text_wrap::normalize_whitespace;
use crate::argv::constants::{
    INDENT_INCREMENT, MAX_HELP_POSITION, MAXIMUM_BLANK_RUN, MINIMUM_TEXT_WIDTH, USAGE_PREFIX,
    VERSION_PLACEHOLDER,
};
use crate::argv::models::{ActionSpec, ArgvSettings, HelpText, Nargs, ParserSpec};
use crate::text::text_wrap::wrap as text_wrap;

/// One formatter's widths: `HelpFormatter.__init__` and the longest invocation it has seen.
struct Formatter {
    width: i64,
    max_help_position: i64,
    action_max_length: i64,
}

impl Formatter {
    fn new(settings: &ArgvSettings) -> Self {
        let width: i64 = settings.columns - 2;
        Self {
            width,
            max_help_position: MAX_HELP_POSITION.min((width - 20).max(INDENT_INCREMENT * 2)),
            action_max_length: 0,
        }
    }
}

/// `parser.format_usage()`.
pub(crate) fn format_usage(settings: &ArgvSettings, parser: &ParserSpec) -> String {
    let formatter: Formatter = Formatter::new(settings);
    finish(&[usage(&formatter, parser)])
}

/// The `--version` action's output.
pub(crate) fn format_version(
    settings: &ArgvSettings,
    action: &ActionSpec,
    version: &str,
) -> String {
    let formatter: Formatter = Formatter::new(settings);
    let text: String = action
        .version
        .as_deref()
        .unwrap_or_default()
        .replace(VERSION_PLACEHOLDER, version);
    finish(&[format_text(&formatter, &text, 0)])
}

/// `parser.format_help()`.
pub(crate) fn format_help(settings: &ArgvSettings, parser: &ParserSpec) -> String {
    let mut formatter: Formatter = Formatter::new(settings);
    for action in parser
        .actions
        .iter()
        .filter(|action| action.help != HelpText::Suppress)
    {
        let entry: HelpEntry = help_entry(action);
        let mut length: i64 = entry.header.chars().count() as i64 + INDENT_INCREMENT;
        for subaction in &entry.subactions {
            length = length.max(subaction.header.chars().count() as i64 + 2 * INDENT_INCREMENT);
        }
        formatter.action_max_length = formatter.action_max_length.max(length);
    }
    let mut items: Vec<String> = vec![usage(&formatter, parser)];
    if let Some(description) = &parser.description {
        items.push(format_text(&formatter, description, 0));
    }
    for (index, group) in parser.groups.iter().enumerate() {
        let mut section: Vec<String> = Vec::new();
        if let Some(description) = &group.description {
            section.push(format_text(&formatter, description, INDENT_INCREMENT));
        }
        for action in parser
            .actions
            .iter()
            .filter(|action| action.group == index && action.help != HelpText::Suppress)
        {
            section.push(format_action(
                &formatter,
                &help_entry(action),
                INDENT_INCREMENT,
            ));
        }
        let body: String = section.concat();
        if body.is_empty() {
            continue;
        }
        let heading: String = group
            .title
            .as_ref()
            .map_or_else(String::new, |title| format!("{title}:\n"));
        items.push(format!("\n{heading}{body}\n"));
    }
    if let Some(epilog) = &parser.epilog {
        items.push(format_text(&formatter, epilog, 0));
    }
    finish(&items)
}

/// The root section's `format_help` followed by `HelpFormatter.format_help`'s clean-up.
fn finish(items: &[String]) -> String {
    let body: String = items.concat();
    if body.is_empty() {
        return String::new();
    }
    let mut text: String = String::with_capacity(body.len() + 2);
    let mut newlines: usize = 0;
    for character in format!("\n{body}\n").chars() {
        if character == '\n' {
            newlines += 1;
            if newlines > MAXIMUM_BLANK_RUN {
                continue;
            }
        } else {
            newlines = 0;
        }
        text.push(character);
    }
    format!("{}\n", text.trim_matches('\n'))
}

fn help_text(help: &HelpText) -> Option<&str> {
    match help {
        HelpText::Text(text) => Some(text),
        HelpText::None | HelpText::Suppress => None,
    }
}

/// `_format_text`: a filled paragraph at `indent`.
fn format_text(formatter: &Formatter, text: &str, indent: i64) -> String {
    let width: i64 = (formatter.width - indent).max(MINIMUM_TEXT_WIDTH);
    let padding: String = " ".repeat(indent as usize);
    let lines: Vec<String> = text_wrap(
        &normalize_whitespace(text),
        width as usize,
        (&padding, &padding),
        true,
    );
    format!("{}\n\n", lines.join("\n"))
}

/// One help line's invocation and text, with its indented sub-actions.
struct HelpEntry {
    header: String,
    help: Option<String>,
    subactions: Vec<HelpEntry>,
}

/// An action's help entry; `_iter_indented_subactions` gives a sub-command action's choices.
fn help_entry(action: &ActionSpec) -> HelpEntry {
    HelpEntry {
        header: invocation(action),
        help: help_text(&action.help).map(str::to_owned),
        subactions: action
            .choices_help
            .iter()
            .map(|choice| HelpEntry {
                header: choice.metavar.clone(),
                help: help_text(&choice.help).map(str::to_owned),
                subactions: Vec::new(),
            })
            .collect(),
    }
}

/// `_format_action`: one action's help entry and its sub-actions'.
fn format_action(formatter: &Formatter, entry: &HelpEntry, indent: i64) -> String {
    let help_position: i64 = (formatter.action_max_length + 2).min(formatter.max_help_position);
    let help_width: i64 = (formatter.width - help_position).max(MINIMUM_TEXT_WIDTH);
    let action_width: i64 = help_position - indent - 2;
    let padding: String = " ".repeat(indent as usize);
    let header: &str = &entry.header;
    let help: Option<&str> = entry.help.as_deref().filter(|text| !text.is_empty());
    let header_length: i64 = header.chars().count() as i64;
    let mut indent_first: i64 = 0;
    let mut text: String = match help {
        None => format!("{padding}{header}\n"),
        Some(_) if header_length <= action_width => {
            format!("{padding}{header:<width$}  ", width = action_width as usize)
        }
        Some(_) => {
            indent_first = help_position;
            format!("{padding}{header}\n")
        }
    };
    let lines: Vec<String> = help
        .map(|help| {
            text_wrap(
                &normalize_whitespace(help),
                help_width as usize,
                ("", ""),
                true,
            )
        })
        .unwrap_or_default();
    if let Some((first, rest)) = lines.split_first() {
        text.push_str(&format!("{}{first}\n", " ".repeat(indent_first as usize)));
        for line in rest {
            text.push_str(&format!("{}{line}\n", " ".repeat(help_position as usize)));
        }
    } else if !text.ends_with('\n') {
        text.push('\n');
    }
    for subaction in &entry.subactions {
        text.push_str(&format_action(
            formatter,
            subaction,
            indent + INDENT_INCREMENT,
        ));
    }
    text
}

/// `_metavar_formatter(action, default)(1)[0]`.
fn metavar(action: &ActionSpec, default: &str) -> String {
    match (&action.metavar, &action.choices) {
        (Some(Some(metavar)), _) => metavar.clone(),
        (Some(None), _) => crate::argv::constants::SUPPRESS_DEST.to_owned(),
        (None, Some(choices)) => format!("{{{}}}", choices.join(",")),
        (None, None) => default.to_owned(),
    }
}

/// `_format_args`.
fn format_args(action: &ActionSpec, default: &str) -> String {
    let name: String = metavar(action, default);
    match action.nargs {
        Nargs::Single => name,
        Nargs::Optional => format!("[{name}]"),
        Nargs::ZeroOrMore => format!("[{name} ...]"),
        Nargs::OneOrMore => format!("{name} [{name} ...]"),
        Nargs::Parser => format!("{name} ..."),
        Nargs::Zero => String::new(),
    }
}

fn dest(action: &ActionSpec) -> &str {
    action
        .dest
        .as_deref()
        .unwrap_or(crate::argv::constants::SUPPRESS_DEST)
}

/// `_format_action_invocation`: every option string with its arguments, comma-separated.
fn invocation(action: &ActionSpec) -> String {
    if action.option_strings.is_empty() {
        return metavar(action, dest(action));
    }
    if action.nargs == Nargs::Zero {
        return action.option_strings.join(", ");
    }
    let arguments: String = format_args(action, &dest(action).to_uppercase());
    action
        .option_strings
        .iter()
        .map(|option| format!("{option} {arguments}"))
        .collect::<Vec<String>>()
        .join(", ")
}

/// `_format_usage`: the `usage:` line, wrapped to the terminal width.
fn usage(formatter: &Formatter, parser: &ParserSpec) -> String {
    let prog: &str = &parser.prog;
    if let Some(usage) = &parser.usage {
        return format!("{USAGE_PREFIX}{}\n\n", usage.replace("%(prog)s", prog));
    }
    if parser.actions.is_empty() {
        return format!("{USAGE_PREFIX}{prog}\n\n");
    }
    let optionals: Vec<&ActionSpec> = parser
        .actions
        .iter()
        .filter(|action| !action.option_strings.is_empty())
        .collect();
    let positionals: Vec<&ActionSpec> = parser
        .actions
        .iter()
        .filter(|action| action.option_strings.is_empty())
        .collect();
    let all: Vec<&ActionSpec> = optionals.iter().chain(&positionals).copied().collect();
    let action_usage: String = actions_usage(parser, &all);
    let text: String = [prog, action_usage.as_str()]
        .iter()
        .filter(|part| !part.is_empty())
        .copied()
        .collect::<Vec<&str>>()
        .join(" ");
    let prefix_length: usize = USAGE_PREFIX.len();
    let text_width: i64 = formatter.width;
    if (prefix_length + text.chars().count()) as i64 <= text_width {
        return format!("{USAGE_PREFIX}{text}\n\n");
    }
    let optional_parts: Vec<String> = usage_parts(&actions_usage(parser, &optionals));
    let positional_parts: Vec<String> = usage_parts(&actions_usage(parser, &positionals));
    let prog_length: usize = prog.chars().count();
    let lines: Vec<String> = if (prefix_length + prog_length) as f64 <= 0.75 * text_width as f64 {
        let indent: String = " ".repeat(prefix_length + prog_length + 1);
        let with_prog = |parts: &[String]| -> Vec<String> {
            let mut all: Vec<String> = vec![prog.to_owned()];
            all.extend(parts.iter().cloned());
            usage_lines(&all, &indent, Some(prefix_length), text_width)
        };
        if !optional_parts.is_empty() {
            let mut lines: Vec<String> = with_prog(&optional_parts);
            lines.extend(usage_lines(&positional_parts, &indent, None, text_width));
            lines
        } else if !positional_parts.is_empty() {
            with_prog(&positional_parts)
        } else {
            vec![prog.to_owned()]
        }
    } else {
        let indent: String = " ".repeat(prefix_length);
        let parts: Vec<String> = optional_parts
            .iter()
            .chain(&positional_parts)
            .cloned()
            .collect();
        let mut lines: Vec<String> = usage_lines(&parts, &indent, None, text_width);
        if lines.len() > 1 {
            lines = usage_lines(&optional_parts, &indent, None, text_width);
            lines.extend(usage_lines(&positional_parts, &indent, None, text_width));
        }
        let mut all: Vec<String> = vec![prog.to_owned()];
        all.extend(lines);
        all
    };
    format!("{USAGE_PREFIX}{}\n\n", lines.join("\n"))
}

/// `get_lines` inside `_format_usage`.
fn usage_lines(
    parts: &[String],
    indent: &str,
    prefix_length: Option<usize>,
    text_width: i64,
) -> Vec<String> {
    let indent_length: i64 = indent.chars().count() as i64;
    let mut lines: Vec<String> = Vec::new();
    let mut line: Vec<&str> = Vec::new();
    let mut line_length: i64 = prefix_length.map_or(indent_length, |length| length as i64) - 1;
    for part in parts {
        let part_length: i64 = part.chars().count() as i64;
        if line_length + 1 + part_length > text_width && !line.is_empty() {
            lines.push(format!("{indent}{}", line.join(" ")));
            line.clear();
            line_length = indent_length - 1;
        }
        line.push(part);
        line_length += part_length + 1;
    }
    if !line.is_empty() {
        lines.push(format!("{indent}{}", line.join(" ")));
    }
    if prefix_length.is_some()
        && let Some(first) = lines.first_mut()
    {
        *first = first.chars().skip(indent_length as usize).collect();
    }
    lines
}

/// `re.findall(r'\(.*?\)+(?=\s|$)|\[.*?\]+(?=\s|$)|\S+', usage)`: the wrappable usage parts.
fn usage_parts(usage: &str) -> Vec<String> {
    let characters: Vec<char> = usage.chars().collect();
    let mut parts: Vec<String> = Vec::new();
    let mut start: usize = 0;
    while start < characters.len() {
        let end: Option<usize> = bracketed_end(&characters, start, '(', ')')
            .or_else(|| bracketed_end(&characters, start, '[', ']'))
            .or_else(|| {
                let run: usize = characters[start..]
                    .iter()
                    .take_while(|character| !is_regex_space(**character))
                    .count();
                (run > 0).then_some(start + run)
            });
        match end {
            Some(end) => {
                parts.push(characters[start..end].iter().collect());
                start = end;
            }
            None => start += 1,
        }
    }
    parts
}

/// The end of `open.*?close+(?=\s|$)` matched at `start`, if it matches there.
fn bracketed_end(characters: &[char], start: usize, open: char, close: char) -> Option<usize> {
    if characters.get(start) != Some(&open) {
        return None;
    }
    for index in start + 1..characters.len() {
        if characters[index] == '\n' {
            return None;
        }
        if characters[index] != close {
            continue;
        }
        let end: usize = index
            + characters[index..]
                .iter()
                .take_while(|character| **character == close)
                .count();
        if characters.get(end).is_none_or(|next| is_regex_space(*next)) {
            return Some(end);
        }
    }
    None
}

/// `re`'s `\s` for `str` patterns.
fn is_regex_space(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

/// `_format_actions_usage`: the usage text of `actions`, with mutually exclusive groups marked.
fn actions_usage(parser: &ParserSpec, actions: &[&ActionSpec]) -> String {
    let mut grouped: Vec<usize> = Vec::new();
    let mut inserts: std::collections::BTreeMap<usize, String> = std::collections::BTreeMap::new();
    for (group, required) in parser.mutex_groups.iter().enumerate() {
        let members: Vec<&ActionSpec> = parser
            .actions
            .iter()
            .filter(|action| action.mutex_group == Some(group))
            .collect();
        let Some(start) = actions.iter().position(|action| action.id == members[0].id) else {
            continue;
        };
        let end: usize = start + members.len();
        let contiguous: bool = end <= actions.len()
            && actions[start..end]
                .iter()
                .zip(&members)
                .all(|(action, member)| action.id == member.id);
        if !contiguous {
            continue;
        }
        grouped.extend(members.iter().map(|action| action.id));
        let exposed: usize = members
            .iter()
            .filter(|action| action.help != HelpText::Suppress)
            .count();
        if exposed == 0 {
            continue;
        }
        let marks: Option<(&str, &str)> = if !required {
            Some(("[", "]"))
        } else if exposed > 1 {
            Some(("(", ")"))
        } else {
            None
        };
        if let Some((open, close)) = marks {
            inserts
                .entry(start)
                .and_modify(|insert| insert.push_str(&format!(" {open}")))
                .or_insert_with(|| open.to_owned());
            inserts
                .entry(end)
                .and_modify(|insert| insert.push_str(close))
                .or_insert_with(|| close.to_owned());
        }
        for index in start + 1..end {
            inserts.insert(index, "|".to_owned());
        }
    }
    let mut parts: Vec<Option<String>> = Vec::with_capacity(actions.len());
    for (index, action) in actions.iter().enumerate() {
        if action.help == HelpText::Suppress {
            parts.push(None);
            if inserts.get(&index).is_some_and(|insert| insert == "|") {
                inserts.remove(&index);
            } else if inserts
                .get(&(index + 1))
                .is_some_and(|insert| insert == "|")
            {
                inserts.remove(&(index + 1));
            }
            continue;
        }
        let in_group: bool = grouped.contains(&action.id);
        if action.option_strings.is_empty() {
            let part: String = format_args(action, dest(action));
            if in_group && part.starts_with('[') && part.ends_with(']') {
                parts.push(Some(part[1..part.len() - 1].to_owned()));
            } else {
                parts.push(Some(part));
            }
            continue;
        }
        let option: &str = &action.option_strings[0];
        let part: String = if action.nargs == Nargs::Zero {
            option.to_owned()
        } else {
            format!(
                "{option} {}",
                format_args(action, &dest(action).to_uppercase())
            )
        };
        if !action.required && !in_group {
            parts.push(Some(format!("[{part}]")));
        } else {
            parts.push(Some(part));
        }
    }
    for (index, insert) in inserts.into_iter().rev() {
        parts.insert(index, Some(insert));
    }
    let text: String = parts
        .into_iter()
        .flatten()
        .collect::<Vec<String>>()
        .join(" ");
    clean_group_marks(&text)
}

/// The `re.sub` clean-ups of `_format_actions_usage`, then `strip()`.
fn clean_group_marks(text: &str) -> String {
    let opens: [char; 2] = ['[', '('];
    let closes: [char; 2] = [']', ')'];
    let characters: Vec<char> = text.chars().collect();
    let mut first: Vec<char> = Vec::with_capacity(characters.len());
    let mut index: usize = 0;
    while index < characters.len() {
        first.push(characters[index]);
        if opens.contains(&characters[index]) && characters.get(index + 1) == Some(&' ') {
            index += 2;
        } else {
            index += 1;
        }
    }
    let mut second: Vec<char> = Vec::with_capacity(first.len());
    index = 0;
    while index < first.len() {
        if first[index] == ' '
            && first
                .get(index + 1)
                .is_some_and(|next| closes.contains(next))
        {
            second.push(first[index + 1]);
            index += 2;
        } else {
            second.push(first[index]);
            index += 1;
        }
    }
    let mut third: Vec<char> = Vec::with_capacity(second.len());
    index = 0;
    while index < second.len() {
        if opens.contains(&second[index]) {
            let after: usize = index
                + 1
                + second[index + 1..]
                    .iter()
                    .take_while(|character| **character == ' ')
                    .count();
            if second.get(after).is_some_and(|next| closes.contains(next)) {
                index = after + 1;
                continue;
            }
        }
        third.push(second[index]);
        index += 1;
    }
    third.into_iter().collect::<String>().trim().to_owned()
}
