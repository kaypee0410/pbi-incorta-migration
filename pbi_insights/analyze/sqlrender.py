"""Render IR nodes as sqlglot expressions, so one translation prints in any SQL dialect."""
from __future__ import annotations

import re

from sqlglot import exp

from .ir import (NESTED_TOTAL, WINDOW_AGG, Agg, BlankArith, Bin, Case, Col, CorrAgg, Fn, InList, IsNull, Lit, Lookup,
                 Null, OuterCol, Rank, Review, SafeDiv, Scalar, Star, TimeShift, Total, Un, removal_text, sql_gaps,
                 windowable_total)

RESERVED = {
    "ADD", "ALL", "ALTER", "AND", "ANY", "AS", "ASC", "BETWEEN", "BY", "CASE", "CAST", "CHECK", "COLUMN",
    "CONSTRAINT", "CREATE", "CROSS", "CURRENT", "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP",
    "CURRENT_USER", "DATABASE", "DATE", "DEFAULT", "DELETE", "DESC", "DISTINCT", "DROP", "ELSE", "END", "ESCAPE",
    "EXCEPT", "EXISTS", "FALSE", "FETCH", "FILE", "FOR", "FOREIGN", "FROM", "FULL", "FUNCTION", "GRANT", "GROUP",
    "HAVING", "IDENTITY", "IF", "IN", "INDEX", "INNER", "INSERT", "INTERSECT", "INTERVAL", "INTO", "IS", "JOIN",
    "KEY", "LEFT", "LIKE", "LIMIT", "NATURAL", "NOT", "NULL", "OF", "OFFSET", "ON", "OR", "ORDER", "OUTER", "OVER",
    "PERCENT", "PIVOT", "PLAN", "PRIMARY", "PROCEDURE", "PUBLIC", "REFERENCES", "RIGHT", "ROW", "ROWS", "SCHEMA",
    "SELECT", "SESSION_USER", "SET", "SOME", "TABLE", "TABLESAMPLE", "THEN", "TIME", "TIMESTAMP", "TO", "TOP",
    "TRUE", "UNION", "UNIQUE", "UNPIVOT", "UPDATE", "USER", "USING", "VALUES", "VIEW", "WHEN", "WHERE", "WINDOW",
    "WITH"}
_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PREC = {"OR": 1, "AND": 2, "NOT": 3, "=": 4, "<>": 4, "<": 4, ">": 4, "<=": 4, ">=": 4, "+": 5, "-": 5,
        "*": 6, "/": 6}
BIN = {"+": exp.Add, "-": exp.Sub, "*": exp.Mul, "/": exp.Div, "=": exp.EQ, "<>": exp.NEQ, "<": exp.LT,
       ">": exp.GT, "<=": exp.LTE, ">=": exp.GTE, "AND": exp.And, "OR": exp.Or}
AGG_CLS = {"SUM": exp.Sum, "AVG": exp.Avg, "MIN": exp.Min, "MAX": exp.Max, "MEDIAN": exp.Median}


def snake(name: str) -> str:
    s = str(name).replace("%", " pct ").replace("$", " usd ").replace("#", " num ").replace("/", " per ")
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s if s and not s[0].isdigit() else f"c_{s}"


def needs_quote(name: str) -> bool:
    return not _SAFE.match(name or "") or name.upper() in RESERVED


def ident(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=needs_quote(name))


def column(name: str, table: str | None = None) -> exp.Column:
    return exp.Column(this=ident(name), table=ident(table) if table else None)


def table_ref(name: str, alias: str | None = None) -> exp.Table:
    parts = name.split(".")
    t = exp.Table(this=ident(parts[-1]), db=ident(parts[-2]) if len(parts) > 1 else None)
    if alias:
        t.set("alias", exp.TableAlias(this=ident(alias)))
    return t


def literal(v) -> exp.Expression:
    if v is None:
        return exp.Null()
    if isinstance(v, bool):
        return exp.Boolean(this=v)
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return exp.Literal.number(v)
    return exp.Literal.string(str(v))


