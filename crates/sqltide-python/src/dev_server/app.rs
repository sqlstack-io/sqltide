//! The dev server application: the route table, request dispatch through the authentication
//! gate, validation, handlers and compression, the in-process client tests drive, the
//! startup and shutdown lifecycle, and the socket server.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::Router;
use axum::body::Body as AxumBody;
use axum::extract::{ConnectInfo, Request};
use axum::response::Response;
use pyo3::exceptions::{PyKeyboardInterrupt, PySystemExit};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyTuple};
use sqltide_json::JsonValue;
use tokio::sync::{Notify, Semaphore};

use super::auth::{AuthService, LoginLimiter, Refusal, protects};
use super::http::{
    Failure, HttpReply, HttpRequest, Outcome, Route, Routed, bodiless, gzip, http_date, method_not_allowed,
    now_seconds, object, percent_decode, py_json, route,
};
use super::routes::routes;
use super::static_files::StaticAssets;
use super::support::{Body, Scalar, Value, attr, parse_scalar, read_body, validate_body, validate_string_mapping};

/// Synchronous handlers run on at most this many worker threads at once, the limit the earlier
/// thread pool applied.
const WORKER_LIMIT: usize = 40;
const SIGNAL_POLL: Duration = Duration::from_millis(100);

pub(crate) type Handler = for<'py, 'a> fn(&Call<'py, 'a>) -> Outcome<HttpReply>;

/// A query parameter a route declares.
pub(crate) struct QueryParam {
    pub(crate) name: &'static str,
    pub(crate) scalar: Scalar,
    pub(crate) required: bool,
    /// A `list[str]` parameter, which collects every value.
    pub(crate) many: bool,
}

impl QueryParam {
    pub(crate) const fn optional(name: &'static str, scalar: Scalar) -> Self {
        Self {
            name,
            scalar,
            required: false,
            many: false,
        }
    }
}

/// How a route reads its request body.
#[derive(Clone, Copy)]
pub(crate) enum BodySpec {
    None,
    /// A pydantic request model, by module and class name.
    Model(&'static str, &'static str),
    /// A `dict[str, str]` body.
    StringMapping,
}

/// One route's handler and the parameters it validates.
pub(crate) struct Endpoint {
    pub(crate) handler: Handler,
    pub(crate) path: &'static [Scalar],
    pub(crate) query: &'static [QueryParam],
    pub(crate) body: BodySpec,
    pub(crate) status: u16,
}

impl Endpoint {
    pub(crate) const fn new(handler: Handler) -> Self {
        Self {
            handler,
            path: &[],
            query: &[],
            body: BodySpec::None,
            status: 200,
        }
    }

    pub(crate) const fn path(self, path: &'static [Scalar]) -> Self {
        Self { path, ..self }
    }

    pub(crate) const fn query(self, query: &'static [QueryParam]) -> Self {
        Self { query, ..self }
    }

    pub(crate) const fn body(self, body: BodySpec) -> Self {
        Self { body, ..self }
    }

    pub(crate) const fn status(self, status: u16) -> Self {
        Self { status, ..self }
    }
}

/// The Python services one application owns, built by `create_dev_app`.
pub(crate) struct Services {
    pub(crate) state: Py<PyAny>,
    pub(crate) warehouse: Py<PyAny>,
    pub(crate) builds: Py<PyAny>,
    pub(crate) audit_scheduler: Py<PyAny>,
    pub(crate) sensor_scheduler: Py<PyAny>,
    pub(crate) kafka_lag_reader: Py<PyAny>,
    pub(crate) kafka_topic_reader: Py<PyAny>,
    pub(crate) control_stores: Py<PyAny>,
    pub(crate) reporter: Py<PyAny>,
    pub(crate) execution_context: Py<PyAny>,
    /// `OperationAuthorizationContext` over the account store.
    pub(crate) authorization: Py<PyAny>,
    pub(crate) project_dir: Py<PyAny>,
    pub(crate) database: Option<String>,
    pub(crate) selected_target: Option<String>,
    pub(crate) presumed_failed_after_seconds: i64,
}

