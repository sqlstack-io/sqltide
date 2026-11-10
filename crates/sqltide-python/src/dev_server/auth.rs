//! Request authentication, the CSRF proof on mutations, login throttling and the session and
//! account administration routes.

use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use pyo3::prelude::*;
use pyo3::types::PyTuple;
use sha2::{Digest, Sha256};
use sqltide_json::JsonValue;

use super::app::Call;
use super::http::{
    Failure, HttpReply, HttpRequest, Outcome, http_date, now_seconds, object, py_json, set_cookie,
    text,
};
use super::support::{attr, is_instance, message};
use crate::py_kwargs;

const AUTH_MODULE: &str = "sqltide.auth.models";
const EXCEPTIONS_MODULE: &str = "sqltide.auth.exceptions";
const CONSTANTS_MODULE: &str = "sqltide.auth.constants";
const ADMIN_ROLE: &str = "admin";
const CSRF_HEADER: &str = "x-sqltide-csrf";
const TRUSTED_PROXY_CSRF_PROOF: &str = "trusted-proxy";
const PUBLIC_API_PATHS: [&str; 2] = ["/api/auth/config", "/api/auth/login"];
const UNSAFE_HTTP_METHODS: [&str; 4] = ["POST", "PUT", "PATCH", "DELETE"];
const CACHE_TTL: Duration = Duration::from_secs(5);
const CACHE_MAX_ENTRIES: usize = 1024;
const LOGIN_ATTEMPT_WINDOW: Duration = Duration::from_secs(60);
const LOGIN_USER_ATTEMPT_LIMIT: usize = 5;
const LOGIN_IP_ATTEMPT_LIMIT: usize = 20;
const LOGIN_LIMITER_MAX_KEYS: usize = 2048;

/// Why a request was refused before it reached a route.
pub(crate) enum Refusal {
    /// 401 with `WWW-Authenticate: Session`.
    Unauthenticated(String),
    /// 403.
    Forbidden(String),
}

impl Refusal {
    pub(crate) fn reply(&self) -> PyResult<HttpReply> {
        match self {
            Self::Unauthenticated(detail) => Failure::reply(
                401,
                &text(detail.clone()),
                &[("www-authenticate".to_owned(), "Session".to_owned())],
            ),
            Self::Forbidden(detail) => Failure::reply(403, &text(detail.clone()), &[]),
        }
    }

    /// The refusal an authentication or account exception maps to, as the middleware that
    /// wraps every private route catches them.
    pub(crate) fn classify(py: Python<'_>, error: &PyErr) -> PyResult<Option<Self>> {
        if is_instance(py, error, EXCEPTIONS_MODULE, "AuthenticationError")? {
            return Ok(Some(Self::Unauthenticated(message(py, error)?)));
        }
        if is_instance(py, error, EXCEPTIONS_MODULE, "AuthorizationError")?
            || is_instance(py, error, EXCEPTIONS_MODULE, "AccountConflictError")?
        {
            return Ok(Some(Self::Forbidden(message(py, error)?)));
        }
        Ok(None)
    }
}

/// Whether the middleware authenticates `path`.
pub(crate) fn protects(path: &str) -> bool {
    path.starts_with("/api/") && !PUBLIC_API_PATHS.contains(&path)
}

/// A resolved principal, its expiry and the account revision it was resolved at.
type CachedPrincipal = (Instant, i64, Py<PyAny>);

/// The configured authentication mechanism over the account control store.
pub(crate) struct AuthService {
    pub(crate) settings: Py<PyAny>,
    pub(crate) store: Py<PyAny>,
    mode: String,
    cache: Mutex<HashMap<String, CachedPrincipal>>,
}

impl AuthService {
    pub(crate) fn new(py: Python<'_>, settings: Py<PyAny>, store: Py<PyAny>) -> PyResult<Self> {
        let mode: String = settings.bind(py).getattr("mode")?.str()?.to_string();
        Ok(Self {
            settings,
            store,
            mode,
            cache: Mutex::new(HashMap::new()),
        })
    }

