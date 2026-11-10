//! The dev server: an axum application whose routes, authentication and static UI serving
//! replace the earlier ASGI stack.

pub(crate) mod app;
pub(crate) mod auth;
pub(crate) mod authz;
pub(crate) mod create;
pub(crate) mod destruction;
pub(crate) mod http;
pub(crate) mod routes;
pub(crate) mod sensors;
pub(crate) mod static_files;
pub(crate) mod support;

pub(crate) use create::register;
