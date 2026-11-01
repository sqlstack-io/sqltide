//! Line diffs that match Python's `difflib.unified_diff` byte for byte.
//!
//! Plans store these diffs and the UI shows them, so hunks must stay identical to the
//! `SequenceMatcher` output earlier releases produced, including its popular-line heuristic.

use std::collections::HashMap;

const CONTEXT_LINES: usize = 3;
const AUTOJUNK_MIN_LENGTH: usize = 200;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Tag {
    Equal,
    Replace,
    Delete,
    Insert,
}

#[derive(Clone, Copy)]
struct Opcode {
    tag: Tag,
    i1: usize,
    i2: usize,
    j1: usize,
    j2: usize,
}

struct Matcher<'a> {
    a: &'a [&'a str],
    b: &'a [&'a str],
    b2j: HashMap<&'a str, Vec<usize>>,
}

impl<'a> Matcher<'a> {
    fn new(a: &'a [&'a str], b: &'a [&'a str]) -> Self {
        let mut b2j: HashMap<&'a str, Vec<usize>> = HashMap::new();
        for (index, line) in b.iter().enumerate() {
            b2j.entry(line).or_default().push(index);
        }
        if b.len() >= AUTOJUNK_MIN_LENGTH {
            let limit = b.len() / 100 + 1;
            b2j.retain(|_, indexes| indexes.len() <= limit);
        }
        Self { a, b, b2j }
    }

    fn longest_match(
        &self,
        alo: usize,
        ahi: usize,
        blo: usize,
        bhi: usize,
    ) -> (usize, usize, usize) {
        let (mut best_i, mut best_j, mut best_size) = (alo, blo, 0);
        let mut lengths: HashMap<usize, usize> = HashMap::new();
        for i in alo..ahi {
            let mut next: HashMap<usize, usize> = HashMap::new();
            if let Some(indexes) = self.b2j.get(self.a[i]) {
                for &j in indexes {
                    if j < blo {
                        continue;
                    }
                    if j >= bhi {
                        break;
                    }
                    let size = j
                        .checked_sub(1)
                        .and_then(|previous| lengths.get(&previous))
                        .copied()
                        .unwrap_or(0)
                        + 1;
                    next.insert(j, size);
                    if size > best_size {
                        (best_i, best_j, best_size) = (i + 1 - size, j + 1 - size, size);
                    }
                }
            }
            lengths = next;
        }
        while best_i > alo && best_j > blo && self.a[best_i - 1] == self.b[best_j - 1] {
            (best_i, best_j, best_size) = (best_i - 1, best_j - 1, best_size + 1);
        }
        while best_i + best_size < ahi
            && best_j + best_size < bhi
            && self.a[best_i + best_size] == self.b[best_j + best_size]
        {
            best_size += 1;
        }
        (best_i, best_j, best_size)
    }

    fn matching_blocks(&self) -> Vec<(usize, usize, usize)> {
        let mut queue = vec![(0, self.a.len(), 0, self.b.len())];
        let mut blocks = Vec::new();
        while let Some((alo, ahi, blo, bhi)) = queue.pop() {
            let (i, j, size) = self.longest_match(alo, ahi, blo, bhi);
            if size == 0 {
                continue;
            }
            blocks.push((i, j, size));
            if alo < i && blo < j {
                queue.push((alo, i, blo, j));
            }
            if i + size < ahi && j + size < bhi {
                queue.push((i + size, ahi, j + size, bhi));
            }
        }
        blocks.sort_unstable();
        let mut collapsed = Vec::new();
        let (mut i1, mut j1, mut k1) = (0, 0, 0);
        for (i2, j2, k2) in blocks {
            if i1 + k1 == i2 && j1 + k1 == j2 {
                k1 += k2;
            } else {
                if k1 > 0 {
                    collapsed.push((i1, j1, k1));
                }
                (i1, j1, k1) = (i2, j2, k2);
            }
        }
        if k1 > 0 {
            collapsed.push((i1, j1, k1));
        }
        collapsed.push((self.a.len(), self.b.len(), 0));
        collapsed
    }

    fn opcodes(&self) -> Vec<Opcode> {
        let (mut i, mut j) = (0, 0);
        let mut opcodes = Vec::new();
        for (ai, bj, size) in self.matching_blocks() {
            let tag = match (i < ai, j < bj) {
                (true, true) => Some(Tag::Replace),
                (true, false) => Some(Tag::Delete),
                (false, true) => Some(Tag::Insert),
                (false, false) => None,
            };
            if let Some(tag) = tag {
                opcodes.push(Opcode {
                    tag,
                    i1: i,
                    i2: ai,
                    j1: j,
                    j2: bj,
                });
            }
            (i, j) = (ai + size, bj + size);
            if size > 0 {
                opcodes.push(Opcode {
                    tag: Tag::Equal,
                    i1: ai,
                    i2: i,
                    j1: bj,
                    j2: j,
                });
            }
        }
        opcodes
    }

    fn grouped_opcodes(&self) -> Vec<Vec<Opcode>> {
        let context = CONTEXT_LINES;
        let mut codes = self.opcodes();
        if codes.is_empty() {
            codes.push(Opcode {
                tag: Tag::Equal,
                i1: 0,
                i2: 1,
                j1: 0,
                j2: 1,
            });
        }
        if let Some(first) = codes.first_mut().filter(|code| code.tag == Tag::Equal) {
            first.i1 = first.i1.max(first.i2.saturating_sub(context));
            first.j1 = first.j1.max(first.j2.saturating_sub(context));
        }
        if let Some(last) = codes.last_mut().filter(|code| code.tag == Tag::Equal) {
            last.i2 = last.i2.min(last.i1 + context);
            last.j2 = last.j2.min(last.j1 + context);
        }
        let mut groups = Vec::new();
        let mut group = Vec::new();
        for mut code in codes {
            if code.tag == Tag::Equal && code.i2 - code.i1 > context * 2 {
                group.push(Opcode {
                    i2: code.i2.min(code.i1 + context),
                    j2: code.j2.min(code.j1 + context),
                    ..code
                });
                groups.push(std::mem::take(&mut group));
                code.i1 = code.i1.max(code.i2.saturating_sub(context));
                code.j1 = code.j1.max(code.j2.saturating_sub(context));
            }
            group.push(code);
        }
        if !group.is_empty() && !(group.len() == 1 && group[0].tag == Tag::Equal) {
            groups.push(group);
        }
        groups
    }
}