    fn setting<'py>(&self, py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
        self.settings.bind(py).getattr(name)
    }

    /// The authenticated principal of one request, with the CSRF proof checked on mutations.
    pub(crate) fn authenticate<'py>(
        &self,
        py: Python<'py>,
        request: &HttpRequest,
    ) -> PyResult<Result<Bound<'py, PyAny>, Refusal>> {
        let outcome: PyResult<Bound<'py, PyAny>> =
            self.resolve(py, request).and_then(|authenticated| {
                if UNSAFE_HTTP_METHODS.contains(&request.method.as_str()) {
                    self.require_csrf(py, &authenticated, request.header(CSRF_HEADER))?;
                }
                Ok(authenticated)
            });
        match outcome {
            Ok(authenticated) => Ok(Ok(authenticated)),
            Err(error) => match Refusal::classify(py, &error)? {
                Some(refusal) => Ok(Err(refusal)),
                None => Err(error),
            },
        }
    }

    fn resolve<'py>(&self, py: Python<'py>, request: &HttpRequest) -> PyResult<Bound<'py, PyAny>> {
        match self.mode.as_str() {
            "disabled" => local_request(py),
            "trusted_proxy" => self.authenticate_proxy(py, request),
            _ => self.authenticate_session(py, request),
        }
    }

    fn require_csrf(
        &self,
        py: Python<'_>,
        authenticated: &Bound<'_, PyAny>,
        supplied: Option<&str>,
    ) -> PyResult<()> {
        let source: String = authenticated
            .getattr("principal")?
            .getattr("authentication_source")?
            .str()?
            .to_string();
        if source == "local" {
            return Ok(());
        }
        if source == "trusted_proxy" {
            if supplied != Some(TRUSTED_PROXY_CSRF_PROOF) {
                return Err(raise(
                    py,
                    "AuthorizationError",
                    "Missing trusted-proxy request proof",
                ));
            }
            return Ok(());
        }
        let expected: Option<String> = authenticated.getattr("csrf_token")?.extract()?;
        let matches: bool = match (expected, supplied) {
            (Some(expected), Some(supplied)) => {
                constant_time_equal(expected.as_bytes(), supplied.as_bytes())
            }
            _ => false,
        };
        if !matches {
            return Err(raise(
                py,
                "AuthorizationError",
                "Missing or invalid CSRF token",
            ));
        }
        Ok(())
    }

    fn authenticate_proxy<'py>(
        &self,
        py: Python<'py>,
        request: &HttpRequest,
    ) -> PyResult<Bound<'py, PyAny>> {
        let header_name: String = self.setting(py, "username_header")?.extract()?;
        let raw: Option<&str> = request.header(&header_name);
        let Some(raw) = raw.filter(|raw| !python_strip(raw).is_empty()) else {
            return Err(raise(
                py,
                "AuthenticationError",
                &format!("Trusted proxy did not provide required header '{header_name}'"),
            ));
        };
        let username: String =
            match attr(py, "sqltide._native", "canonical_username")?.call1((raw,)) {
                Ok(username) => username.extract()?,
                Err(error) => {
                    if error
                        .value(py)
                        .is_instance_of::<pyo3::exceptions::PyValueError>()
                    {
                        return Err(raise(py, "AuthenticationError", &message(py, &error)?));
                    }
                    return Err(error);
                }
            };
        let cache_key: String = format!("proxy:{username}");
        if let Some(cached) = self.cached(py, &cache_key)? {
            return Ok(cached);
        }
        let store: &Bound<'py, PyAny> = self.store.bind(py);
        let source =
            attr(py, "sqltide.auth.types", "AuthenticationSource")?.getattr("TRUSTED_PROXY")?;
        let mut account: Bound<'py, PyAny> = store.call_method(
            "resolve_external_identity",
            (),
            Some(&py_kwargs!(py; source = &source, subject = username.as_str())?),
        )?;
        if account.is_none() {
            let policy: String = self.setting(py, "unknown_user_policy")?.str()?.to_string();
            if policy == "deny" {
                return Err(raise(
                    py,
                    "AuthorizationError",
                    &format!("Authenticated proxy user '{username}' is not provisioned in SQLTide"),
                ));
            }
            let display_name: Option<String> =
                self.optional_header(py, request, "display_name_header")?;
            let email: Option<String> = self.optional_header(py, request, "email_header")?;
            account = store.call_method(
                "provision_proxy_user",
                (),
                Some(&py_kwargs!(py;
                    subject = username.as_str(),
                    username = username.as_str(),
                    display_name = display_name,
                    email = email,
                    default_role = self.setting(py, "default_role")?,
                )?),
            )?;
        }
        if !account.getattr("is_active")?.is_truthy()? {
            return Err(raise(py, "AuthorizationError", "Account is disabled"));
        }
        let authenticated: Bound<'py, PyAny> = authenticated_request(
            py,
            principal(py, &account, &source)?,
            account.getattr("roles")?,
            None,
        )?;
        self.remember(py, &cache_key, &authenticated)?;
        Ok(authenticated)
    }

    fn optional_header(
        &self,
        py: Python<'_>,
        request: &HttpRequest,
        setting: &str,
    ) -> PyResult<Option<String>> {
        let name: Option<String> = self.setting(py, setting)?.extract()?;
        Ok(name
            .and_then(|name| {
                request
                    .header(&name)
                    .map(|value| python_strip(value).to_owned())
            })
            .filter(|value| !value.is_empty()))
    }

    fn authenticate_session<'py>(
        &self,
        py: Python<'py>,
        request: &HttpRequest,
    ) -> PyResult<Bound<'py, PyAny>> {
        let cookie_name: String = self.setting(py, "session_cookie_name")?.extract()?;
        let Some(token) = request.cookie(&cookie_name) else {
            return Err(raise(py, "AuthenticationError", "Authentication required"));
        };
        let digest: String = Sha256::digest(token.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        let cache_key: String = format!("session:{digest}");
        if let Some(cached) = self.cached(py, &cache_key)? {
            return Ok(cached);
        }
        let resolved: Bound<'py, PyAny> = self.store.bind(py).call_method(
            "resolve_session",
            (),
            Some(&py_kwargs!(py; token = token.as_str())?),
        )?;
        if resolved.is_none() {
            return Err(raise(
                py,
                "AuthenticationError",
                "Session is invalid or expired",
            ));
        }
        let authenticated: Bound<'py, PyAny> = authenticated_request(
            py,
            resolved.getattr("principal")?,
            resolved.getattr("roles")?,
            Some(resolved.getattr("csrf_token")?),
        )?;
        self.remember(py, &cache_key, &authenticated)?;
        Ok(authenticated)
    }

    fn revision(&self, py: Python<'_>) -> PyResult<i64> {
        self.store
            .bind(py)
            .getattr("authentication_revision")?
            .extract()
    }

    fn cached<'py>(&self, py: Python<'py>, cache_key: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
        let now: Instant = Instant::now();
        let revision: i64 = self.revision(py)?;
        let mut cache = self
            .cache
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let Some((expires_at, cached_revision, authenticated)) = cache.get(cache_key) else {
            return Ok(None);
        };
        if *expires_at <= now || *cached_revision != revision {
            cache.remove(cache_key);
            return Ok(None);
        }
        Ok(Some(authenticated.bind(py).clone()))
    }

    fn remember(
        &self,
        py: Python<'_>,
        cache_key: &str,
        authenticated: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let now: Instant = Instant::now();
        let revision: i64 = self.revision(py)?;
        let mut cache = self
            .cache
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if cache.len() >= CACHE_MAX_ENTRIES {
            cache.retain(|_, (expires_at, _, _)| *expires_at > now);
        }
        cache.insert(
            cache_key.to_owned(),
            (now + CACHE_TTL, revision, authenticated.clone().unbind()),
        );
        Ok(())
    }

    /// The public authentication configuration.
    pub(crate) fn config_payload(&self, py: Python<'_>) -> PyResult<JsonValue> {
        let proxy_logout_url: Option<String> = self.setting(py, "proxy_logout_url")?.extract()?;
        Ok(object(vec![
            ("mode", text(self.mode.clone())),
            ("loginRequired", JsonValue::Bool(self.mode == "password")),
            (
                "proxyLogoutUrl",
                proxy_logout_url.map_or(JsonValue::Null, JsonValue::String),
            ),
        ]))
    }

    /// The configuration and current session the browser bootstraps from.
    pub(crate) fn browser_payload(
        &self,
        py: Python<'_>,
        authenticated: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<(&'static str, JsonValue)>> {
        Ok(vec![
            ("config", self.config_payload(py)?),
            ("session", self.authenticated_payload(authenticated)?),
        ])
    }

    pub(crate) fn authenticated_payload(
        &self,
        authenticated: &Bound<'_, PyAny>,
    ) -> PyResult<JsonValue> {
        let principal: Bound<'_, PyAny> = authenticated.getattr("principal")?;
        Ok(object(vec![
            ("mode", text(self.mode.clone())),
            (
                "user",
                object(vec![
                    ("id", text(principal.getattr("user_id")?.str()?.to_string())),
                    ("username", py_json(&principal.getattr("username")?)?),
                    ("displayName", py_json(&principal.getattr("display_name")?)?),
                    ("email", py_json(&principal.getattr("email")?)?),
                    (
                        "authenticationSource",
                        py_json(&principal.getattr("authentication_source")?)?,
                    ),
                ]),
            ),
            ("roles", py_json(&authenticated.getattr("roles")?)?),
            ("csrfToken", py_json(&authenticated.getattr("csrf_token")?)?),
        ]))
    }
}

