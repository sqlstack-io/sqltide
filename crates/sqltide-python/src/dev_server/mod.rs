//! The dev server: an axum application whose routes, authentication and static UI serving
//! replace the earlier ASGI stack.

pub(crate) mod activity;
pub(crate) mod app;
pub(crate) mod auth;
pub(crate) mod audits;
pub(crate) mod authz;
pub(crate) mod build_process;
pub(crate) mod checks;
pub(crate) mod create;
pub(crate) mod definitions;
pub(crate) mod deployments;
pub(crate) mod destruction;
pub(crate) mod drift;
pub(crate) mod http;
pub(crate) mod lifecycle;
pub(crate) mod messages;
pub(crate) mod payloads;
pub(crate) mod plan;
pub(crate) mod readers;
pub(crate) mod routes;
pub(crate) mod runs;
pub(crate) mod sensor_scheduler;
pub(crate) mod sensors;
pub(crate) mod state;
pub(crate) mod static_files;
pub(crate) mod support;
pub(crate) mod terminal;
pub(crate) mod warehouse;

pub(crate) use create::register;