def not_null(e: exp.Expression) -> exp.Expression:
    return exp.Not(this=exp.Is(this=e, expression=exp.Null()))


def _prec(node) -> int:
    if isinstance(node, Bin):
        return PREC.get(node.op, 9)
    if isinstance(node, BlankArith):
        return 5
    if isinstance(node, SafeDiv):
        return 6
    if isinstance(node, Un):
        return 3 if node.op == "NOT" else 8
    return 9


class Renderer:
    """IR -> sqlglot. `col_fn(Col)` places columns; `agg_fn(Agg)` may redirect an aggregate to a
    pre-computed column (multi-grain queries); `scalar_fn(Scalar)` builds filter-independent subqueries."""

    def __init__(self, dialect: str, col_fn, agg_fn=None, scalar_fn=None, lookup_fn=None, corr_fn=None, ctx_fn=None,
                 total_fn=None):
        self.dialect = dialect
        self.col_fn = col_fn
        self.agg_fn = agg_fn
        self.scalar_fn = scalar_fn
        self.lookup_fn = lookup_fn      # Lookup -> expression (scalar subquery on the lookup table)
        self.corr_fn = corr_fn          # CorrAgg over another table -> expression (grouped subquery)
        self.ctx_fn = ctx_fn            # value of the visual used inside an aggregate -> pre-computed column
        self.total_fn = total_fn        # Total -> PARTITION BY expressions of its window (None: generic comment)
        self._inside = False            # rendering the argument / filter of an aggregate
        self._window = None             # (partition expressions, comment) while rendering a windowed total

    def _child(self, node, parent_prec: int, right: bool = False, strict_ops=("-", "/")) -> exp.Expression:
        e = self.r(node)
        p = _prec(node)
        if p < parent_prec or (right and p == parent_prec):
            return exp.Paren(this=e)
        return e

    def r(self, n) -> exp.Expression:
        if isinstance(n, Col):
            return self.col_fn(n)
        if isinstance(n, Lit):
            return literal(n.value)
        if isinstance(n, Null):
            return exp.Null()
        if isinstance(n, Star):
            return exp.Star()
        if isinstance(n, Agg):
            return self.agg(n)
        if isinstance(n, Bin):
            p = PREC.get(n.op, 9)
            return BIN[n.op](this=self._child(n.left, p), expression=self._child(n.right, p, right=n.op in ("-", "/")))
        if isinstance(n, Un):
            if n.op == "NOT":
                return exp.Not(this=self._child(n.x, 4))
            return exp.Neg(this=self._child(n.x, 8))
        if isinstance(n, Case):
            return exp.Case(ifs=[exp.If(this=self.r(c), true=self.r(v)) for c, v in n.whens],
                            default=None if n.default is None or isinstance(n.default, Null) else self.r(n.default))
        if isinstance(n, SafeDiv):
            num = self._child(n.num, 6)
            den = self.r(n.den)
            if n.alt is not None:
                return exp.Case(ifs=[exp.If(this=exp.Or(this=exp.EQ(this=den, expression=literal(0)),
                                                        expression=exp.Is(this=den.copy(), expression=exp.Null())),
                                            true=self.r(n.alt))],
                                default=exp.Div(this=num, expression=exp.Paren(this=den.copy()) if _prec(n.den) < 7 else den.copy()))
            return exp.Div(this=num, expression=exp.Nullif(this=den, expression=literal(0)))
        if isinstance(n, BlankArith):
            cls = exp.Add if n.op == "+" else exp.Sub
            return cls(this=self._coalesce0(n.left), expression=self._coalesce0(n.right))
        if isinstance(n, IsNull):
            return exp.Is(this=self._child(n.x, 5), expression=exp.Null())
        if isinstance(n, InList):
            return exp.In(this=self.r(n.x), expressions=[self.r(v) for v in n.values])
        if isinstance(n, Fn):
            return self.fn(n)
        if self._inside and self.ctx_fn and (isinstance(n, Scalar) or (isinstance(n, Total) and n.independent)):
            return self.ctx_fn(n)
        if isinstance(n, Total) and n.independent and self.scalar_fn:
            return self.scalar_fn(Scalar(n.expr, n.independent))
        if isinstance(n, Scalar):
            if self.scalar_fn:
                return self.scalar_fn(n)
            e = exp.Null()
            e.add_comments([" REVIEW: value that ignores filters "])
            return e
        if isinstance(n, Review):
            e = exp.Null()
            e.add_comments([f" REVIEW: {n.reason} "])
            return e
        if isinstance(n, CorrAgg):
            return self.corr_agg(n)
        if isinstance(n, Lookup):
            if self.lookup_fn:
                return self.lookup_fn(n, self)
            return self.review_null(f"lookup in {n.result.table}: join it in")
        if isinstance(n, OuterCol):
            return self.col_fn(Col(n.table, n.column))
        if windowable_total(n) and self._window is None and not self._inside:
            return self.window_total(n)
        if isinstance(n, (TimeShift, Rank, Total)):
            gaps = sql_gaps(n)
            return self.review_null(gaps[0] if gaps else NESTED_TOTAL)
        raise TypeError(f"Cannot render {n!r}")

    def window_total(self, n: Total) -> exp.Expression:
        """Total at a coarser grain: each aggregate of `n.expr` becomes SUM / MIN / MAX(<its value per group>) OVER
        (PARTITION BY the visual's dimensions except the removed ones). Without `total_fn` the partition is not known
        (one measure on its own), so the window is OVER () with a comment naming what the partition leaves out."""
        if self.total_fn:
            part, comment = self.total_fn(n), None
        else:
            removed = removal_text(n)
            what = (f"the visual's own grouping by {removed} (slicers stay)" if n.kind == "ALLSELECTED"
                    else f"the visual's dimensions except {removed}")
            part, comment = [], f" PARTITION BY {what} "
        self._window = (part, comment)
        try:
            return self.r(n.expr)
        finally:
            self._window = None

    def _windowed(self, a: Agg, value: exp.Expression) -> exp.Expression:
        part, comment = self._window
        outer = {"SUM": exp.Sum, "MIN": exp.Min, "MAX": exp.Max}[WINDOW_AGG[a.func]]
        w = exp.Window(this=outer(this=value), partition_by=[p.copy() for p in part] or None)
        if comment:
            w.add_comments([comment])
        return w

    @staticmethod
    def review_null(reason: str) -> exp.Expression:
        e = exp.Null()
        e.add_comments([f" REVIEW: {reason} "])
        return e

    def corr_agg(self, n: CorrAgg) -> exp.Expression:
        """Per-row aggregate of a calculated column: a window over the same table, else a subquery."""
        if not n.windowable:
            if self.corr_fn and not n.complex:
                return self.corr_fn(n, self)
            return self.review_null(sql_gaps(n)[0] if sql_gaps(n) else "per-row aggregate")
        part = [self.r(i) for i, _ in n.keys]
        if isinstance(n.arg, Star):
            x = literal(1) if n.inner is not None else exp.Star()
        else:
            x = self.r(n.arg)
        if n.inner is not None:
            x = exp.Case(ifs=[exp.If(this=self.r(n.inner), true=x)])
        if n.func == "MEDIAN":
            return median_window(x, part, self.dialect)
        if n.func == "COUNT":
            agg = exp.Count(this=exp.Distinct(expressions=[x]) if n.distinct else x)
        else:
            agg = AGG_CLS[n.func](this=x)
        return exp.Window(this=agg, partition_by=part or None)

    def _coalesce0(self, n) -> exp.Expression:
        if isinstance(n, Lit):
            return self.r(n)
        return exp.Coalesce(this=self.r(n), expressions=[literal(0)])

    def agg(self, a: Agg) -> exp.Expression:
        if self._inside:
            return self.ctx_fn(a) if self.ctx_fn else self.review_null("a value of the visual inside an aggregate")
        if self._window is not None:
            window, self._window = self._window, None      # the aggregate's own value per group, then the window
            try:
                value = self.agg(a)
            finally:
                self._window = window
            return self._windowed(a, value)
        if self.agg_fn:
            res = self.agg_fn(a)
            if res is not None:
                return res
        self._inside = True
        try:
            return self._agg(a)
        finally:
            self._inside = False

    def _agg(self, a: Agg) -> exp.Expression:
        if isinstance(a.arg, Star):
            inner = exp.Star() if a.pred is None else exp.Case(ifs=[exp.If(this=self.r(a.pred), true=literal(1))])
            return exp.Count(this=inner)
        x = self.r(a.arg)
        if a.pred is not None:
            x = exp.Case(ifs=[exp.If(this=self.r(a.pred), true=x)])
        if a.func == "COUNT":
            return exp.Count(this=exp.Distinct(expressions=[x]) if a.distinct else x)
        return AGG_CLS[a.func](this=x)

    def numfmt(self, x: exp.Expression, decimals: int, percent: bool, thousands: bool) -> exp.Expression:
        """FORMAT(x, "0.0%" / "#,0.00" / ...): rounded text with fixed decimals."""
        d = self.dialect
        if percent:
            x = exp.Mul(this=exp.Paren(this=x), expression=literal(100))
        x = exp.Round(this=x, decimals=literal(decimals))
        if thousands:
            if d == "tsql":
                e = exp.Anonymous(this="FORMAT", expressions=[x, literal(f"N{decimals}")])
            elif d in ("spark", "databricks", "hive"):
                e = exp.Anonymous(this="FORMAT_NUMBER", expressions=[x, literal(decimals)])
            elif d == "mysql":
                e = exp.Anonymous(this="FORMAT", expressions=[x, literal(decimals)])
            elif d == "bigquery":
                e = exp.Anonymous(this="FORMAT", expressions=[literal(f"%'.{decimals}f"), x])
            elif d == "sqlite":
                e = exp.Anonymous(this="PRINTF", expressions=[literal(f"%,.{decimals}f"), x])
            else:
                mask = "FM999,999,999,999,990" + ("." + "0" * decimals if decimals else "")
                e = exp.Anonymous(this="TO_CHAR", expressions=[x, literal(mask)])
        elif d == "sqlite":
            e = exp.Case(ifs=[exp.If(this=not_null(x.copy()),
                                     true=exp.Anonymous(this="PRINTF", expressions=[literal(f"%.{decimals}f"), x]))])
        else:
            kind = exp.DataType.build("BIGINT") if not decimals else exp.DataType.build(f"DECIMAL(38, {decimals})")
            e = _text(exp.Cast(this=x, to=kind))
        return exp.Concat(expressions=[e, literal("%")], coalesce=False) if percent else e

    def fn(self, n: Fn) -> exp.Expression:
        a = [self.r(x) for x in n.args]
        name, d = n.name, self.dialect
        if name == "COALESCE":
            return exp.Coalesce(this=a[0], expressions=a[1:])
        if name == "CONCAT":
            # DAX & turns numbers into text; cast them so strict engines accept the concatenation
            a = [exp.Cast(this=x, to=exp.DataType.build("VARCHAR")) if _numeric_ir(arg) else x
                 for arg, x in zip(n.args, a)]
            return exp.Concat(expressions=a, coalesce=True)
        if name in ("YEAR", "MONTH", "DAY"):
            return {"YEAR": exp.Year, "MONTH": exp.Month, "DAY": exp.Day}[name](this=a[0])
        if name == "WEEKDAY":
            ret = n.args[1].value if len(n.args) > 1 and isinstance(n.args[1], Lit) else 1
            base = (exp.Anonymous(this="DATEPART", expressions=[exp.var("WEEKDAY"), a[0]]) if d == "tsql"
                    else exp.Anonymous(this="DAYOFWEEK", expressions=[a[0]]))          # Sunday = 1 ... Saturday = 7
            if ret == 2:      # Monday = 1 ... Sunday = 7
                return exp.Add(this=exp.Paren(this=exp.Mod(this=exp.Paren(this=exp.Add(this=base, expression=literal(5))),
                                                            expression=literal(7))), expression=literal(1))
            if ret == 3:      # Monday = 0 ... Sunday = 6
                return exp.Paren(this=exp.Mod(this=exp.Paren(this=exp.Add(this=base, expression=literal(5))),
                                              expression=literal(7)))
            return base
        if name == "WEEKNUM" and d != "tsql":
            # DAX WEEKNUM: week 1 holds 1 January; weeks start on Sunday (type 1) or Monday (type 2)
            ret = n.args[1].value if len(n.args) > 1 and isinstance(n.args[1], Lit) else 1
            jan1 = exp.DateTrunc(this=a[0].copy(), unit=exp.var("YEAR"))
            dow = exp.Anonymous(this="DAYOFWEEK", expressions=[jan1])                         # Sunday = 1
            offset = (exp.Paren(this=exp.Mod(this=exp.Paren(this=exp.Add(this=dow, expression=literal(5))),
                                             expression=literal(7))) if ret == 2
                      else exp.Paren(this=exp.Sub(this=dow, expression=literal(1))))
            doy = exp.Anonymous(this="DAYOFYEAR", expressions=[a[0]])
            inner = exp.Div(this=exp.Paren(this=exp.Add(this=exp.Sub(this=doy, expression=literal(1)), expression=offset)),
                            expression=literal(7))
            return exp.Add(this=exp.Floor(this=inner), expression=literal(1))
        if name in ("QUARTER", "HOUR", "MINUTE", "SECOND", "WEEKNUM"):
            if d == "tsql":
                part = {"WEEKNUM": "WEEK"}.get(name, name)
                return exp.Anonymous(this="DATEPART", expressions=[exp.var(part), a[0]])
            if name == "QUARTER":
                return exp.Quarter(this=a[0])
            return exp.Anonymous(this={"WEEKNUM": "WEEKOFYEAR"}.get(name, name), expressions=a[:1])
        if name == "TIMEFMT":
            return exp.TimeToStr(this=a[0], format=a[1])
        if name == "TOSTRING":
            return exp.Cast(this=a[0], to=exp.DataType.build("VARCHAR"))
        if name == "NUMFMT":
            return self.numfmt(a[0], n.args[1].value, n.args[2].value, n.args[3].value)
        if name in ("EDATE", "EOMONTH") and len(a) == 2 and d != "sqlite":      # SQLite: the test run adds EDATE / EOMONTH
            if d == "oracle":
                moved = exp.Anonymous(this="ADD_MONTHS", expressions=a)
            else:
                moved = exp.DateAdd(this=a[0], expression=a[1], unit=exp.var("MONTH"))
            return moved if name == "EDATE" else exp.LastDay(this=moved)
        if name == "TONUMBER":
            return exp.Cast(this=a[0], to=exp.DataType.build("DOUBLE"))
        if name == "TIMESTAMP_LIT":
            return exp.Cast(this=a[0], to=exp.DataType.build("TIMESTAMP"))
        if name == "DATE":
            return exp.DateFromParts(year=a[0], month=a[1], day=a[2])
        if name == "CURRENT_DATE":
            return exp.CurrentDate()
        if name == "CURRENT_TIMESTAMP":
            return exp.CurrentTimestamp()
        if name == "DATEDIFF":
            unit = n.args[0].value if isinstance(n.args[0], Lit) else "DAY"
            return exp.DateDiff(this=a[2], expression=a[1], unit=exp.var(str(unit)))
        if name in ("LEFT", "RIGHT"):
            return (exp.Left if name == "LEFT" else exp.Right)(this=a[0], expression=a[1])
        if name == "SUBSTRING":
            return exp.Substring(this=a[0], start=a[1], length=a[2] if len(a) > 2 else None)
        if name == "LENGTH":
            return exp.Length(this=a[0])
        if name in ("UPPER", "LOWER", "TRIM"):
            return {"UPPER": exp.Upper, "LOWER": exp.Lower, "TRIM": exp.Trim}[name](this=a[0])
        if name == "ROUND":
            return exp.Round(this=a[0], decimals=a[1] if len(a) > 1 else None)
        if name in ("ABS", "FLOOR", "CEIL", "SQRT", "EXP", "LN"):
            return {"ABS": exp.Abs, "FLOOR": exp.Floor, "CEIL": exp.Ceil, "SQRT": exp.Sqrt, "EXP": exp.Exp,
                    "LN": exp.Ln}[name](this=a[0])
        if name == "POWER":
            return exp.Pow(this=a[0], expression=a[1])
        if name == "MOD":
            return exp.Mod(this=a[0], expression=a[1])
        if name in ("GREATEST", "LEAST"):
            return (exp.Greatest if name == "GREATEST" else exp.Least)(this=a[0], expressions=a[1:])
        if name.startswith("LIKE_"):
            v = n.args[1]
            pattern = {"LIKE_CONTAINS": "%{}%", "LIKE_STARTS": "{}%", "LIKE_ENDS": "%{}"}[name]
            if isinstance(v, Lit):
                return exp.Like(this=a[0], expression=literal(pattern.format(v.value)))
            pieces = {"LIKE_CONTAINS": [literal("%"), a[1], literal("%")], "LIKE_STARTS": [a[1], literal("%")],
                      "LIKE_ENDS": [literal("%"), a[1]]}[name]
            return exp.Like(this=a[0], expression=exp.Concat(expressions=pieces, coalesce=True))
        return exp.Anonymous(this=name, expressions=a)