fn python_strip(text: &str) -> &str {
    text.trim_matches(char::is_whitespace)
}

fn constant_time_equal(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter()
        .zip(right)
        .fold(0_u8, |acc, (a, b)| acc | (a ^ b))
        == 0
}

/// A new `sqltide.auth.exceptions.<name>(detail)`.
fn raise(py: Python<'_>, name: &str, detail: &str) -> PyErr {
    match attr(py, EXCEPTIONS_MODULE, name).and_then(|class| class.call1((detail,))) {
        Ok(value) => PyErr::from_value(value),
        Err(error) => error,
    }
}

fn authenticated_request<'py>(
    py: Python<'py>,
    principal: Bound<'py, PyAny>,
    roles: Bound<'py, PyAny>,
    csrf_token: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    attr(py, AUTH_MODULE, "AuthenticatedRequest")?.call(
        (),
        Some(&py_kwargs!(py; principal = principal, roles = roles, csrf_token = csrf_token)?),
    )
}

fn principal<'py>(
    py: Python<'py>,
    account: &Bound<'py, PyAny>,
    source: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    attr(py, AUTH_MODULE, "Principal")?.call(
        (),
        Some(&py_kwargs!(py;
            user_id = account.getattr("user_id")?,
            username = account.getattr("username")?,
            display_name = account.getattr("display_name")?,
            email = account.getattr("email")?,
            authentication_source = source,
        )?),
    )
}

