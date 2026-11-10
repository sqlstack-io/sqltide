//! The packaged UI build: hashed assets under `/_app/immutable`, the rest of `/_app` with
//! revalidation, and every other path answered by the matching root file or the SPA shell.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use pyo3::prelude::*;

use sqltide_json::{JsonDialect, JsonValue, StdlibJsonOptions, dumps};

use super::http::{Failure, HttpReply, HttpRequest, http_date, md5_hex, text};

const INDEX_FILE_NAME: &str = "index.html";
const REVALIDATE_CACHE_CONTROL: &str = "no-cache, must-revalidate";
const IMMUTABLE_CACHE_CONTROL: &str = "public, max-age=31536000, immutable";
const NOT_MODIFIED_HEADERS: [&str; 6] = [
    "cache-control",
    "content-location",
    "date",
    "etag",
    "expires",
    "vary",
];
const MAX_RANGES: usize = 100;

/// The packaged UI build directory.
#[derive(Clone, Debug)]
pub(crate) struct StaticAssets {
    pub(crate) root: PathBuf,
}

impl StaticAssets {
    /// Whether a built UI exists at `root`.
    pub(crate) fn present(root: &Path) -> bool {
        root.join(INDEX_FILE_NAME).is_file()
    }

    /// The reply of the asset mounts or the SPA catch-all when one of them fully matches a
    /// path no API route took; `None` leaves the router's 405.
    pub(crate) fn serve(&self, py: Python<'_>, request: &HttpRequest) -> Option<HttpReply> {
        let immutable: PathBuf = self.root.join("_app").join("immutable");
        if immutable.is_dir()
            && let Some(rest) = mount_rest(&request.path, "/_app/immutable") {
                return Some(mounted(py, request, &immutable, rest, IMMUTABLE_CACHE_CONTROL));
            }
        if let Some(rest) = mount_rest(&request.path, "/_app") {
            return Some(mounted(py, request, &self.root.join("_app"), rest, REVALIDATE_CACHE_CONTROL));
        }
        if request.method != "GET" {
            return None;
        }
        let full_path: &str = request.path.strip_prefix('/').unwrap_or(&request.path);
        let candidate: PathBuf = self.root.join(full_path);
        let chosen: PathBuf = if !full_path.is_empty()
            && candidate.is_file()
            && fs::canonicalize(&candidate).is_ok_and(|resolved| resolved.starts_with(&self.root))
        {
            candidate
        } else {
            self.root.join(INDEX_FILE_NAME)
        };
        Some(match fs::metadata(&chosen) {
            Ok(metadata) => file_reply(py, request, &chosen, &metadata, Some(REVALIDATE_CACHE_CONTROL)),
            Err(_) => not_found(),
        })
    }
}

/// The path inside a `Mount(prefix)`, which matches only `prefix/...`.
fn mount_rest<'a>(path: &'a str, prefix: &str) -> Option<&'a str> {
    path.strip_prefix(prefix)
        .and_then(|rest| rest.strip_prefix('/'))
}

fn not_found() -> HttpReply {
    reply_or_empty(Failure::reply(404, &text("Not Found"), &[]), 404)
}

fn reply_or_empty(reply: pyo3::PyResult<HttpReply>, status: u16) -> HttpReply {
    reply.unwrap_or_else(|_| HttpReply::new(status, "application/json", Vec::new()))
}

/// `StaticFiles(directory)` behind a middleware that sets `Cache-Control` on what it sends.
fn mounted(py: Python<'_>, request: &HttpRequest, directory: &Path, rest: &str, cache_control: &str) -> HttpReply {
    if request.method != "GET" && request.method != "HEAD" {
        return reply_or_empty(
            Failure::reply(
                405,
                &text("Method Not Allowed"),
                &[("Allow".to_owned(), "GET, HEAD".to_owned())],
            ),
            405,
        );
    }
    let Some(relative) = normalized(rest) else {
        return not_found();
    };
    let Some((path, metadata)) = lookup(directory, &relative) else {
        return not_found();
    };
    if !metadata.is_file() {
        return not_found();
    }
    let mut reply: HttpReply = file_reply(py, request, &path, &metadata, None);
    if reply.status == 200 && not_modified(request, &reply) {
        let kept: Vec<(String, String)> = reply
            .headers
            .iter()
            .filter(|(name, _)| NOT_MODIFIED_HEADERS.contains(&name.as_str()))
            .cloned()
            .collect();
        reply = HttpReply {
            status: 304,
            headers: kept,
            body: Vec::new(),
        };
    }
    reply.set_header("cache-control", cache_control.to_owned());
    reply
}