fn unified_range(start: usize, stop: usize) -> String {
    let length = stop - start;
    match length {
        0 => format!("{start},0"),
        1 => format!("{}", start + 1),
        _ => format!("{},{length}", start + 1),
    }
}

/// The lines of `difflib.unified_diff(a, b, fromfile, tofile, lineterm=lineterm)`.
#[must_use]
pub fn unified_diff(
    a: &[&str],
    b: &[&str],
    from_file: &str,
    to_file: &str,
    line_terminator: &str,
) -> Vec<String> {
    let matcher = Matcher::new(a, b);
    let groups = matcher.grouped_opcodes();
    let mut lines = Vec::new();
    if groups.is_empty() {
        return lines;
    }
    lines.push(format!("--- {from_file}{line_terminator}"));
    lines.push(format!("+++ {to_file}{line_terminator}"));
    for group in groups {
        let (first, last) = (group[0], group[group.len() - 1]);
        lines.push(format!(
            "@@ -{} +{} @@{line_terminator}",
            unified_range(first.i1, last.i2),
            unified_range(first.j1, last.j2)
        ));
        for code in group {
            if code.tag == Tag::Equal {
                lines.extend(a[code.i1..code.i2].iter().map(|line| format!(" {line}")));
                continue;
            }
            if matches!(code.tag, Tag::Replace | Tag::Delete) {
                lines.extend(a[code.i1..code.i2].iter().map(|line| format!("-{line}")));
            }
            if matches!(code.tag, Tag::Replace | Tag::Insert) {
                lines.extend(b[code.j1..code.j2].iter().map(|line| format!("+{line}")));
            }
        }
    }
    lines
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;

    use super::*;

    /// Lines that `unified_diff` would treat as junk, exposed for tests of the heuristic.
    fn popular_lines<'a>(b: &[&'a str]) -> HashSet<&'a str> {
        let matcher = Matcher::new(b, b);
        b.iter()
            .copied()
            .filter(|line| !matcher.b2j.contains_key(line))
            .collect()
    }

    #[test]
    fn matches_difflib_for_a_replaced_line_with_context() {
        let a = [
            "select",
            "a,",
            "b",
            "from t",
            "where x",
            "and y",
            "and z",
            "order by 1",
        ];
        let b = [
            "select",
            "a,",
            "c",
            "from t",
            "where x",
            "and y",
            "and z",
            "order by 1",
        ];
        assert_eq!(
            unified_diff(&a, &b, "current", "desired", ""),
            [
                "--- current",
                "+++ desired",
                "@@ -1,6 +1,6 @@",
                " select",
                " a,",
                "-b",
                "+c",
                " from t",
                " where x",
                " and y",
            ]
        );
    }

    #[test]
    fn renders_creation_and_empty_ranges_like_difflib() {
        assert_eq!(
            unified_diff(&[], &["x\n", "y\n"], "previous", "current", "\n"),
            [
                "--- previous\n",
                "+++ current\n",
                "@@ -0,0 +1,2 @@\n",
                "+x\n",
                "+y\n"
            ]
        );
        assert!(unified_diff(&["same"], &["same"], "a", "b", "").is_empty());
        assert!(unified_diff(&[], &[], "a", "b", "").is_empty());
    }

    #[test]
    fn splits_distant_changes_into_separate_hunks() {
        let a: Vec<String> = (0..20).map(|index| format!("line {index}")).collect();
        let mut b = a.clone();
        b[1] = "changed 1".into();
        b[18] = "changed 18".into();
        let a: Vec<&str> = a.iter().map(String::as_str).collect();
        let b: Vec<&str> = b.iter().map(String::as_str).collect();
        let headers: Vec<String> = unified_diff(&a, &b, "a", "b", "")
            .into_iter()
            .filter(|line| line.starts_with("@@"))
            .collect();
        assert_eq!(headers, ["@@ -1,5 +1,5 @@", "@@ -16,5 +16,5 @@"]);
    }

    #[test]
    fn treats_lines_repeated_more_than_one_percent_as_popular_in_long_inputs() {
        let mut b = vec!["x"; 3];
        b.extend(std::iter::repeat_n("y", 197));
        assert_eq!(popular_lines(&b), HashSet::from(["y"]));
        assert!(popular_lines(&b[..199]).is_empty());
    }
}
