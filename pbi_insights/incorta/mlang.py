"""A parser for the Power Query M language (the subset report queries use).

Produces a small AST of tuples:
  ("let", [(name, expr)], body)        ("each", body)              ("fn", [params], body)
  ("call", func, [args])               ("id", name)                ("str", text) ("num", value)
  ("null",) ("bool", value)            ("list", [items])           ("record", [(name, expr)])
  ("field", target|None, name, opt)    ("item", target, selector, opt)
  ("if", cond, then, else)             ("try", expr, otherwise|None)
  ("bin", op, left, right)             ("un", op, expr)            ("type", text) ("error", expr)
  ("coalesce", left, right)            ("proj", target|None, [names], opt)   ("range", first, last) inside a list
  ("trycatch", expr, fn)  only when the catch function uses the error record; else try ... otherwise
`[Col]` inside `each` is ("field", None, "Col", False); `_` is ("id", "_").
"""
from __future__ import annotations

import re

KEYWORDS = {"let", "in", "each", "if", "then", "else", "try", "otherwise", "and", "or", "not", "type", "null",
            "true", "false", "meta", "as", "is", "error", "section", "shared", "nullable", "optional"}
ESCAPES = {"lf": "\n", "cr": "\r", "tab": "\t", "(": "(", "#": "#"}


class MSyntaxError(ValueError):
    pass


def place(src: str, i: int) -> str:
    """Where a problem is, for a person: line number and the text around it."""
    line = src.count("\n", 0, i) + 1
    start, end = src.rfind("\n", 0, i) + 1, src.find("\n", i)
    end = len(src) if end < 0 else end
    near = src[max(start, i - 20):min(end, i + 20)].strip()
    return f"line {line}" + (f", near: {near}" if near else "")


def tokenize(src: str) -> tuple[list[tuple], list[tuple[int, int]]]:
    """The tokens, and the (start, end) position of each one in the source."""
    toks, spans, i, n, start = [], [], 0, len(src), 0
    while i < n:
        if len(toks) > len(spans):
            spans.append((start, i))
        start = i
        c = src[i]
        if c in " \t\r\n﻿ ":
            i += 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise MSyntaxError(f"comment /* without a closing */ ({place(src, i)})")
            i = j + 2
        elif c == '"' or src.startswith('#"', i):
            quoted = c == "#"
            j = i + (2 if quoted else 1)
            buf = []
            while j < n:
                if src[j] == '"':
                    if src[j + 1:j + 2] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                if src.startswith("#(", j):
                    k = src.find(")", j)
                    if k < 0:
                        raise MSyntaxError(f"escape #( without a closing ) ({place(src, j)})")
                    for code in src[j + 2:k].split(","):           # one or more: #(cr,lf)
                        code = code.strip()
                        if code.lower() in ESCAPES:
                            buf.append(ESCAPES[code.lower()])
                        elif re.fullmatch(r"[0-9A-Fa-f]{4}|[0-9A-Fa-f]{8}", code) and int(code, 16) <= 0x10FFFF:
                            buf.append(chr(int(code, 16)))
                        else:
                            raise MSyntaxError(f"unknown text escape #({code}) ({place(src, j)})")
                    j = k + 1
                    continue
                buf.append(src[j])
                j += 1
            if j >= n:
                raise MSyntaxError(f"text without a closing quote ({place(src, i)})")
            toks.append(("qid" if quoted else "str", "".join(buf)))
            i = j + 1
        elif c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit() and toks[-1:] != [("op", "..")]):
            m = re.match(r"0[xX][0-9a-fA-F]+|(\d+(?:\.\d+)?|\.\d+)([eE][+-]?\d+)?", src[i:])
            text = m.group(0)
            toks.append(("num", int(text, 16) if text.lower().startswith("0x") else
                         (float(text) if any(ch in text for ch in ".eE") else int(text))))
            i += len(text)
        elif c.isalpha() or c == "_" or c == "#":
            m = re.match(r"#?[^\W\d][\w]*(?:\.[\w]+)*", src[i:])
            if not m:
                raise MSyntaxError(f"unexpected character {c!r} ({place(src, i)})")
            word = m.group(0)
            toks.append(("kw" if word in KEYWORDS else "id", word))
            i += len(word)
        else:
            for op in ("=>", "<=", ">=", "<>", "...", "..", "??"):
                if src.startswith(op, i):
                    toks.append(("op", op))
                    i += len(op)
                    break
            else:
                if c in "=<>&+-*/(){}[],?@!;.":       # a lone "." only occurs inside a name: [No. of Units]
                    toks.append(("op", c))
                    i += 1
                else:
                    raise MSyntaxError(f"unexpected character {c!r} ({place(src, i)})")
    if len(toks) > len(spans):
        spans.append((start, i))
    toks.append(("eof", None))
    spans.append((n, n))
    return toks, spans


