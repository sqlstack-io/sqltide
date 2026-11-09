//! The Python built-ins PyYAML's safe constructor relies on: `int()`, `float()`, the UTF-8
//! and ASCII codecs and `base64.decodebytes`, with CPython 3.12's acceptance and messages.

use sqltide_sqltext::python_text::{decimal_digit, python_isspace, python_repr};

/// CPython's default limit on the digits `int()` converts in a base that is not a power of two.
pub(crate) const MAX_INT_DIGITS: usize = 4_300;

/// Whitespace as C's `isspace` in the C locale, which CPython's number parsers skip.
fn is_c_space(character: char) -> bool {
    matches!(character, ' ' | '\t' | '\n' | '\u{0b}' | '\u{0c}' | '\r')
}

/// `_PyUnicode_TransformDecimalAndSpaceToASCII`: decimal digits to ASCII and Unicode spaces to
/// `' '`; any other non-ASCII character ends the text with `?`, which no parser accepts.
fn ascii_number_text(text: &str) -> Vec<char> {
    let mut transformed: Vec<char> = Vec::with_capacity(text.len());
    for character in text.chars() {
        if u32::from(character) < 127 {
            transformed.push(character);
        } else if python_isspace(character) {
            transformed.push(' ');
        } else if let Some(digit) = decimal_digit(character) {
            transformed.push(char::from_digit(digit, 10).expect("a decimal digit"));
        } else {
            transformed.push('?');
            break;
        }
    }
    transformed
}

fn trim_c_space(characters: &[char]) -> &[char] {
    let start: usize = characters
        .iter()
        .position(|character| !is_c_space(*character))
        .unwrap_or(characters.len());
    let end: usize = characters
        .iter()
        .rposition(|character| !is_c_space(*character))
        .map_or(start, |index| index + 1);
    &characters[start..end]
}

/// Whether `int(text, base)` succeeds; the error is Python's message when it raises.
pub(crate) fn python_int(text: &str, base: u32) -> Result<(), String> {
    let invalid = || {
        // CPython formats the literal with `%.200R`: its repr cut to 200 characters.
        let literal: String = python_repr(text).chars().take(200).collect();
        format!("invalid literal for int() with base {base}: {literal}")
    };
    let transformed: Vec<char> = ascii_number_text(text);
    let mut body: &[char] = trim_c_space(&transformed);
    if let Some(rest) = body
        .strip_prefix(&['+'])
        .or_else(|| body.strip_prefix(&['-']))
    {
        body = rest;
    }
    let prefix: Option<char> = match base {
        16 => Some('x'),
        8 => Some('o'),
        2 => Some('b'),
        _ => None,
    };
    if let (Some(letter), ['0', marker, rest @ ..]) = (prefix, body)
        && marker.to_ascii_lowercase() == letter
    {
        body = rest;
    }
    if body.is_empty() || !body.iter().all(|character| character.is_digit(base)) {
        return Err(invalid());
    }
    if !base.is_power_of_two() && body.len() > MAX_INT_DIGITS {
        return Err(format!(
            "Exceeds the limit ({MAX_INT_DIGITS} digits) for integer string conversion: value \
             has {} digits; use sys.set_int_max_str_digits() to increase the limit",
            body.len()
        ));
    }
    Ok(())
}

/// Whether `float(text)` succeeds; the error is Python's message when it raises.
pub(crate) fn python_float(text: &str) -> Result<(), String> {
    let transformed: Vec<char> = ascii_number_text(text);
    let mut body: &[char] = trim_c_space(&transformed);
    if let Some(rest) = body
        .strip_prefix(&['+'])
        .or_else(|| body.strip_prefix(&['-']))
    {
        body = rest;
    }
    let word: String = body.iter().collect::<String>().to_ascii_lowercase();
    let valid: bool = matches!(word.as_str(), "inf" | "infinity" | "nan") || is_decimal(body);
    if valid {
        Ok(())
    } else {
        Err(format!(
            "could not convert string to float: {}",
            python_repr(text)
        ))
    }
}