/// The application shared by every request.
pub(crate) struct DevApp {
    pub(crate) auth: AuthService,
    pub(crate) limiter: LoginLimiter,
    pub(crate) services: Services,
    pub(crate) routes: Vec<Route<Endpoint>>,
    pub(crate) assets: Mutex<Option<StaticAssets>>,
    /// Destruction plans whose background execution is running.
    pub(crate) dispatches: Mutex<std::collections::HashSet<String>>,
}

/// One validated request, as a handler sees it.
pub(crate) struct Call<'py, 'a> {
    pub(crate) py: Python<'py>,
    pub(crate) app: &'a Arc<DevApp>,
    pub(crate) request: &'a HttpRequest,
    pub(crate) authenticated: Option<Bound<'py, PyAny>>,
    pub(crate) path: Vec<Value>,
    pub(crate) query: Vec<Option<Value>>,
    body: Option<Bound<'py, PyAny>>,
    status: u16,
}

impl<'py, 'a> Call<'py, 'a> {
    /// A call outside request dispatch, for work that continues after a route returned.
    pub(crate) fn detached(
        py: Python<'py>,
        app: &'a Arc<DevApp>,
        request: &'a HttpRequest,
        authenticated: Option<Bound<'py, PyAny>>,
    ) -> Self {
        Self {
            py,
            app,
            request,
            authenticated,
            path: Vec::new(),
            query: Vec::new(),
            body: None,
            status: 200,
        }
    }

    /// The validated request body.
    pub(crate) fn body(&self) -> PyResult<Bound<'py, PyAny>> {
        self.body
            .clone()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("route has no body"))
    }

    pub(crate) fn query_text(&self, index: usize) -> Option<String> {
        self.query[index].as_ref().map(|value| value.text().to_owned())
    }

    pub(crate) fn query_flag(&self, index: usize) -> bool {
        self.query[index].as_ref().is_some_and(Value::boolean)
    }

    pub(crate) fn query_int(&self, index: usize, default: i128) -> i128 {
        self.query[index].as_ref().map_or(default, Value::integer)
    }

    pub(crate) fn query_list(&self, index: usize) -> Option<Vec<String>> {
        match &self.query[index] {
            Some(Value::List(values)) => Some(values.clone()),
            _ => None,
        }
    }

    pub(crate) fn service(&self, pick: fn(&Services) -> &Py<PyAny>) -> Bound<'py, PyAny> {
        pick(&self.app.services).bind(self.py).clone()
    }

    /// A handler's JSON result, written as the response serializer writes it.
    pub(crate) fn payload_json(&self, value: &JsonValue) -> Outcome<HttpReply> {
        Ok(HttpReply::payload_with_status(self.status, value)?)
    }

    /// A handler's Python result.
    pub(crate) fn payload(&self, value: &Bound<'_, PyAny>) -> Outcome<HttpReply> {
        self.payload_json(&py_json(value)?)
    }
}

impl DevApp {
    /// Answer one request; a Python exception no handler mapped propagates.
    pub(crate) fn handle(self: &Arc<Self>, py: Python<'_>, request: &HttpRequest) -> PyResult<HttpReply> {
        let mut authenticated: Option<Bound<'_, PyAny>> = None;
        if protects(&request.path) {
            match self.auth.authenticate(py, request)? {
                Ok(principal) => authenticated = Some(principal),
                Err(refusal) => return finish(refusal.reply()?),
            }
        }
        let reply: HttpReply = match route(&self.routes, request) {
            Routed::Matched(endpoint, parameters) => {
                match self.dispatch(py, request, endpoint, &parameters, authenticated) {
                    Ok(reply) => reply,
                    Err(Failure::Http { status, detail, headers }) => Failure::reply(status, &detail, &headers)?,
                    Err(Failure::Validation(errors)) => {
                        HttpReply::json(422, &object(vec![("detail", JsonValue::Array(errors))]))?
                    }
                    Err(Failure::Python(error)) => {
                        // The authentication middleware also maps these when a private route
                        // raises them.
                        if protects(&request.path)
                            && let Some(refusal) = Refusal::classify(py, &error)? {
                                return finish(refusal.reply()?);
                            }
                        return Err(error);
                    }
                }
            }
            unmatched => {
                // The asset mounts and the SPA catch-all come after every API route; a full
                // match there wins over an API route that only matched the path.
                let assets: Option<StaticAssets> = self.assets.lock().ok().and_then(|assets| assets.clone());
                let served: Option<HttpReply> = assets.as_ref().and_then(|assets| assets.serve(py, request));
                match (served, unmatched, assets.is_some()) {
                    (Some(reply), _, _) => reply,
                    (None, Routed::MethodNotAllowed(methods), _) => method_not_allowed(&methods),
                    (None, _, true) => method_not_allowed(&["GET"]),
                    (None, Routed::Redirect(location), false) => HttpReply {
                        status: 307,
                        headers: vec![("content-length".to_owned(), "0".to_owned()), ("location".to_owned(), location)],
                        body: Vec::new(),
                    },
                    (None, _, false) => Failure::reply(404, &super::http::text("Not Found"), &[])?,
                }
            }
        };
        finish(gzip(request, reply))
    }

