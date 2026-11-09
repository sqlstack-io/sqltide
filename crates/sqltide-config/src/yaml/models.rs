//! The composed node tree of one YAML document, before construction.

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum NodeContent {
    Scalar(String),
    Sequence(Vec<Node>),
    Mapping(Vec<(Node, Node)>),
}

/// One node with its resolved tag and the one-based line and column where it starts.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Node {
    pub(crate) tag: String,
    pub(crate) content: NodeContent,
    pub(crate) position: (usize, usize),
}

impl Node {
    /// The node kind that construction errors name.
    pub(crate) fn kind(&self) -> &'static str {
        match self.content {
            NodeContent::Scalar(_) => "scalar",
            NodeContent::Sequence(_) => "sequence",
            NodeContent::Mapping(_) => "mapping",
        }
    }
}
