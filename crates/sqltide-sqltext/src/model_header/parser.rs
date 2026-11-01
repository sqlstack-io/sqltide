//! Build the ordered header map from tokens.

use super::MAX_HEADER_NESTING_DEPTH;
use super::tokenizer::{Token, TokenKind};
use super::value::{HeaderValue, insert_entry, word_value};

const COMMA: &str = ",";
const KEY_VALUE_SEPARATOR: &str = ":";
const OPEN_PAREN: &str = "(";
const CLOSE_PAREN: &str = ")";
const OPEN_BRACKET: &str = "[";
const CLOSE_BRACKET: &str = "]";
const OPEN_BRACE: &str = "{";
const CLOSE_BRACE: &str = "}";

pub(super) fn parse(
    tokens: &[Token],
    declaration: bool,
) -> Result<Vec<(String, HeaderValue)>, String> {
    let mut parser = Parser {
        tokens,
        index: 0,
        depth: 0,
        declaration,
    };
    if parser.peek().kind == TokenKind::End {
        return Ok(Vec::new());
    }
    let values = parser.parse_map(None)?;
    parser.expect_end()?;
    Ok(values)
}

struct Parser<'a> {
    tokens: &'a [Token],
    index: usize,
    depth: usize,
    declaration: bool,
}

impl Parser<'_> {
    fn parse_map(
        &mut self,
        end_symbol: Option<&str>,
    ) -> Result<Vec<(String, HeaderValue)>, String> {
        let mut values: Vec<(String, HeaderValue)> = Vec::new();
        while !self.is_at_end_symbol(end_symbol) {
            if self.match_symbol(COMMA) {
                continue;
            }
            let key: String = self.consume_key()?;
            if self.match_symbol(KEY_VALUE_SEPARATOR) {
                return Err(format!(
                    "unexpected ':' after key '{key}'; use SQLBuild syntax '{key} value'"
                ));
            }
            if self.is_at_end_symbol(end_symbol) || self.peek().kind == TokenKind::End {
                return Err(format!(
                    "unexpected token '{key}' without a value; quote values with spaces"
                ));
            }
            let value: HeaderValue = self.parse_value()?;
            insert_entry(&mut values, key, value);
            self.match_symbol(COMMA);
        }
        if let Some(symbol) = end_symbol {
            self.consume_symbol(symbol)?;
        }
        Ok(values)
    }

    fn parse_value(&mut self) -> Result<HeaderValue, String> {
        let (kind, position, text) = {
            let token: &Token = self.peek();
            (token.kind, token.position, token.value.clone())
        };
        match kind {
            TokenKind::Text => {
                self.index += 1;
                Ok(HeaderValue::Text(text))
            }
            TokenKind::Word => {
                let word: String = text;
                self.index += 1;
                if self.peek_is_symbol(OPEN_PAREN) {
                    self.index += 1;
                    let entries = self.nested(|parser| parser.parse_map(Some(CLOSE_PAREN)))?;
                    return Ok(HeaderValue::Map(vec![(word, HeaderValue::Map(entries))]));
                }
                Ok(word_value(&word, self.declaration))
            }
            _ if self.match_symbol(OPEN_BRACKET) => self
                .nested(|parser| parser.parse_list(CLOSE_BRACKET))
                .map(HeaderValue::List),
            _ if self.match_symbol(OPEN_BRACE) => self
                .nested(|parser| parser.parse_list(CLOSE_BRACE))
                .map(HeaderValue::Set),
            _ if self.match_symbol(OPEN_PAREN) => self
                .nested(|parser| parser.parse_map(Some(CLOSE_PAREN)))
                .map(HeaderValue::Map),
            _ => Err(format!("expected value at position {position}")),
        }
    }

    fn parse_list(&mut self, close: &str) -> Result<Vec<HeaderValue>, String> {
        let mut values: Vec<HeaderValue> = Vec::new();
        while !self.is_at_end_symbol(Some(close)) {
            if self.match_symbol(COMMA) {
                continue;
            }
            values.push(self.parse_value()?);
            self.match_symbol(COMMA);
        }
        self.consume_symbol(close)?;
        Ok(values)
    }

    fn nested<T>(
        &mut self,
        parse: impl FnOnce(&mut Self) -> Result<T, String>,
    ) -> Result<T, String> {
        if self.depth >= MAX_HEADER_NESTING_DEPTH {
            return Err(format!(
                "nesting deeper than {MAX_HEADER_NESTING_DEPTH} levels at position {}",
                self.peek().position
            ));
        }
        self.depth += 1;
        let result = parse(self);
        self.depth -= 1;
        result
    }

    fn consume_key(&mut self) -> Result<String, String> {
        let token: &Token = self.peek();
        if token.kind != TokenKind::Word {
            return Err(format!("expected key at position {}", token.position));
        }
        let key: String = token.value.clone();
        self.index += 1;
        Ok(key)
    }

    fn is_at_end_symbol(&self, symbol: Option<&str>) -> bool {
        match symbol {
            None => self.peek().kind == TokenKind::End,
            Some(symbol) => self.peek_is_symbol(symbol),
        }
    }

    fn peek_is_symbol(&self, symbol: &str) -> bool {
        let token: &Token = self.peek();
        token.kind == TokenKind::Symbol && token.value == symbol
    }

    fn match_symbol(&mut self, symbol: &str) -> bool {
        if self.peek_is_symbol(symbol) {
            self.index += 1;
            return true;
        }
        false
    }

    fn consume_symbol(&mut self, symbol: &str) -> Result<(), String> {
        if self.match_symbol(symbol) {
            return Ok(());
        }
        Err(format!(
            "expected '{symbol}' at position {}",
            self.peek().position
        ))
    }

    fn expect_end(&self) -> Result<(), String> {
        let token: &Token = self.peek();
        if token.kind != TokenKind::End {
            return Err(format!(
                "unexpected token '{}' at position {}",
                token.value, token.position
            ));
        }
        Ok(())
    }

    fn peek(&self) -> &Token {
        &self.tokens[self.index]
    }
}
