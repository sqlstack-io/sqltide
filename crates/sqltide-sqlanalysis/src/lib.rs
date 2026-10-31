//! Analysis of parsed Polyglot trees for the SQLTide compiler. Plain Rust with no Python
//! dependency.
//!
//! Trees are Polyglot's serde form of an [`polyglot_sql::Expression`] held as a
//! [`serde_json::Value`], so each walk reads the same keys in the same order as the Python walks
//! it replaces did over the equivalent dicts. Only compact facts and generated SQL leave a walk.
//!
//! Ported from SQLTide's Python `sql_analysis/_helpers/{polyglot,aggregates,projections,storage}.py`,
//! which adapted Apache-2.0 SQLBuild `planner/_helpers/scenario/relations.py` at 7e3b2f854f05.

mod aggregates;
mod catalog;
mod facts;
mod model;
mod projections;
mod relations;
mod rewriting;
mod storage;
mod tree;
mod vocabulary;

pub use catalog::{
    CatalogAnalysis, RelationIdentity, analyze_catalog, canonicalize_sql, expression_list,
    render_string_literal, validate_insert_query, validate_query,
};
pub use model::{ModelError, ParsedModel, analyze_model};
pub use projections::{Projection, ProjectionTypeCache};
pub use relations::{
    ExpectedReference, RelationCache, ResolvedQuery, Resolver,
    canonical_query_with_database_template, validated_relation_rewrite,
};
pub use rewriting::{
    NamedQuery, RelationRewrite, RewrittenQuery, rewrite_query, select_aggregate_facts,
};
pub use storage::storage_column_names;
pub use tree::{AnalysisError, SqlDialect};

#[cfg(test)]
mod tests;