/// The local developer with administrator access when authentication is disabled.
fn local_request(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    let source = attr(py, "sqltide.auth.types", "AuthenticationSource")?.getattr("LOCAL")?;
    let principal = attr(py, AUTH_MODULE, "Principal")?.call(
        (),
        Some(&py_kwargs!(py;
            user_id = attr(py, CONSTANTS_MODULE, "LOCAL_USER_ID")?,
            username = "local",
            display_name = "Local developer",
            email = py.None(),
            authentication_source = source,
        )?),
    )?;
    authenticated_request(
        py,
        principal,
        PyTuple::new(py, [ADMIN_ROLE])?.into_any(),
        None,
    )
}

/// Throttles password verification per user and per client inside one server process.
#[derive(Default)]
pub(crate) struct LoginLimiter {
    attempts: Mutex<HashMap<String, VecDeque<Instant>>>,
}

impl LoginLimiter {
    fn check(&self, ip_key: &str, user_key: &str) -> Outcome<()> {
        let mut attempts = self
            .attempts
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let keys: Vec<String> = attempts.keys().cloned().collect();
        for key in keys {
            if active(&mut attempts, &key) == 0 {
                attempts.remove(&key);
            }
        }
        if attempts.len() >= LOGIN_LIMITER_MAX_KEYS && !attempts.contains_key(user_key) {
            return Err(limited());
        }
        if active(&mut attempts, user_key) >= LOGIN_USER_ATTEMPT_LIMIT
            || active(&mut attempts, ip_key) >= LOGIN_IP_ATTEMPT_LIMIT
        {
            return Err(limited());
        }
        Ok(())
    }

    fn failed(&self, ip_key: &str, user_key: &str) {
        let mut attempts = self
            .attempts
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let now: Instant = Instant::now();
        active(&mut attempts, ip_key);
        attempts
            .entry(ip_key.to_owned())
            .or_default()
            .push_back(now);
        active(&mut attempts, user_key);
        attempts
            .entry(user_key.to_owned())
            .or_default()
            .push_back(now);
    }