/// `os.path.normpath(os.path.join(*path.split("/")))` as a relative path, or `None` for a
/// path that cannot name a file inside the directory.
fn normalized(rest: &str) -> Option<String> {
    if rest.contains('\0') {
        return None;
    }
    let mut parts: Vec<&str> = Vec::new();
    let mut absolute: bool = false;
    for (index, part) in rest.split('/').enumerate() {
        // `os.path.join` restarts at an absolute component, which only an empty first
        // segment of a split cannot produce; a later empty segment is dropped by normpath.
        if part.is_empty() {
            if index == 0 {
                continue;
            }
            continue;
        }
        if part.starts_with('\\') {
            absolute = true;
        }
        match part {
            "." => {}
            ".." => match parts.last() {
                Some(last) if *last != ".." => {
                    parts.pop();
                }
                _ => parts.push(".."),
            },
            part => parts.push(part),
        }
    }
    if absolute {
        return None;
    }
    Some(if parts.is_empty() {
        ".".to_owned()
    } else {
        parts.join("/")
    })
}

/// `StaticFiles.lookup_path`: the real path of `relative`, if it stays inside `directory`.
fn lookup(directory: &Path, relative: &str) -> Option<(PathBuf, fs::Metadata)> {
    let base: PathBuf = fs::canonicalize(directory).ok()?;
    let full: PathBuf = fs::canonicalize(directory.join(relative)).ok()?;
    if !full.starts_with(&base) {
        return None;
    }
    let metadata: fs::Metadata = fs::metadata(&full).ok()?;
    Some((full, metadata))
}

/// `StaticFiles.is_not_modified`.
fn not_modified(request: &HttpRequest, reply: &HttpReply) -> bool {
    if let Some(tags) = request.header("if-none-match").filter(|tags| !tags.is_empty()) {
        if tags.trim() == "*" {
            return true;
        }
        let etag: &str = reply.header("etag").unwrap_or("");
        return tags
            .split(',')
            .any(|tag| tag.trim().strip_prefix("W/").unwrap_or(tag.trim()) == etag);
    }
    let since: Option<i64> = request.header("if-modified-since").and_then(parse_http_date);
    let modified: Option<i64> = reply.header("last-modified").and_then(parse_http_date);
    matches!((since, modified), (Some(since), Some(modified)) if since >= modified)
}