class Parser:
    def __init__(self, src: str):
        self.src = src
        self.t, self.spans = tokenize(src)
        self.i = 0

    # ---- helpers
    def peek(self, k=0):
        return self.t[min(self.i + k, len(self.t) - 1)]

    def take(self):
        tok = self.t[self.i]
        self.i += 1
        return tok

    def at(self, kind, value=None, k=0) -> bool:
        tok = self.peek(k)
        return tok[0] == kind and (value is None or tok[1] == value)

    def expect(self, kind, value=None):
        if not self.at(kind, value):
            raise MSyntaxError(f"expected {value or kind}, got {self.peek()[1]!r}")
        return self.take()

    # ---- grammar
    def parse(self):
        try:
            e = self.expr()
            if not self.at("eof"):
                raise MSyntaxError(f"unexpected {self.peek()[1]!r} after the expression")
            return e
        except RecursionError:
            raise MSyntaxError("the expression is nested too deeply") from None
        except MSyntaxError as ex:
            at = self.spans[min(self.i, len(self.spans) - 1)][0]
            raise MSyntaxError(f"{ex} ({place(self.src, at)})") from None

    def expr(self):
        if self.at("kw", "let"):
            self.take()
            binds = []
            while True:
                name = self.name()
                self.expect("op", "=")
                binds.append((name, self.expr()))
                if self.at("op", ","):
                    self.take()
                    continue
                break
            self.expect("kw", "in")
            return ("let", binds, self.expr())
        if self.at("kw", "each"):
            self.take()
            return ("each", self.expr())
        if self.at("kw", "if"):
            self.take()
            c = self.expr()
            self.expect("kw", "then")
            a = self.expr()
            self.expect("kw", "else")
            return ("if", c, a, self.expr())
        if self.at("kw", "try"):
            self.take()
            e = self.expr()
            if self.at("kw", "otherwise"):
                self.take()
                return ("try", e, self.expr())
            if self.at("id", "catch"):
                self.take()
                fn = self.expr()
                if fn[0] != "fn":
                    raise MSyntaxError("try ... catch without a function")
                return ("trycatch", e, fn) if any(_uses(fn[2], x) for x in fn[1]) else ("try", e, fn[2])
            return ("try", e, None)
        if self.at("kw", "error"):
            self.take()
            return ("error", self.expr())
        if self.is_lambda():
            return self.lambda_()
        left = self.logical_or()
        while self.at("op", "??"):
            self.take()
            left = ("coalesce", left, self.logical_or())
        return left

    def gen_name(self) -> str:
        """Generalized identifier of a field access or record field: [RECV DATE], [Rate 0.10], [No. of Units].
        The name is the source text as written: joining tokens would change its spaces, dots and digits."""
        if self.at("qid") or self.at("str"):
            return self.take()[1]
        first = self.i
        while self.peek()[0] in ("id", "kw", "num") or any(self.at("op", o) for o in ("-", "/", ".")):
            self.take()
        if self.i == first:
            raise MSyntaxError(f"expected a field name, got {self.peek()[1]!r}")
        name = self.src[self.spans[first][0]:self.spans[self.i - 1][1]]
        if re.search(r"[\r\n]|//|/\*", name):
            raise MSyntaxError(f"line break or comment inside the field name {name!r}")
        return name

    def bracket_is_record(self) -> bool:
        """At '[': a record has '=' at depth 0 before the closing ']'."""
        depth, k = 0, 0
        while True:
            tok = self.peek(k)
            if tok[0] == "eof":
                return False
            if tok[0] == "op" and tok[1] in "[({":
                depth += 1
            elif tok[0] == "op" and tok[1] in "])}":
                depth -= 1
                if depth == 0:
                    return False
            elif depth == 1 and tok == ("op", "="):
                return True
            k += 1

    def name(self) -> str:
        tok = self.take()
        if tok[0] in ("id", "qid") or (tok[0] == "kw" and tok[1] not in ("in",)):
            return tok[1]
        raise MSyntaxError(f"expected a name, got {tok[1]!r}")

    def is_lambda(self) -> bool:
        if not self.at("op", "("):
            return False
        depth, k = 0, 0
        while True:
            tok = self.peek(k)
            if tok[0] == "eof":
                return False
            if tok == ("op", "("):
                depth += 1
            elif tok == ("op", ")"):
                depth -= 1
                if depth == 0:
                    nxt = self.peek(k + 1)
                    if nxt == ("op", "=>"):
                        return True
                    if nxt == ("kw", "as"):
                        return True
                    return False
            k += 1

    def lambda_(self):
        self.expect("op", "(")
        params = []
        while not self.at("op", ")"):
            if self.at("kw", "optional"):
                self.take()
            params.append(self.name())
            if self.at("kw", "as"):
                self.take()
                self.type_text()
            if self.at("op", ","):
                self.take()
        self.expect("op", ")")
        if self.at("kw", "as"):
            self.take()
            self.type_text()
        self.expect("op", "=>")
        return ("fn", params, self.expr())

    def logical_or(self):
        left = self.logical_and()
        while self.at("kw", "or"):
            self.take()
            left = ("bin", "or", left, self.logical_and())
        return left

    def logical_and(self):
        left = self.is_as()
        while self.at("kw", "and"):
            self.take()
            left = ("bin", "and", left, self.is_as())
        return left

    def is_as(self):
        left = self.equality()
        while self.at("kw", "is") or self.at("kw", "as"):
            op = self.take()[1]
            left = ("bin", op, left, ("type", self.type_text()))
        return left

    def equality(self):
        left = self.relational()
        while self.at("op", "=") or self.at("op", "<>"):
            op = self.take()[1]
            left = ("bin", op, left, self.relational())
        return left

    def relational(self):
        left = self.additive()
        while any(self.at("op", o) for o in ("<", ">", "<=", ">=")):
            op = self.take()[1]
            left = ("bin", op, left, self.additive())
        return left

    def additive(self):
        left = self.multiplicative()
        while any(self.at("op", o) for o in ("+", "-", "&")):
            op = self.take()[1]
            left = ("bin", op, left, self.multiplicative())
        return left

    def multiplicative(self):
        left = self.unary()
        while self.at("op", "*") or self.at("op", "/"):
            op = self.take()[1]
            left = ("bin", op, left, self.unary())
        return left

    def unary(self):
        if self.at("op", "-") or self.at("op", "+"):
            op = self.take()[1]
            return ("un", op, self.unary())
        if self.at("kw", "not"):
            self.take()
            return ("un", "not", self.unary())
        if self.at("kw", "type"):
            self.take()
            return ("type", self.type_text())
        e = self.postfix()
        while self.at("kw", "meta"):
            self.take()
            self.postfix()
        return e

    def type_text(self) -> str:
        parts = []
        if self.at("kw", "nullable"):
            parts.append(self.take()[1])
        if self.at("op", "[") or self.at("op", "{"):
            parts.append(self.skip_group())
        elif self.at("id", "function"):
            parts.append(self.take()[1])
            if self.at("op", "("):
                parts.append(self.skip_group())
                if self.at("kw", "as"):
                    self.take()
                    parts.append("as " + self.type_text())
        elif self.at("id", "table") or self.at("id", "list") or self.at("id", "record"):
            parts.append(self.take()[1])
            if self.at("op", "[") or self.at("op", "{") or self.at("op", "("):
                parts.append(self.skip_group())
        else:
            tok = self.take()
            parts.append(str(tok[1]))
        return " ".join(parts)

    def skip_group(self) -> str:
        """The source text of a bracketed group, brackets included."""
        begin = self.spans[self.i][0]
        open_, close = self.take()[1], None
        close = {"[": "]", "{": "}", "(": ")"}[open_]
        depth = 1
        while depth:
            tok = self.take()
            if tok[0] == "eof":
                raise MSyntaxError("unclosed type")
            if tok == ("op", open_):
                depth += 1
            elif tok == ("op", close):
                depth -= 1
        return self.src[begin:self.spans[self.i - 1][1]]

    def projection(self):
        """[[a], [b]] -> (names, optional)."""
        self.expect("op", "[")
        names = []
        while True:
            self.expect("op", "[")
            names.append(self.gen_name())
            self.expect("op", "]")
            if not self.at("op", ","):
                break
            self.take()
        self.expect("op", "]")
        opt = self.at("op", "?")
        if opt:
            self.take()
        return names, opt

    def postfix(self):
        e = self.primary()
        while True:
            if self.at("op", "("):
                self.take()
                args = []
                while not self.at("op", ")"):
                    args.append(self.expr())
                    if self.at("op", ","):
                        self.take()
                self.expect("op", ")")
                e = ("call", e, args)
            elif self.at("op", "{"):
                self.take()
                sel = self.expr()
                self.expect("op", "}")
                opt = False
                if self.at("op", "?"):
                    self.take()
                    opt = True
                e = ("item", e, sel, opt)
            elif self.at("op", "["):
                if self.at("op", "[", 1):
                    e = ("proj", e, *self.projection())
                    continue
                self.take()
                name = self.gen_name()
                self.expect("op", "]")
                opt = False
                if self.at("op", "?"):
                    self.take()
                    opt = True
                e = ("field", e, name, opt)
            else:
                return e

    def primary(self):
        tok = self.peek()
        if tok[0] == "num":
            self.take()
            return ("num", tok[1])
        if tok[0] == "str":
            self.take()
            return ("str", tok[1])
        if tok[0] == "qid":
            self.take()
            return ("id", tok[1])
        if tok[0] == "id":
            self.take()
            return ("id", tok[1])
        if tok[0] == "kw":
            if tok[1] == "null":
                self.take()
                return ("null",)
            if tok[1] in ("true", "false"):
                self.take()
                return ("bool", tok[1] == "true")
            if tok[1] in ("let", "each", "if", "try", "error"):
                return self.expr()
            if tok[1] == "type":
                self.take()
                return ("type", self.type_text())
        if tok == ("op", "("):
            self.take()
            e = self.expr()
            self.expect("op", ")")
            return e
        if tok == ("op", "{"):
            self.take()
            items = []
            while not self.at("op", "}"):
                item = self.expr()
                if self.at("op", ".."):
                    self.take()
                    item = ("range", item, self.expr())
                items.append(item)
                if self.at("op", ","):
                    self.take()
            self.expect("op", "}")
            return ("list", items)
        if tok == ("op", "..."):
            self.take()
            return ("error", ("str", "not implemented (...)"))
        if tok == ("op", "[") and self.at("op", "[", 1):
            return ("proj", None, *self.projection())
        if tok == ("op", "["):
            self.take()
            if self.at("op", "]"):
                self.take()
                return ("record", [])
            # [Col] alone (implicit _) vs record [a = 1, b = 2]
            self.i -= 1
            is_record = self.bracket_is_record()
            self.take()
            if not is_record:
                name = self.gen_name()
                self.expect("op", "]")
                opt = False
                if self.at("op", "?"):
                    self.take()
                    opt = True
                return ("field", None, name, opt)
            fields = []
            while not self.at("op", "]"):
                name = self.gen_name()
                self.expect("op", "=")
                fields.append((name, self.expr()))
                if self.at("op", ","):
                    self.take()
            self.expect("op", "]")
            return ("record", fields)
        if tok == ("op", "@"):
            self.take()
            return self.primary()
        raise MSyntaxError(f"unexpected {tok[1]!r}")


def _uses(node, name: str) -> bool:
    if isinstance(node, (tuple, list)):
        return tuple(node[:2]) == ("id", name) or any(_uses(x, name) for x in node)
    return False


def table_type_columns(text: str):
    """The (name, type) pairs of a table type's text, "table [A = text, B = Int64.Type]"; None when it lists none."""
    m = re.match(r"\s*table\s*(\[.*\])\s*$", text or "", re.S)
    if not m:
        return None
    p = Parser(m.group(1))
    p.expect("op", "[")
    out = []
    while not p.at("op", "]"):
        name = p.gen_name()
        if p.at("op", "="):
            p.take()
            out.append((name, p.type_text()))
        else:
            out.append((name, "any"))
        if p.at("op", ","):
            p.take()
    return out


def parse_m(src: str):
    return Parser(src or "").parse()