    fn dispatch<'py>(
        self: &Arc<Self>,
        py: Python<'py>,
        request: &HttpRequest,
        endpoint: &Endpoint,
        parameters: &[(String, String)],
        authenticated: Option<Bound<'py, PyAny>>,
    ) -> Outcome<HttpReply> {
        let mut errors: Vec<JsonValue> = Vec::new();
        let mut path: Vec<Value> = Vec::new();
        for ((name, raw), scalar) in parameters.iter().zip(endpoint.path) {
            match parse_scalar(py, *scalar, raw, vec![super::http::text("path"), super::http::text(name.clone())])? {
                Ok(value) => path.push(value),
                Err(error) => {
                    errors.push(error);
                    path.push(Value::Text(raw.clone()));
                }
            }
        }
        let pairs: Vec<(String, String)> = request.query();
        let mut query: Vec<Option<Value>> = Vec::new();
        for parameter in endpoint.query {
            let values: Vec<&String> = pairs
                .iter()
                .filter(|(key, _)| key == parameter.name)
                .map(|(_, value)| value)
                .collect();
            if parameter.many {
                query.push((!values.is_empty()).then(|| Value::List(values.into_iter().cloned().collect())));
                continue;
            }
            let Some(raw) = values.last() else {
                if parameter.required {
                    errors.push(super::support::validation_error(
                        "missing",
                        vec![super::http::text("query"), super::http::text(parameter.name)],
                        "Field required",
                        JsonValue::Null,
                        None,
                    ));
                }
                query.push(None);
                continue;
            };
            match parse_scalar(
                py,
                parameter.scalar,
                raw,
                vec![super::http::text("query"), super::http::text(parameter.name)],
            )? {
                Ok(value) => query.push(Some(value)),
                Err(error) => {
                    errors.push(error);
                    query.push(None);
                }
            }
        }
        let body: Option<Bound<'py, PyAny>> = match endpoint.body {
            BodySpec::None => None,
            BodySpec::Model(module, model) => {
                let raw: Body<'py> = read_body(py, request)?;
                validate_body(py, &raw, module, model, &mut errors)?
            }
            BodySpec::StringMapping => {
                let raw: Body<'py> = read_body(py, request)?;
                validate_string_mapping(py, &raw, &mut errors)?
            }
        };
        if !errors.is_empty() {
            return Err(Failure::Validation(errors));
        }
        let call: Call<'py, '_> = Call {
            py,
            app: self,
            request,
            authenticated,
            path,
            query,
            body,
            status: endpoint.status,
        };
        (endpoint.handler)(&call)
    }

    /// Start schedulers and warm caches; on a failure, close what started, as the lifespan's
    /// `finally` did.
    pub(crate) fn startup(&self, py: Python<'_>) -> PyResult<()> {
        let services: &Services = &self.services;
        let started: PyResult<()> = (|| {
            services.warehouse.bind(py).call_method0("start")?;
            let sensors = services.sensor_scheduler.bind(py);
            sensors.call_method0("initialize_event_checkpoints")?;
            sensors.call_method0("start")?;
            services.audit_scheduler.bind(py).call_method0("start")?;
            services.state.bind(py).getattr("snapshot")?.call_method0("start")?;
            self.warm_broker_caches(py)
        })();
        if let Err(error) = started {
            return Err(match self.shutdown(py) {
                Ok(()) => error,
                Err(closing) => crate::handling::while_handling(py, closing, &error),
            });
        }
        Ok(())
    }

    fn warm_broker_caches(&self, py: Python<'_>) -> PyResult<()> {
        let services: &Services = &self.services;
        let analysis = match services.state.bind(py).call_method0("current_analysis") {
            Ok(analysis) => analysis,
            Err(error) if super::support::is_instance(py, &error, "sqltide.dev_server.exceptions", "ProjectNotCompiledError")? => {
                return Ok(());
            }
            Err(error) => return Err(error),
        };
        attr(py, "sqltide._native", "build_topics_payload")?.call(
            (),
            Some(&crate::py_kwargs!(py;
                analysis = analysis,
                connection = py.None(),
                database = services.database.clone(),
                topic_reader = services.kafka_topic_reader.bind(py),
                kafka_lag_reader = services.kafka_lag_reader.bind(py),
            )?),
        )?;
        Ok(())
    }

    /// Close every service in order; a failure is chained to the next as `finally` chains.
    pub(crate) fn shutdown(&self, py: Python<'_>) -> PyResult<()> {
        let services: &Services = &self.services;
        let steps: [Box<dyn Fn() -> PyResult<()> + '_>; 8] = [
            Box::new(|| services.state.bind(py).getattr("snapshot")?.call_method0("close").map(drop)),
            Box::new(|| services.sensor_scheduler.bind(py).call_method0("close").map(drop)),
            Box::new(|| services.audit_scheduler.bind(py).call_method0("close").map(drop)),
            Box::new(|| services.kafka_lag_reader.bind(py).call_method0("close").map(drop)),
            Box::new(|| services.kafka_topic_reader.bind(py).call_method0("close").map(drop)),
            Box::new(|| services.builds.bind(py).call_method0("close").map(drop)),
            Box::new(|| services.warehouse.bind(py).call_method0("close").map(drop)),
            Box::new(|| services.control_stores.bind(py).call_method0("close").map(drop)),
        ];
        // The earlier lifespan closed these in sequence without guarding each step, so the
        // first failure stops the rest.
        for step in steps {
            step()?;
        }
        Ok(())
    }
}