    fn succeeded(&self, user_key: &str) {
        let mut attempts = self
            .attempts
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        attempts.remove(user_key);
    }
}

/// Drop expired attempts of `key`, creating it as the earlier default dict did, and count the
/// rest.
fn active(attempts: &mut HashMap<String, VecDeque<Instant>>, key: &str) -> usize {
    let entry: &mut VecDeque<Instant> = attempts.entry(key.to_owned()).or_default();
    let now: Instant = Instant::now();
    while entry
        .front()
        .is_some_and(|first| now.duration_since(*first) > LOGIN_ATTEMPT_WINDOW)
    {
        entry.pop_front();
    }
    entry.len()
}

fn limited() -> Failure {
    Failure::status(429, "Too many login attempts; try again later")
}

/// The middleware-authenticated principal of a private route.
pub(crate) fn authenticated<'py>(call: &Call<'py, '_>) -> Outcome<Bound<'py, PyAny>> {
    call.authenticated
        .clone()
        .ok_or_else(|| Failure::status(401, "Authentication required"))
}

/// The authenticated caller, who must hold the system administrator role.
pub(crate) fn require_admin<'py>(call: &Call<'py, '_>) -> Outcome<Bound<'py, PyAny>> {
    let authenticated: Bound<'py, PyAny> = authenticated(call)?;
    if !has_admin_role(&authenticated)? {
        return Err(Failure::status(403, "System administrator role required"));
    }
    Ok(authenticated)
}

pub(crate) fn has_admin_role(authenticated: &Bound<'_, PyAny>) -> PyResult<bool> {
    authenticated.getattr("roles")?.contains(ADMIN_ROLE)
}

pub(crate) fn user_id<'py>(authenticated: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    authenticated.getattr("principal")?.getattr("user_id")
}

// Session routes.

pub(crate) fn read_auth_config(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    call.payload_json(&call.app.auth.config_payload(call.py)?)
}

pub(crate) fn read_current_user(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let authenticated = authenticated(call)?;
    call.payload_json(&call.app.auth.authenticated_payload(&authenticated)?)
}

pub(crate) fn login(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let service: &AuthService = &call.app.auth;
    if service.mode != "password" {
        return Err(Failure::status(404, "Password login is not enabled"));
    }
    let body = call.body()?;
    let username: String = body.getattr("username")?.extract()?;
    let password: String = body.getattr("password")?.extract()?;
    let client_host: String = call
        .request
        .client_host
        .clone()
        .unwrap_or_else(|| "unknown".to_owned());
    let limiter_username: String =
        match attr(py, "sqltide._native", "canonical_username")?.call1((username.as_str(),)) {
            Ok(canonical) => canonical.extract()?,
            Err(error) if is_instance(py, &error, EXCEPTIONS_MODULE, "AccountValidationError")? => {
                "<invalid>".to_owned()
            }
            Err(error) => return Err(error.into()),
        };
    let ip_key: String = format!("ip:{client_host}");
    let user_key: String = format!("user:{limiter_username}");
    call.app.limiter.check(&ip_key, &user_key)?;
    let ttl: i64 = service.setting(py, "session_ttl_seconds")?.extract()?;
    let result: Bound<'_, PyAny> = service.store.bind(py).call_method(
        "authenticate_password",
        (),
        Some(&py_kwargs!(py; username = username.as_str(), password = password.as_str(), session_ttl_seconds = ttl)?),
    )?;
    if result.is_none() {
        call.app.limiter.failed(&ip_key, &user_key);
        return Err(Failure::status(401, "Invalid username or password"));
    }
    call.app.limiter.succeeded(&user_key);
    let account = result.get_item(0)?;
    let credentials = result.get_item(1)?;
    let source = attr(py, "sqltide.auth.types", "AuthenticationSource")?.getattr("PASSWORD")?;
    let authenticated = authenticated_request(
        py,
        principal(py, &account, &source)?,
        account.getattr("roles")?,
        Some(credentials.getattr("csrf_token")?),
    )?;
    let mut reply: HttpReply =
        HttpReply::json(200, &service.authenticated_payload(&authenticated)?)?;
    let cookie_name: String = service.setting(py, "session_cookie_name")?.extract()?;
    let token: String = credentials.getattr("token")?.extract()?;
    let secure: bool = service.setting(py, "session_cookie_secure")?.is_truthy()?;
    reply.headers.push((
        "set-cookie".to_owned(),
        set_cookie(&cookie_name, &token, ttl, None, true, secure),
    ));
    Ok(reply)
}

