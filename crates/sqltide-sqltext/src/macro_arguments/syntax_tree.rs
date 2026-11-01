//! Parse argument tokens into Python's `ast` node shapes for the literal subset.

use super::failures::{Failure, syntax};
use super::lexer::{NumberToken, Token};

const PYTHON_KEYWORDS: [&str; 35] = [
    "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue",
    "def", "del", "elif", "else", "except", "finally", "for", "from", "global", "if", "import",
    "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try", "while",
    "with", "yield",
];

/// One expression node.
#[derive(Clone, Debug)]
pub enum Node {
    /// The string's code points, which may include lone surrogates.
    Str {
        value: Vec<u32>,
        bytes: bool,
        formatted: bool,
    },
    Number(NumberToken),
    Ellipsis,
    Name(String),
    /// The nested macro call at this index of the call's nested spans.
    Nested(usize),
    Call,
    List(Vec<Node>),
    Tuple(Vec<Node>),
    /// Key and value pairs; a `None` key is `**` unpacking.
    Dict(Vec<(Option<Node>, Node)>),
    Set,
    Starred,
    Unary {
        negative: bool,
        operand: Box<Node>,
    },
}

/// One keyword argument; `name` is `None` for `**` expansion.
#[derive(Clone, Debug)]
pub struct Keyword {
    pub name: Option<String>,
    pub value: Node,
}

/// The arguments of one call, as `ast.Call.args` and `ast.Call.keywords`.
#[derive(Clone, Debug)]
pub struct CallArguments {
    pub args: Vec<Node>,
    pub keywords: Vec<Keyword>,
}

struct Parser<'tokens> {
    tokens: &'tokens [Token],
    index: usize,
}

/// Parse every token as the arguments of one call.
pub(crate) fn parse_call_arguments(tokens: &[Token]) -> Result<CallArguments, Failure> {
    let mut parser = Parser { tokens, index: 0 };
    let arguments: CallArguments = parser.arguments()?;
    if matches!(parser.peek(), Token::End) {
        Ok(arguments)
    } else {
        Err(parser.unexpected())
    }
}