/// `Content-Length` as the response classes set it. A `HEAD` reply keeps its body here, as
/// the ASGI app sent it; the HTTP server drops it on the wire.
fn finish(mut reply: HttpReply) -> PyResult<HttpReply> {
    if !bodiless(reply.status) && reply.header("content-length").is_none() {
        reply.headers.push(("content-length".to_owned(), reply.body.len().to_string()));
    }
    Ok(reply)
}

/// The dev server application object Python holds.
#[pyclass(frozen, module = "sqltide._native")]
pub(crate) struct DevApplication {
    pub(crate) app: Arc<DevApp>,
}

#[pymethods]
impl DevApplication {
    /// Answer one request in process: `(status, headers, body)`. A Python exception no route
    /// maps propagates, as the earlier in-process test client re-raised it.
    #[pyo3(signature = (method, path, *, query_string = "", headers = Vec::new(), body = Vec::new(), client = None))]
    #[allow(clippy::too_many_arguments)]
    fn request<'py>(
        &self,
        py: Python<'py>,
        method: &str,
        path: &str,
        query_string: &str,
        headers: Vec<(String, String)>,
        body: Vec<u8>,
        client: Option<String>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let request: HttpRequest = HttpRequest {
            method: method.to_ascii_uppercase(),
            path: path.to_owned(),
            query_string: query_string.to_owned(),
            headers: headers
                .into_iter()
                .map(|(name, value)| (name.to_ascii_lowercase(), value))
                .collect(),
            body,
            client_host: client,
            scheme: "http".to_owned(),
        };
        let reply: HttpReply = self.app.handle(py, &request)?;
        let headers: Bound<'py, PyList> = PyList::empty(py);
        for (name, value) in &reply.headers {
            headers.append((name.as_str(), value.as_str()))?;
        }
        PyTuple::new(
            py,
            [
                reply.status.into_pyobject(py)?.into_any(),
                headers.into_any(),
                PyBytes::new(py, &reply.body).into_any(),
            ],
        )
    }

    /// The route table as `(methods, path)` pairs.
    fn routes(&self) -> Vec<(Vec<&'static str>, String)> {
        self.app
            .routes
            .iter()
            .map(|route| (route.methods.to_vec(), route.template.clone()))
            .collect()
    }

    /// Serve the packaged UI build under every path the API does not take.
    fn mount_static_assets(&self, assets_root: PathBuf) {
        if StaticAssets::present(&assets_root)
            && let Ok(mut assets) = self.app.assets.lock() {
                *assets = Some(StaticAssets { root: assets_root });
            }
    }

    /// Run the startup half of the lifecycle.
    fn startup(&self, py: Python<'_>) -> PyResult<()> {
        self.app.startup(py)
    }

    /// Run the shutdown half of the lifecycle.
    fn shutdown(&self, py: Python<'_>) -> PyResult<()> {
        self.app.shutdown(py)
    }

    /// The shared server state.
    #[getter]
    fn state<'py>(&self, py: Python<'py>) -> Bound<'py, PyAny> {
        self.app.services.state.bind(py).clone()
    }

    /// Serve HTTP on `host:port` until interrupted, with the lifecycle around it.
    fn serve(&self, py: Python<'_>, host: &str, port: u16) -> PyResult<()> {
        serve(py, &self.app, host, port)
    }
}