pub(crate) fn logout(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let service: &AuthService = &call.app.auth;
    let authenticated = authenticated(call)?;
    let cookie_name: String = service.setting(py, "session_cookie_name")?.extract()?;
    if let Some(token) = call.request.cookie(&cookie_name) {
        service.store.bind(py).call_method(
            "revoke_session",
            (),
            Some(
                &py_kwargs!(py; token = token.as_str(), actor_user_id = user_id(&authenticated)?)?,
            ),
        )?;
    }
    let mut reply: HttpReply = HttpReply::json(200, &object(vec![("status", text("ok"))]))?;
    reply.headers.push((
        "set-cookie".to_owned(),
        set_cookie(
            &cookie_name,
            "",
            0,
            Some(http_date(now_seconds())),
            false,
            false,
        ),
    ));
    Ok(reply)
}

// Account administration routes.

fn store<'py>(call: &Call<'py, '_>) -> Bound<'py, PyAny> {
    call.app.auth.store.bind(call.py).clone()
}

fn user_payload(account: &Bound<'_, PyAny>) -> PyResult<JsonValue> {
    Ok(object(vec![
        ("id", text(account.getattr("user_id")?.str()?.to_string())),
        ("username", py_json(&account.getattr("username")?)?),
        ("displayName", py_json(&account.getattr("display_name")?)?),
        ("email", py_json(&account.getattr("email")?)?),
        ("active", py_json(&account.getattr("is_active")?)?),
        ("roles", py_json(&account.getattr("roles")?)?),
        (
            "authenticationSources",
            py_json(&account.getattr("authentication_sources")?)?,
        ),
        (
            "createdAt",
            py_json(&account.getattr("created_at")?.call_method0("isoformat")?)?,
        ),
        (
            "updatedAt",
            py_json(&account.getattr("updated_at")?.call_method0("isoformat")?)?,
        ),
    ]))
}

fn optional_uuid_text(value: &Bound<'_, PyAny>) -> PyResult<JsonValue> {
    if value.is_none() {
        return Ok(JsonValue::Null);
    }
    Ok(text(value.str()?.to_string()))
}

fn optional_iso(value: &Bound<'_, PyAny>) -> PyResult<JsonValue> {
    if value.is_none() {
        return Ok(JsonValue::Null);
    }
    py_json(&value.call_method0("isoformat")?)
}

fn audit_payload(record: &Bound<'_, PyAny>) -> PyResult<JsonValue> {
    Ok(object(vec![
        ("operation", py_json(&record.getattr("operation")?)?),
        (
            "actorUserId",
            optional_uuid_text(&record.getattr("actor_user_id")?)?,
        ),
        (
            "affectedUserId",
            optional_uuid_text(&record.getattr("affected_user_id")?)?,
        ),
        (
            "occurredAt",
            py_json(&record.getattr("occurred_at")?.call_method0("isoformat")?)?,
        ),
        ("details", py_json(&record.getattr("details")?)?),
    ]))
}

fn assignment_payload(assignment: &Bound<'_, PyAny>) -> PyResult<JsonValue> {
    Ok(object(vec![
        (
            "assignmentId",
            text(assignment.getattr("assignment_id")?.str()?.to_string()),
        ),
        (
            "userId",
            text(assignment.getattr("user_id")?.str()?.to_string()),
        ),
        (
            "projectName",
            py_json(&assignment.getattr("project_name")?)?,
        ),
        ("role", py_json(&assignment.getattr("role_name")?)?),
        ("targetName", py_json(&assignment.getattr("target_name")?)?),
        (
            "assignedBy",
            optional_uuid_text(&assignment.getattr("assigned_by")?)?,
        ),
        (
            "assignedAt",
            py_json(
                &assignment
                    .getattr("assigned_at")?
                    .call_method0("isoformat")?,
            )?,
        ),
        (
            "revokedBy",
            optional_uuid_text(&assignment.getattr("revoked_by")?)?,
        ),
        (
            "revokedAt",
            optional_iso(&assignment.getattr("revoked_at")?)?,
        ),
    ]))
}