/// `digits [. digits] [e [sign] digits]` with at least one mantissa digit.
fn is_decimal(characters: &[char]) -> bool {
    let mantissa_end: usize = characters
        .iter()
        .position(|character| matches!(character, 'e' | 'E'))
        .unwrap_or(characters.len());
    let (mantissa, exponent) = characters.split_at(mantissa_end);
    let mut parts = mantissa.splitn(2, |character| *character == '.');
    let whole: &[char] = parts.next().unwrap_or_default();
    let fraction: &[char] = parts.next().unwrap_or_default();
    let digits = |part: &[char]| part.iter().all(char::is_ascii_digit);
    if !digits(whole) || !digits(fraction) || whole.len() + fraction.len() == 0 {
        return false;
    }
    match exponent {
        [] => true,
        [_, rest @ ..] => {
            let rest: &[char] = rest
                .strip_prefix(&['+'])
                .or_else(|| rest.strip_prefix(&['-']))
                .unwrap_or(rest);
            !rest.is_empty() && digits(rest)
        }
    }
}

/// `bytes(codes).decode('utf-8')`, with the `UnicodeDecodeError` message when it fails.
///
/// Both CPython and Rust report the maximal invalid subpart, so the failing span is Rust's.
pub(crate) fn decode_utf8(bytes: &[u8]) -> Result<String, String> {
    let error = match std::str::from_utf8(bytes) {
        Ok(text) => return Ok(text.to_owned()),
        Err(error) => error,
    };
    let start: usize = error.valid_up_to();
    let (end, reason): (usize, &str) = match error.error_len() {
        None => (bytes.len(), "unexpected end of data"),
        Some(length) if (0xc2..=0xf4).contains(&bytes[start]) => {
            (start + length, "invalid continuation byte")
        }
        Some(length) => (start + length, "invalid start byte"),
    };
    Err(if end - start == 1 {
        format!(
            "'utf-8' codec can't decode byte 0x{:02x} in position {start}: {reason}",
            bytes[start]
        )
    } else {
        format!(
            "'utf-8' codec can't decode bytes in position {start}-{}: {reason}",
            end - 1
        )
    })
}

/// The `UnicodeEncodeError` message of `text.encode('ascii')`, when it fails.
pub(crate) fn ascii_encode_error(text: &str) -> Option<String> {
    let characters: Vec<char> = text.chars().collect();
    let start: usize = characters
        .iter()
        .position(|character| !character.is_ascii())?;
    let end: usize = characters[start..]
        .iter()
        .position(char::is_ascii)
        .map_or(characters.len(), |offset| start + offset);
    Some(if end - start == 1 {
        let code: u32 = u32::from(characters[start]);
        let escaped: String = if code <= 0xff {
            format!("\\x{code:02x}")
        } else if code <= 0xffff {
            format!("\\u{code:04x}")
        } else {
            format!("\\U{code:08x}")
        };
        format!(
            "'ascii' codec can't encode character '{escaped}' in position {start}: ordinal not \
             in range(128)"
        )
    } else {
        format!(
            "'ascii' codec can't encode characters in position {start}-{}: ordinal not in \
             range(128)",
            end - 1
        )
    })
}

fn base64_value(byte: u8) -> Option<u8> {
    match byte {
        b'A'..=b'Z' => Some(byte - b'A'),
        b'a'..=b'z' => Some(byte - b'a' + 26),
        b'0'..=b'9' => Some(byte - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}

/// Whether `binascii.a2b_base64` in non-strict mode accepts the ASCII text; the error is its
/// `binascii.Error` message.
pub(crate) fn base64_decode_error(text: &str) -> Option<String> {
    let mut quad_position: usize = 0;
    let mut pads: usize = 0;
    let mut decoded: usize = 0;
    for byte in text.bytes() {
        if byte == b'=' {
            if quad_position >= 2 {
                pads += 1;
                if quad_position + pads >= 4 {
                    return None;
                }
            }
            continue;
        }
        if base64_value(byte).is_none() {
            continue;
        }
        pads = 0;
        quad_position = (quad_position + 1) % 4;
        if quad_position != 1 {
            decoded += 1;
        }
    }
    match quad_position {
        0 => None,
        1 => Some(format!(
            "Invalid base64-encoded string: number of data characters ({}) cannot be 1 more \
             than a multiple of 4",
            decoded / 3 * 4 + 1
        )),
        _ => Some("Incorrect padding".to_owned()),
    }
}