def _text(e) -> exp.Expression:
    return exp.Cast(this=e, to=exp.DataType.build("VARCHAR"))


NUMERIC_FNS = {"YEAR", "MONTH", "DAY", "QUARTER", "HOUR", "MINUTE", "SECOND", "WEEKDAY", "WEEKNUM", "LENGTH",
               "ROUND", "FLOOR", "CEIL", "ABS", "POWER", "MOD", "SQRT", "EXP", "LN", "LOG10", "SIGN", "TONUMBER",
               "DATEDIFF"}


def _numeric_ir(node) -> bool:
    if isinstance(node, Lit):
        return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    if isinstance(node, Fn):
        return node.name in NUMERIC_FNS
    if isinstance(node, (BlankArith, SafeDiv)):
        return True
    return isinstance(node, Bin) and node.op in ("+", "-", "*", "/")


def lookup_subquery(view_of):
    """Renderer lookup_fn: LOOKUPVALUE as (SELECT MAX(result) FROM <lookup view> WHERE key = value)."""
    def fn(n, rnd):
        where = None
        for key, value in n.keys:
            cond = exp.EQ(this=column(key.column, "lk"), expression=rnd.r(value))
            where = cond if where is None else exp.And(this=where, expression=cond)
        sub = exp.select(exp.Max(this=column(n.result.column, "lk"))).from_(table_ref(view_of(n.result.table), "lk"))
        if where is not None:
            sub = sub.where(where)
        e = exp.Subquery(this=sub)
        if n.default is not None and not isinstance(n.default, Null):
            e = exp.Coalesce(this=e, expressions=[rnd.r(n.default)])
        return e
    return fn


def median_window(x: exp.Expression, partition: list, dialect: str) -> exp.Expression:
    """MEDIAN(x) OVER (PARTITION BY ...) in the spelling each dialect accepts."""
    part = partition or None
    if dialect in ("spark", "databricks", "hive"):
        return exp.Window(this=exp.Anonymous(this="percentile", expressions=[x, literal(0.5)]), partition_by=part)
    if dialect == "bigquery":
        return exp.Window(this=exp.Anonymous(this="PERCENTILE_CONT", expressions=[x, literal(0.5)]), partition_by=part)
    if dialect in ("tsql", "postgres", "mysql", "sqlite"):
        w = exp.Window(this=exp.WithinGroup(this=exp.PercentileCont(this=literal(0.5)),
                                            expression=exp.Order(expressions=[exp.Ordered(this=x, nulls_first=dialect == "tsql")])),
                       partition_by=part)
        if dialect != "tsql":
            w.add_comments([f" REVIEW: {dialect} has no median window function: use a GROUP BY subquery "])
        return w
    return exp.Window(this=exp.Median(this=x), partition_by=part)


def to_sql(e: exp.Expression, dialect: str, pretty: bool = True) -> str:
    return e.sql(dialect=dialect, pretty=pretty)