impl Parser<'_> {
    fn peek(&self) -> &Token {
        &self.tokens[self.index]
    }

    fn peek_at(&self, offset: usize) -> Option<&Token> {
        self.tokens.get(self.index + offset)
    }

    fn at_operator(&self, operator: &str) -> bool {
        matches!(self.peek(), Token::Operator(found) if *found == operator)
    }

    fn at_end(&self) -> bool {
        matches!(self.peek(), Token::End)
    }

    fn eat(&mut self, operator: &str) -> bool {
        let found: bool = self.at_operator(operator);
        if found {
            self.index += 1;
        }
        found
    }

    /// The error for the next token where no supported argument can continue.
    fn unexpected(&self) -> Failure {
        if self.missing_comma() {
            return syntax("a comma is missing between arguments");
        }
        match self.peek() {
            Token::End => syntax("the arguments end where a value is expected"),
            Token::Operator(")" | "]" | "}") => {
                syntax("a closing bracket does not match an opening one")
            }
            _ => Failure::unsupported(),
        }
    }

    /// Whether the next token starts a value right after one ended, so a comma is missing.
    fn missing_comma(&self) -> bool {
        let starts_value: bool = match self.peek() {
            Token::Name(name) => {
                matches!(name.as_str(), "True" | "False" | "None")
                    || !PYTHON_KEYWORDS.contains(&name.as_str())
            }
            Token::Str(_) | Token::Number(_) | Token::Nested(_) => true,
            _ => self.at_operator("{"),
        };
        let ends_value: bool = self
            .index
            .checked_sub(1)
            .and_then(|previous| self.tokens.get(previous))
            .is_some_and(|token| {
                matches!(
                    token,
                    Token::Str(_)
                        | Token::Number(_)
                        | Token::Name(_)
                        | Token::Nested(_)
                        | Token::Operator(")" | "]" | "}")
                )
            });
        starts_value && ends_value
    }

    /// Arguments up to a `)` or the end, as Python's call grammar orders them.
    fn arguments(&mut self) -> Result<CallArguments, Failure> {
        let mut args: Vec<Node> = Vec::new();
        let mut keywords: Vec<Keyword> = Vec::new();
        let mut expanded: bool = false;
        while !self.at_end() && !self.at_operator(")") {
            if self.eat("**") {
                expanded = true;
                keywords.push(Keyword {
                    name: None,
                    value: self.expression()?,
                });
            } else if let Some(name) = self.keyword_name()? {
                keywords.push(Keyword {
                    name: Some(name),
                    value: self.expression()?,
                });
            } else {
                if !keywords.is_empty() && (expanded || !self.at_operator("*")) {
                    return Err(syntax(if expanded {
                        "positional argument follows keyword argument unpacking"
                    } else {
                        "positional argument follows keyword argument"
                    }));
                }
                args.push(self.starred_or_expression()?);
            }
            if !self.eat(",") {
                break;
            }
        }
        Ok(CallArguments { args, keywords })
    }

    /// `name=` starting a keyword argument, consumed, or `None` without consuming.
    fn keyword_name(&mut self) -> Result<Option<String>, Failure> {
        let Token::Name(name) = self.peek() else {
            return Ok(None);
        };
        let assigns: bool = matches!(self.peek_at(1), Some(Token::Operator("=")))
            && !matches!(self.peek_at(2), Some(Token::Operator("=")));
        if !assigns {
            return Ok(None);
        }
        let name: String = name.clone();
        if PYTHON_KEYWORDS.contains(&name.as_str()) {
            return Err(syntax(&format!(
                "'{name}' is a Python keyword, not an argument name"
            )));
        }
        self.index += 2;
        Ok(Some(name))
    }

    fn starred_or_expression(&mut self) -> Result<Node, Failure> {
        if self.eat("*") {
            self.expression()?;
            return Ok(Node::Starred);
        }
        self.expression()
    }

    fn expression(&mut self) -> Result<Node, Failure> {
        if self.at_operator("-") || self.at_operator("+") {
            let negative: bool = self.at_operator("-");
            self.index += 1;
            return Ok(Node::Unary {
                negative,
                operand: Box::new(self.expression()?),
            });
        }
        let mut node: Node = self.atom()?;
        while self.eat("(") {
            self.arguments()?;
            self.close(")")?;
            node = Node::Call;
        }
        Ok(node)
    }

    fn close(&mut self, operator: &str) -> Result<(), Failure> {
        if self.eat(operator) {
            return Ok(());
        }
        if self.at_end() {
            return Err(syntax(&format!(
                "a bracket is not closed with '{operator}'"
            )));
        }
        Err(self.unexpected())
    }

    fn atom(&mut self) -> Result<Node, Failure> {
        let node: Node = match self.peek().clone() {
            Token::Str(_) => return Ok(self.strings()),
            Token::Number(number) => Node::Number(number),
            Token::Name(name) => Node::Name(name),
            Token::Nested(call) => Node::Nested(call),
            Token::Operator("...") => Node::Ellipsis,
            Token::Operator("(") => return self.parenthesized(),
            Token::Operator("[") => {
                self.index += 1;
                return Ok(Node::List(self.items("]")?));
            }
            Token::Operator("{") => return self.braced(),
            Token::Operator(_) => return Err(syntax("a value is missing here")),
            _ => return Err(self.unexpected()),
        };
        self.index += 1;
        Ok(node)
    }

    /// Adjacent string pieces concatenate into one constant, as Python's parser joins them.
    fn strings(&mut self) -> Node {
        let mut value: Vec<u32> = Vec::new();
        let mut bytes: bool = false;
        let mut formatted: bool = false;
        while let Token::Str(piece) = self.peek() {
            value.extend(&piece.value);
            bytes |= piece.bytes;
            formatted |= piece.formatted;
            self.index += 1;
        }
        Node::Str {
            value,
            bytes,
            formatted,
        }
    }

    fn parenthesized(&mut self) -> Result<Node, Failure> {
        self.index += 1;
        if self.eat(")") {
            return Ok(Node::Tuple(Vec::new()));
        }
        let first: Node = self.starred_or_expression()?;
        if !self.eat(",") {
            self.close(")")?;
            return Ok(first);
        }
        let mut items: Vec<Node> = vec![first];
        items.extend(self.items(")")?);
        Ok(Node::Tuple(items))
    }

    /// Comma-separated items up to `close`, which is consumed; a trailing comma is allowed.
    fn items(&mut self, close: &str) -> Result<Vec<Node>, Failure> {
        let mut items: Vec<Node> = Vec::new();
        while !self.at_operator(close) && !self.at_end() {
            items.push(self.starred_or_expression()?);
            if !self.eat(",") {
                break;
            }
        }
        self.close(close)?;
        Ok(items)
    }

    fn braced(&mut self) -> Result<Node, Failure> {
        self.index += 1;
        if self.eat("}") {
            return Ok(Node::Dict(Vec::new()));
        }
        if self.at_operator("**") {
            return Ok(Node::Dict(self.pairs()?));
        }
        let first: Node = self.starred_or_expression()?;
        if !self.eat(":") {
            if self.eat(",") {
                self.items("}")?;
            } else {
                self.close("}")?;
            }
            return Ok(Node::Set);
        }
        let value: Node = self.expression()?;
        let mut pairs: Vec<(Option<Node>, Node)> = vec![(Some(first), value)];
        if self.eat(",") {
            pairs.extend(self.pairs()?);
        } else {
            self.close("}")?;
        }
        Ok(Node::Dict(pairs))
    }

    fn pairs(&mut self) -> Result<Vec<(Option<Node>, Node)>, Failure> {
        let mut pairs: Vec<(Option<Node>, Node)> = Vec::new();
        while !self.at_operator("}") && !self.at_end() {
            if self.eat("**") {
                pairs.push((None, self.expression()?));
            } else {
                let key: Node = self.expression()?;
                if !self.eat(":") {
                    return Err(self.unexpected());
                }
                pairs.push((Some(key), self.expression()?));
            }
            if !self.eat(",") {
                break;
            }
        }
        self.close("}")?;
        Ok(pairs)
    }
}