/// Start, serve until Ctrl-C or SIGTERM, and shut down, as the earlier server did.
fn serve(py: Python<'_>, app: &Arc<DevApp>, host: &str, port: u16) -> PyResult<()> {
    let runtime: tokio::runtime::Runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .thread_name("sqltide-dev-server")
        .build()
        .map_err(|error| pyo3::exceptions::PyOSError::new_err(error.to_string()))?;
    if let Err(error) = app.startup(py) {
        error.print(py);
        eprintln!("ERROR:    Application startup failed. Exiting.");
        return Err(PySystemExit::new_err(3));
    }
    let address: String = if host.contains(':') {
        format!("[{host}]:{port}")
    } else {
        format!("{host}:{port}")
    };
    let bound: std::io::Result<Vec<tokio::net::TcpListener>> =
        py.detach(|| runtime.block_on(bind_all(&address)));
    let listeners: Vec<tokio::net::TcpListener> = match bound {
        Ok(listeners) => listeners,
        Err(error) => {
            let reason: String = match error.raw_os_error() {
                Some(code) => format!(
                    "[Errno {code}] error while attempting to bind on address ('{host}', {port}): {}",
                    attr(py, "os", "strerror")?.call1((code,))?.str()?.to_string().to_lowercase()
                ),
                None => error.to_string(),
            };
            eprintln!("ERROR:    {reason}");
            app.shutdown(py)?;
            return Err(PySystemExit::new_err(3));
        }
    };
    let stop: Arc<Notify> = Arc::new(Notify::new());
    let workers: Arc<Semaphore> = Arc::new(Semaphore::new(WORKER_LIMIT));
    let mut servers: Vec<tokio::task::JoinHandle<std::io::Result<()>>> = Vec::new();
    for listener in listeners {
        let router: Router = Router::new().fallback({
            let app: Arc<DevApp> = Arc::clone(app);
            let workers: Arc<Semaphore> = Arc::clone(&workers);
            move |connect: ConnectInfo<SocketAddr>, request: Request| {
                answer(Arc::clone(&app), Arc::clone(&workers), connect.0, request)
            }
        });
        let stop: Arc<Notify> = Arc::clone(&stop);
        servers.push(runtime.spawn(async move {
            axum::serve(listener, router.into_make_service_with_connect_info::<SocketAddr>())
                .with_graceful_shutdown(async move { stop.notified().await })
                .await
        }));
    }
    let terminated: Arc<Notify> = Arc::new(Notify::new());
    #[cfg(unix)]
    {
        let terminated: Arc<Notify> = Arc::clone(&terminated);
        let stop: Arc<Notify> = Arc::clone(&stop);
        runtime.spawn(async move {
            if let Ok(mut signal) = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                && signal.recv().await.is_some() {
                    stop.notify_waiters();
                    terminated.notify_one();
                }
        });
    }
    let mut stopping: bool = false;
    loop {
        let finished: bool = py.detach(|| {
            runtime.block_on(async {
                tokio::select! {
                    () = tokio::time::sleep(SIGNAL_POLL) => {}
                    () = terminated.notified() => {}
                }
            });
            servers.iter().all(tokio::task::JoinHandle::is_finished)
        });
        if finished {
            break;
        }
        if let Err(error) = py.check_signals() {
            if !error.is_instance_of::<PyKeyboardInterrupt>(py) {
                return Err(error);
            }
            if stopping {
                // A second interrupt abandons the connections still open.
                break;
            }
            stopping = true;
            stop.notify_waiters();
        }
    }
    py.detach(|| runtime.shutdown_timeout(Duration::from_secs(1)));
    app.shutdown(py)
}