fn account_errors(py: Python<'_>, error: PyErr, clauses: &[(&str, u16)]) -> Failure {
    let clauses: Vec<(&str, &str, u16)> = clauses
        .iter()
        .map(|(name, status)| (EXCEPTIONS_MODULE, *name, *status))
        .collect();
    super::support::translate(py, error, &clauses)
}

pub(crate) fn list_users(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    require_admin(call)?;
    let accounts = store(call).call_method0("list_users")?;
    let mut payload: Vec<JsonValue> = Vec::new();
    for account in accounts.try_iter()? {
        payload.push(user_payload(&account?)?);
    }
    call.payload_json(&JsonValue::Array(payload))
}

pub(crate) fn create_user(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let actor = require_admin(call)?;
    let body = call.body()?;
    let source = attr(py, "sqltide.auth.types", "AuthenticationSource")?
        .call1((body.getattr("authenticationSource")?,))?;
    let source_text: String = source.str()?.to_string();
    let password = body.getattr("password")?;
    if source_text == "password" && password.is_none() {
        return Err(Failure::status(400, "Password account requires a password"));
    }
    if source_text == "trusted_proxy" && !password.is_none() {
        return Err(Failure::status(
            400,
            "Proxy account must not include a password",
        ));
    }
    let username = body.getattr("username")?;
    let result: PyResult<Bound<'_, PyAny>> = (|| {
        let proxy: bool = source_text == "trusted_proxy";
        let external_subject: Option<Bound<'_, PyAny>> = if proxy {
            Some(attr(py, "sqltide._native", "canonical_username")?.call1((&username,))?)
        } else {
            None
        };
        let roles = PyTuple::new(
            py,
            body.getattr("roles")?
                .try_iter()?
                .collect::<PyResult<Vec<_>>>()?,
        )?;
        store(call).call_method(
            "create_user",
            (),
            Some(&py_kwargs!(py;
                username = &username,
                display_name = body.getattr("displayName")?,
                email = body.getattr("email")?,
                password = &password,
                authentication_source = if proxy { Some(source.clone()) } else { None },
                external_subject = external_subject,
                roles = roles,
                actor_user_id = user_id(&actor)?,
            )?),
        )
    })();
    match result {
        Ok(account) => call.payload_json(&user_payload(&account)?),
        Err(error) => Err(account_errors(
            py,
            error,
            &[
                ("PasswordValidationError", 400),
                ("AccountConflictError", 409),
                ("AccountNotFoundError", 409),
                ("AccountValidationError", 409),
            ],
        )),
    }
}

fn path_uuid<'py>(call: &Call<'py, '_>, index: usize) -> PyResult<Bound<'py, PyAny>> {
    attr(call.py, "uuid", "UUID")?.call1((call.path[index].text(),))
}

pub(crate) fn update_user(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let actor = require_admin(call)?;
    let body = call.body()?;
    let user_id_value = path_uuid(call, 0)?;
    let result: PyResult<Bound<'_, PyAny>> = (|| {
        let account = store(call).call_method(
            "get_user_by_id",
            (),
            Some(&py_kwargs!(py; user_id = &user_id_value)?),
        )?;
        if account.is_none() {
            return Err(PyErr::from_value(
                attr(py, EXCEPTIONS_MODULE, "AccountNotFoundError")?
                    .call1((format!("User '{}' was not found", user_id_value.str()?),))?,
            ));
        }
        let fields = body.getattr("model_fields_set")?;
        let display_name = if fields.contains("displayName")? {
            body.getattr("displayName")?
        } else {
            account.getattr("display_name")?
        };
        let email = if fields.contains("email")? {
            body.getattr("email")?
        } else {
            account.getattr("email")?
        };
        let active = body.getattr("active")?;
        let is_active = if active.is_none() {
            account.getattr("is_active")?
        } else {
            active
        };
        store(call).call_method(
            "update_account",
            (),
            Some(&py_kwargs!(py;
                user_id = &user_id_value,
                display_name = display_name,
                email = email,
                is_active = is_active,
                actor_user_id = user_id(&actor)?,
            )?),
        )
    })();
    match result {
        Ok(account) => call.payload_json(&user_payload(&account)?),
        Err(error) => Err(account_errors(
            py,
            error,
            &[("AccountNotFoundError", 404), ("AccountConflictError", 409)],
        )),
    }
}