/// `email.utils.parsedate` reduced to the RFC 1123 dates this server writes.
fn parse_http_date(value: &str) -> Option<i64> {
    const MONTHS: [&str; 12] = [
        "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    ];
    let words: Vec<&str> = value.split_whitespace().collect();
    let words: &[&str] = if words.first().is_some_and(|word| word.ends_with(',')) {
        &words[1..]
    } else {
        &words
    };
    let [day, month, year, time, ..] = words else {
        return None;
    };
    let day: i64 = day.parse().ok()?;
    let month: i64 = i64::try_from(MONTHS.iter().position(|name| month.eq_ignore_ascii_case(name))?).ok()? + 1;
    let mut year: i64 = year.parse().ok()?;
    if year < 100 {
        year += if year > 68 { 1900 } else { 2000 };
    }
    let mut clock = time.split(':').map(str::parse::<i64>);
    let hours: i64 = clock.next()?.ok()?;
    let minutes: i64 = clock.next()?.ok()?;
    let seconds: i64 = clock.next().map_or(Some(0), Result::ok)?;
    let shifted_year: i64 = if month <= 2 { year - 1 } else { year };
    let era: i64 = shifted_year.div_euclid(400);
    let year_of_era: i64 = shifted_year - era * 400;
    let month_index: i64 = if month > 2 { month - 3 } else { month + 9 };
    let day_of_year: i64 = (153 * month_index + 2) / 5 + day - 1;
    let day_of_era: i64 = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let days: i64 = era * 146_097 + day_of_era - 719_468;
    Some(days * 86_400 + hours * 3600 + minutes * 60 + seconds)
}

/// `FileResponse(path, stat_result=...)`, including byte ranges.
fn file_reply(
    py: Python<'_>,
    request: &HttpRequest,
    path: &Path,
    metadata: &fs::Metadata,
    cache_control: Option<&str>,
) -> HttpReply {
    let size: u64 = metadata.len();
    let modified = metadata
        .modified()
        .ok()
        .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
        .unwrap_or_default();
    // `str(st_mtime)`, the float repr of the nanosecond timestamp.
    let mtime: f64 = modified.as_nanos() as f64 / 1e9;
    let mtime_text: String = dumps(
        &JsonValue::Float(mtime),
        &JsonDialect::Stdlib(StdlibJsonOptions::new(None)),
    )
    .unwrap_or_default();
    let etag: String = format!("\"{}\"", md5_hex(format!("{mtime_text}-{size}").as_bytes()));
    let last_modified: String = http_date(i64::try_from(modified.as_secs()).unwrap_or(0));
    let media: String = content_type(py, path);
    let mut reply: HttpReply = HttpReply {
        status: 200,
        headers: Vec::new(),
        body: Vec::new(),
    };
    if let Some(cache_control) = cache_control {
        reply.headers.push(("cache-control".to_owned(), cache_control.to_owned()));
    }
    reply.headers.push(("content-type".to_owned(), media.clone()));
    reply.headers.push(("accept-ranges".to_owned(), "bytes".to_owned()));
    reply.headers.push(("content-length".to_owned(), size.to_string()));
    reply.headers.push(("last-modified".to_owned(), last_modified.clone()));
    reply.headers.push(("etag".to_owned(), etag.clone()));
    let head: bool = request.method == "HEAD";
    let use_range: bool = request.header("range").is_some()
        && request
            .header("if-range")
            .is_none_or(|if_range| !if_range.starts_with("W/") && (if_range == last_modified || if_range == etag));
    let Ok(body) = fs::read(path) else {
        return not_found();
    };
    if !use_range {
        reply.body = if head { Vec::new() } else { body };
        return reply;
    }
    let file_size: u64 = u64::try_from(body.len()).unwrap_or(size);
    match parse_range_header(request.header("range").unwrap_or(""), file_size) {
        Err(RangeError::Malformed(message)) => {
            HttpReply::new(400, "text/plain; charset=utf-8", message.into_bytes())
        }
        Err(RangeError::NotSatisfiable) => {
            let mut reply: HttpReply = HttpReply::new(416, "text/plain; charset=utf-8", Vec::new());
            reply.headers.insert(0, ("content-range".to_owned(), format!("bytes */{file_size}")));
            reply
        }
        Ok(ranges) if ranges.is_empty() => {
            reply.body = if head { Vec::new() } else { body };
            reply
        }
        Ok(ranges) if ranges.len() == 1 => {
            let (start, end) = ranges[0];
            reply.status = 206;
            reply.set_header("content-range", format!("bytes {start}-{}/{file_size}", end - 1));
            reply.set_header("content-length", (end - start).to_string());
            reply.body = if head {
                Vec::new()
            } else {
                body[usize::try_from(start).unwrap_or(0)..usize::try_from(end).unwrap_or(0)].to_vec()
            };
            reply
        }
        Ok(ranges) => {
            let boundary: String = random_boundary();
            let mut payload: Vec<u8> = Vec::new();
            for (start, end) in &ranges {
                payload.extend_from_slice(
                    format!(
                        "--{boundary}\r\nContent-Type: {media}\r\nContent-Range: bytes {start}-{}/{file_size}\r\n\r\n",
                        end - 1
                    )
                    .as_bytes(),
                );
                payload.extend_from_slice(
                    &body[usize::try_from(*start).unwrap_or(0)..usize::try_from(*end).unwrap_or(0)],
                );
                payload.extend_from_slice(b"\r\n");
            }
            payload.extend_from_slice(format!("--{boundary}--").as_bytes());
            reply.status = 206;
            reply.set_header("content-type", format!("multipart/byteranges; boundary={boundary}"));
            reply.set_header("content-length", payload.len().to_string());
            reply.body = if head { Vec::new() } else { payload };
            reply
        }
    }
}

fn random_boundary() -> String {
    use std::collections::hash_map::RandomState;
    use std::hash::{BuildHasher, Hasher};
    let mut text: String = String::new();
    while text.len() < 26 {
        let mut hasher = RandomState::new().build_hasher();
        hasher.write_u64(super::http::now_seconds().unsigned_abs());
        text.push_str(&format!("{:016x}", hasher.finish()));
    }
    text.truncate(26);
    text
}

enum RangeError {
    Malformed(String),
    NotSatisfiable,
}

/// `FileResponse._parse_range_header`.
fn parse_range_header(header: &str, file_size: u64) -> Result<Vec<(u64, u64)>, RangeError> {
    let Some((units, ranges_text)) = header.split_once('=') else {
        return Err(RangeError::Malformed("Malformed range header.".to_owned()));
    };
    if units.trim().to_lowercase() != "bytes" {
        return Err(RangeError::Malformed("Only support bytes range".to_owned()));
    }
    if ranges_text.matches(',').count() + 1 > MAX_RANGES {
        return Ok(Vec::new());
    }
    let mut ranges: Vec<(u64, u64)> = Vec::new();
    for part in ranges_text.split(',') {
        let part: &str = part.trim();
        if part.is_empty() || part == "-" || !part.contains('-') {
            continue;
        }
        let (start_text, end_text) = part.split_once('-').unwrap_or((part, ""));
        let (start_text, end_text) = (start_text.trim(), end_text.trim());
        let parsed = (|| -> Option<(u64, u64)> {
            let start: u64 = if start_text.is_empty() {
                file_size.saturating_sub(python_int(end_text)?)
            } else {
                python_int(start_text)?
            };
            let end: u64 = if !start_text.is_empty() && !end_text.is_empty() {
                let end: u64 = python_int(end_text)?;
                if end < file_size { end + 1 } else { file_size }
            } else {
                file_size
            };
            Some((start, end))
        })();
        if let Some(range) = parsed {
            ranges.push(range);
        }
    }
    if ranges.is_empty() {
        return Err(RangeError::Malformed("Range header: range must be requested".to_owned()));
    }
    if ranges.iter().any(|(start, _)| *start >= file_size) {
        return Err(RangeError::NotSatisfiable);
    }
    if ranges.iter().any(|(start, end)| start >= end) {
        return Err(RangeError::Malformed("Range header: start must be less than end".to_owned()));
    }
    if ranges.len() == 1 {
        return Ok(ranges);
    }
    ranges.sort_unstable();
    let mut merged: Vec<(u64, u64)> = vec![ranges[0]];
    for (start, end) in ranges.into_iter().skip(1) {
        let last: &mut (u64, u64) = merged.last_mut().unwrap_or_else(|| unreachable!("seeded"));
        if start <= last.1 {
            last.1 = last.1.max(end);
        } else {
            merged.push((start, end));
        }
    }
    Ok(merged)
}

/// `int(text)` for the unsigned decimal byte positions a range can hold.
fn python_int(text: &str) -> Option<u64> {
    let digits: String = text.replace('_', "");
    if digits.is_empty() || text.starts_with('_') || text.ends_with('_') || text.contains("__") {
        return None;
    }
    let digits: &str = digits.strip_prefix('+').unwrap_or(&digits);
    if !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    digits.parse().ok()
}

/// `mimetypes.guess_type(path)[0] or "application/octet-stream"`, from Python's table so the
/// platform's MIME registry applies as before; text types gain a UTF-8 charset.
fn content_type(py: Python<'_>, path: &Path) -> String {
    let guessed: Option<String> = py
        .import("mimetypes")
        .and_then(|module| module.call_method1("guess_type", (path,)))
        .and_then(|guess| guess.get_item(0)?.extract())
        .ok()
        .flatten();
    let media: String = guessed.unwrap_or_else(|| "application/octet-stream".to_owned());
    if media.starts_with("text/") {
        format!("{media}; charset=utf-8")
    } else {
        media
    }
}