/// Bind every address `address` resolves to, as the earlier server bound a host name.
async fn bind_all(address: &str) -> std::io::Result<Vec<tokio::net::TcpListener>> {
    let mut listeners: Vec<tokio::net::TcpListener> = Vec::new();
    let mut last_error: Option<std::io::Error> = None;
    let mut seen: Vec<SocketAddr> = Vec::new();
    for candidate in tokio::net::lookup_host(address).await? {
        if seen.contains(&candidate) {
            continue;
        }
        seen.push(candidate);
        match tokio::net::TcpListener::bind(candidate).await {
            Ok(listener) => listeners.push(listener),
            Err(error) => {
                last_error = Some(error);
                break;
            }
        }
    }
    match last_error {
        Some(error) => Err(error),
        None if listeners.is_empty() => Err(std::io::Error::other("no address to bind")),
        None => Ok(listeners),
    }
}

/// Run one request on a blocking worker that attaches to Python; tokio workers never hold
/// the interpreter lock.
async fn answer(app: Arc<DevApp>, workers: Arc<Semaphore>, peer: SocketAddr, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let Ok(bytes) = axum::body::to_bytes(body, usize::MAX).await else {
        return plain(400, "Bad Request");
    };
    let raw_path: &str = parts.uri.path();
    let http: HttpRequest = HttpRequest {
        method: parts.method.as_str().to_ascii_uppercase(),
        path: percent_decode(raw_path),
        query_string: parts.uri.query().unwrap_or("").to_owned(),
        headers: parts
            .headers
            .iter()
            .map(|(name, value)| {
                (
                    name.as_str().to_ascii_lowercase(),
                    value.as_bytes().iter().map(|byte| char::from(*byte)).collect(),
                )
            })
            .collect(),
        body: bytes.to_vec(),
        client_host: Some(peer.ip().to_string()),
        scheme: "http".to_owned(),
    };
    let Ok(_permit) = workers.acquire_owned().await else {
        return plain(503, "Service Unavailable");
    };
    let outcome = tokio::task::spawn_blocking(move || {
        Python::attach(|py| match app.handle(py, &http) {
            Ok(reply) => reply,
            Err(error) => {
                eprintln!("ERROR:    Exception in ASGI application");
                error.print(py);
                HttpReply::new(500, "text/plain; charset=utf-8", b"Internal Server Error".to_vec())
            }
        })
    })
    .await;
    let Ok(reply) = outcome else {
        return plain(500, "Internal Server Error");
    };
    let mut response: Response = Response::new(AxumBody::from(reply.body));
    *response.status_mut() = axum::http::StatusCode::from_u16(reply.status)
        .unwrap_or(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
    let headers = response.headers_mut();
    for (name, value) in reply.headers {
        let bytes: Vec<u8> = value.chars().map(|character| u8::try_from(u32::from(character)).unwrap_or(b'?')).collect();
        if let (Ok(name), Ok(value)) = (
            axum::http::HeaderName::from_bytes(name.as_bytes()),
            axum::http::HeaderValue::from_bytes(&bytes),
        ) {
            headers.append(name, value);
        }
    }
    if let Ok(date) = axum::http::HeaderValue::from_str(&http_date(now_seconds())) {
        headers.insert(axum::http::header::DATE, date);
    }
    response
}

fn plain(status: u16, text: &'static str) -> Response {
    let mut response: Response = Response::new(AxumBody::from(text));
    *response.status_mut() = axum::http::StatusCode::from_u16(status).unwrap_or(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
    response
}

/// The route table, in the order the earlier application registered its routes.
pub(crate) fn route_table() -> Vec<Route<Endpoint>> {
    routes()
}