pub(crate) fn reset_password(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let actor = require_admin(call)?;
    let body = call.body()?;
    let user_id_value = path_uuid(call, 0)?;
    if let Err(error) = store(call).call_method(
        "reset_password",
        (),
        Some(&py_kwargs!(py;
            user_id = &user_id_value,
            password = body.getattr("password")?,
            actor_user_id = user_id(&actor)?,
        )?),
    ) {
        return Err(account_errors(
            py,
            error,
            &[
                ("AccountNotFoundError", 404),
                ("PasswordValidationError", 400),
            ],
        ));
    }
    call.payload_json(&object(vec![("status", text("ok"))]))
}

pub(crate) fn grant_role(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let actor = require_admin(call)?;
    let body = call.body()?;
    let result = store(call).call_method(
        "grant_role",
        (),
        Some(&py_kwargs!(py;
            user_id = path_uuid(call, 0)?,
            role_name = body.getattr("role")?,
            actor_user_id = user_id(&actor)?,
        )?),
    );
    match result {
        Ok(account) => call.payload_json(&user_payload(&account)?),
        Err(error) => Err(account_errors(
            py,
            error,
            &[("AccountNotFoundError", 404), ("AccountConflictError", 409)],
        )),
    }
}

pub(crate) fn revoke_role(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let actor = require_admin(call)?;
    let result = store(call).call_method(
        "revoke_role",
        (),
        Some(&py_kwargs!(py;
            user_id = path_uuid(call, 0)?,
            role_name = call.path[1].text(),
            actor_user_id = user_id(&actor)?,
        )?),
    );
    match result {
        Ok(account) => call.payload_json(&user_payload(&account)?),
        Err(error) => Err(account_errors(
            py,
            error,
            &[("AccountNotFoundError", 404), ("AccountConflictError", 409)],
        )),
    }
}

pub(crate) fn read_account_audit(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    require_admin(call)?;
    let records = store(call).call_method0("list_audit_records")?;
    let mut payload: Vec<JsonValue> = Vec::new();
    for record in records.try_iter()? {
        payload.push(audit_payload(&record?)?);
    }
    call.payload_json(&JsonValue::Array(payload))
}

pub(crate) fn list_project_roles(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    require_admin(call)?;
    let assignments = store(call).call_method(
        "list_project_role_assignments",
        (),
        Some(&py_kwargs!(py;
            user_id = path_uuid(call, 0)?,
            project_name = call.query_text(0).unwrap_or_default(),
            include_revoked = call.query_flag(1),
        )?),
    )?;
    let mut payload: Vec<JsonValue> = Vec::new();
    for assignment in assignments.try_iter()? {
        payload.push(assignment_payload(&assignment?)?);
    }
    call.payload_json(&JsonValue::Array(payload))
}

pub(crate) fn grant_project_role(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let actor = require_admin(call)?;
    let body = call.body()?;
    let result = store(call).call_method(
        "grant_project_role",
        (),
        Some(&py_kwargs!(py;
            user_id = path_uuid(call, 0)?,
            project_name = body.getattr("projectName")?,
            role_name = body.getattr("role")?,
            target_name = body.getattr("targetName")?,
            actor_user_id = user_id(&actor)?,
        )?),
    );
    match result {
        Ok(assignment) => call.payload_json(&assignment_payload(&assignment)?),
        Err(error) => Err(account_errors(
            py,
            error,
            &[
                ("AccountValidationError", 400),
                ("AccountConflictError", 409),
            ],
        )),
    }
}

pub(crate) fn revoke_project_role(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let actor = require_admin(call)?;
    let result = store(call).call_method(
        "revoke_project_role",
        (),
        Some(
            &py_kwargs!(py; assignment_id = path_uuid(call, 0)?, actor_user_id = user_id(&actor)?)?,
        ),
    );
    match result {
        Ok(assignment) => call.payload_json(&assignment_payload(&assignment)?),
        Err(error) => Err(account_errors(py, error, &[("AccountNotFoundError", 404)])),
    }
}
