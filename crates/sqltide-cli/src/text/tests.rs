//! `textwrap.wrap` outcomes recorded from CPython 3.12.

use crate::text::text_wrap::wrap;

#[test]
fn given_long_word_at_full_line_when_wrapping_then_trailing_space_stays_as_in_cpython() {
    let cases: [(&str, usize, &[&str]); 4] = [
        (
            "Replay from a specific time, e.g. '2026-04-01T00:00:00'",
            20,
            &[
                "Replay from a",
                "specific time, e.g. ",
                "'2026-04-",
                "01T00:00:00'",
            ],
        ),
        (
            "alpha beta-gamma-delta epsilon",
            9,
            &["alpha", "beta-", "gamma-", "delta", "epsilon"],
        ),
        ("one  two\tthree", 5, &["one", "two", "three"]),
        (
            "aaaaaaaaaaaaaaa bb",
            4,
            &["aaaa", "aaaa", "aaaa", "aaa", "bb"],
        ),
    ];
    for (text, width, expected) in cases {
        assert_eq!(
            wrap(text, width, ("", ""), true),
            expected,
            "{text:?} at {width}"
        );
    }
}
