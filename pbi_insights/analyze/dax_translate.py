"""DAX -> IR translator (rule-based).

Covers the patterns most report measures use: aggregations, CALCULATE with boolean / FILTER filters,
DIVIDE and arithmetic with DAX BLANK rules, IF / SWITCH, iterators over a table (SUMX ...),
SELECTEDVALUE / HASONEVALUE, FILTER(ALL(T), ...) as a filter-independent value, and the common scalar
text / date / math functions. Anything else becomes a `Review` node with a reason, so the workbook
can flag it instead of guessing.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace

from .dax_parser import DaxSyntaxError, functions_used, parse_dax
from .ir import (Agg, BlankArith, Bin, Case, Col, CorrAgg, Fn, InList, IsNull, Lit, Lookup, Null, OuterCol, Rank,
                 Review, SafeDiv, Scalar, Star, TimeShift, Total, Un, aggs_in, and_, cols_in, conjuncts, has_agg, has_outer,
                 is_boolean, override, reviews_in, row_cols, transform, walk)

AGG = {"SUM": "SUM", "AVERAGE": "AVG", "MIN": "MIN", "MAX": "MAX", "COUNT": "COUNT", "COUNTA": "COUNT",
       "MEDIAN": "MEDIAN"}
ITER = {"SUMX": "SUM", "AVERAGEX": "AVG", "MINX": "MIN", "MAXX": "MAX", "COUNTX": "COUNT", "COUNTAX": "COUNT",
        "MEDIANX": "MEDIAN"}
TIME_INTEL = {"DATEADD", "SAMEPERIODLASTYEAR", "PARALLELPERIOD", "DATESYTD", "DATESQTD", "DATESMTD",
              "DATESBETWEEN", "DATESINPERIOD", "TOTALYTD", "TOTALQTD", "TOTALMTD", "PREVIOUSDAY", "PREVIOUSMONTH",
              "PREVIOUSQUARTER", "PREVIOUSYEAR", "NEXTDAY", "NEXTMONTH", "NEXTQUARTER", "NEXTYEAR", "STARTOFMONTH",
              "STARTOFQUARTER", "STARTOFYEAR", "ENDOFMONTH", "ENDOFQUARTER", "ENDOFYEAR", "FIRSTDATE", "LASTDATE",
              "OPENINGBALANCEMONTH", "OPENINGBALANCEQUARTER", "OPENINGBALANCEYEAR", "CLOSINGBALANCEMONTH",
              "CLOSINGBALANCEQUARTER", "CLOSINGBALANCEYEAR"}
FILTER_MODIFIERS = {"ALL", "ALLEXCEPT", "ALLSELECTED", "ALLNOBLANKROW", "REMOVEFILTERS", "ALLCROSSFILTERED"}
RELATIONSHIP_FNS = {"USERELATIONSHIP", "CROSSFILTER", "TREATAS"}
SIMPLE_SCALAR = {"ABS": "ABS", "SQRT": "SQRT", "EXP": "EXP", "LN": "LN", "LOG10": "LOG10", "SIGN": "SIGN",
                 "INT": "FLOOR", "YEAR": "YEAR", "MONTH": "MONTH", "DAY": "DAY", "QUARTER": "QUARTER",
                 "HOUR": "HOUR", "MINUTE": "MINUTE", "SECOND": "SECOND", "UPPER": "UPPER", "LOWER": "LOWER",
                 "TRIM": "TRIM", "LEN": "LENGTH", "VALUE": "TONUMBER", "WEEKDAY": "WEEKDAY", "WEEKNUM": "WEEKNUM",
                 "EOMONTH": "EOMONTH", "EDATE": "EDATE"}
CMP_OPS = {"=", "==", "<>", "<", ">", "<=", ">="}
FLIP = {"=": "=", "<>": "<>", "<": ">", ">": "<", "<=": ">=", ">=": "<="}
TIME_PART = {"DAY": "Day", "MONTH": "Month", "QUARTER": "Quarter", "YEAR": "Year"}
TO_DATE = {"DATESYTD": "Year", "DATESQTD": "Quarter", "DATESMTD": "Month",
           "TOTALYTD": "Year", "TOTALQTD": "Quarter", "TOTALMTD": "Month"}
PREVIOUS = {"PREVIOUSDAY": "Day", "PREVIOUSMONTH": "Month", "PREVIOUSQUARTER": "Quarter", "PREVIOUSYEAR": "Year"}
SQL_GAP_PREFIXES = ("time intelligence (", "RANKX:", "REMOVEFILTERS / ALL:")


def _blank_as(value):
    return 0 if isinstance(value, (int, float)) and not isinstance(value, bool) else ""
DATE_TOKENS = [("yyyy", "%Y"), ("YYYY", "%Y"), ("yy", "%y"), ("YY", "%y"), ("mmmm", "%B"), ("MMMM", "%B"),
               ("mmm", "%b"), ("MMM", "%b"), ("dddd", "%A"), ("DDDD", "%A"), ("ddd", "%a"), ("DDD", "%a"),
               ("mm", "%m"), ("MM", "%m"), ("dd", "%d"), ("DD", "%d"), ("hh", "%H"), ("HH", "%H"), ("nn", "%M"),
               ("ss", "%S"), ("m", "%m"), ("M", "%m"), ("d", "%d"), ("D", "%d")]


log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------------------- model index
class ModelIndex:
    """Case-insensitive lookups over a normalized model (DAX names are case-insensitive)."""

    def __init__(self, model: dict):
        self.model = model
        self.tables = {t["name"]: t for t in model.get("tables", [])}
        self._tables_ci = {n.lower(): n for n in self.tables}
        self.columns = {(c["table"], c["column"]): c for c in model.get("columns", [])}
        self._cols_ci = {(t.lower(), c.lower()): (t, c) for (t, c) in self.columns}
        self.measures = {m["name"]: m for m in model.get("measures", [])}
        self._meas_ci = {n.lower(): n for n in self.measures}

    def table(self, name: str | None) -> str | None:
        return self._tables_ci.get((name or "").lower())

    def column(self, table: str | None, column: str | None) -> tuple[str, str] | None:
        return self._cols_ci.get(((table or "").lower(), (column or "").lower()))

    def measure(self, name: str | None) -> str | None:
        return self._meas_ci.get((name or "").lower())

    def is_calculated(self, table: str, column: str) -> bool:
        c = self.columns.get((table, column))
        return bool(c and c.get("dax"))


@dataclass
class Translation:
    ir: object = None
    status: str = "Translated"
    notes: list = field(default_factory=list)
    reviews: list = field(default_factory=list)
    patterns: list = field(default_factory=list)
    measure_refs: list = field(default_factory=list)
    column_refs: list = field(default_factory=list)
    functions: list = field(default_factory=list)
    error: str | None = None
    explanation: str = ""
    is_alias: bool = False


@dataclass
class _Ctx:
    host: str
    mode: str = "measure"            # "measure" or "row"
    pred: object = None
    row_tables: tuple = ()
    vars: tuple = ()                 # ((name, ir), ...)
    any_table: bool = False          # predicate context: any column may be referenced row by row
    inline_calc: bool = False        # views: expand calculated columns into their SQL
    calc: bool = False               # calculated column, top row context: no filter context at all
    outer: tuple = ()                # tables whose direct column references mean the OUTER row
    nested: tuple = ()               # outer row contexts reachable with EARLIER
    tkeep: tuple = ()                # conditions of `pred` that come from FILTER(table): not just one column's filter

    def var(self, name: str):
        for n, v in reversed(self.vars):
            if n.lower() == name.lower():
                return v
        return None


class _Acc:
    def __init__(self):
        self.notes, self.reviews, self.patterns = [], [], []
        self.measure_refs, self.column_refs = [], []

    def add(self, bucket: list, item):
        if item not in bucket:
            bucket.append(item)


# ----------------------------------------------------------------------------------------- translator
class Translator:
    def __init__(self, idx: ModelIndex, graph=None):
        self.idx = idx
        self.graph = graph
        self._ast_cache: dict = {}
        self._measure_cache: dict = {}
        self._ir_cache: dict = {}
        self._stack: list = []
        self._calc_cache: dict = {}
        self.acc = _Acc()

    # ---- public ---------------------------------------------------------------------------------
    def measure(self, name: str) -> Translation:
        canon = self.idx.measure(name) or name
        if canon in self._measure_cache:
            return self._measure_cache[canon]
        m = self.idx.measures.get(canon)
        if not m:
            return Translation(status="Not found", error=f"Measure {name} not found")
        res = self._run(m["dax"], _Ctx(host=m["table"]), self_name=canon)
        self._measure_cache[canon] = res
        return res

    def calc_column(self, table: str, column: str, inline_calc: bool = False) -> Translation:
        c = self.idx.columns.get((table, column))
        if not c or not c.get("dax"):
            return Translation(status="Not calculated")
        key = (table, column, inline_calc)
        if key not in self._calc_cache:
            self._calc_cache[key] = self._run(
                c["dax"], _Ctx(host=table, mode="row", row_tables=(table,), inline_calc=inline_calc, calc=True),
                self_name=f"{table}[{column}]")
        return self._calc_cache[key]

    def never_blank(self, table: str, column: str) -> bool:
        """A calculated column that always returns a value (IF/SWITCH over literals), so a SQL COALESCE for
        DAX blank semantics is not needed."""
        if not self.idx.is_calculated(table, column) or f"{table}[{column}]" in self._stack:
            return False
        ir = self.calc_column(table, column).ir
        if isinstance(ir, Case):
            vals = [v for _, v in ir.whens] + [ir.default]
            return all(isinstance(v, Lit) and v.value is not None for v in vals)
        return isinstance(ir, Lit) and ir.value is not None

    def expression(self, dax: str, host: str, mode: str = "measure") -> Translation:
        return self._run(dax, _Ctx(host=host, mode=mode, row_tables=(host,) if mode == "row" else ()))

    # ---- machinery ---------------------------------------------------------------------------------
    def _parse(self, dax: str):
        if dax not in self._ast_cache:
            self._ast_cache[dax] = parse_dax(dax)
        return self._ast_cache[dax]

    def _run(self, dax: str, ctx: _Ctx, self_name: str | None = None) -> Translation:
        res = Translation()
        try:
            ast = self._parse(dax)
        except DaxSyntaxError as ex:
            res.status, res.error = "Parse error", str(ex)
            res.reviews.append(f"DAX could not be parsed: {ex}")
            return res
        outer_acc, self.acc = self.acc, _Acc()
        if self_name:
            self._stack.append(self_name)
        try:
            res.ir = self.tr(ast, ctx)
        except RecursionError:
            res.ir = Review("expression too deeply nested")
        except Exception as ex:  # noqa: BLE001 - an unexpected DAX shape is a review item, never a crash
            log.exception("DAX translation failed")
            res.ir = Review(f"translator could not handle this DAX ({type(ex).__name__}: {ex})")
        finally:
            if self_name:
                self._stack.pop()
            acc, self.acc = self.acc, outer_acc
        res.notes, res.patterns = acc.notes, acc.patterns
        res.reviews = acc.reviews + [r for r in reviews_in(res.ir) if r not in acc.reviews]
        res.measure_refs, res.column_refs = acc.measure_refs, acc.column_refs
        res.functions = sorted(functions_used(ast))
        res.explanation = explain(ast)
        if ast[0] == "ref" and self.idx.measure(ast[1]):
            res.is_alias = True
            res.patterns.insert(0, "Alias")
        res.status = "Needs review" if res.reviews else ("Translated with notes" if res.notes else "Translated")
        return res

    def note(self, text: str):
        self.acc.add(self.acc.notes, text)

    def pattern(self, text: str):
        self.acc.add(self.acc.patterns, text)

    def review(self, reason: str) -> Review:
        self.acc.add(self.acc.reviews, reason)
        return Review(reason)

    # ---- node dispatch -----------------------------------------------------------------------------
    def tr(self, n, ctx: _Ctx):
        k = n[0]
        if k == "num":
            return Lit(n[1])
        if k in ("str", "bool"):
            return Lit(n[1])
        if k == "empty":
            return Null()
        if k == "col":
            return self.column_ref(n[1], n[2], ctx)
        if k == "ref":
            m = self.idx.measure(n[1])
            if m:
                return self.measure_ref(m, ctx)
            return self.column_ref(ctx.host, n[1], ctx)
        if k == "name":
            v = ctx.var(n[1])
            if v is not None:
                return v
            if self.idx.table(n[1]):
                return self.review(f"table {n[1]} used where a value is expected")
            return self.review(f"unknown name {n[1]}")
        if k == "table":
            return self.review(f"table '{n[1]}' used where a value is expected")
        if k == "var":
            c = ctx
            for name, e in n[1]:
                c = replace(c, vars=c.vars + ((name, self.tr(e, c)),))
            return self.tr(n[2], c)
        if k == "neg":
            x = self.tr(n[1], ctx)
            return Lit(-x.value) if isinstance(x, Lit) and isinstance(x.value, (int, float)) else Un("NEG", x)
        if k == "not":
            return Un("NOT", self.cond(n[1], ctx))
        if k == "in":
            return self.in_list(n[1], n[2], ctx)
        if k == "bin":
            return self.binary(n[1], n[2], n[3], ctx)
        if k == "func":
            return self.func(n[1], n[2], ctx)
        return self.review(f"unsupported construct {k}")

    # ---- references ----------------------------------------------------------------------------------
    def column_ref(self, table, column, ctx: _Ctx):
        res = self.idx.column(table, column)
        if not res and self.idx.measure(column):
            return self.measure_ref(column, ctx)      # 'Table'[Measure]: a measure referenced with its table
        if not res:
            return self.review(f"unknown column {table}[{column}]")
        t, c = res
        self.acc.add(self.acc.column_refs, f"{t}[{c}]")
        if t in ctx.outer:
            if ctx.inline_calc and self.idx.is_calculated(t, c) and f"{t}[{c}]" not in self._stack:
                self._stack.append(f"{t}[{c}]")
                try:
                    return self.tr(self._parse(self.idx.columns[(t, c)]["dax"]), replace(ctx, host=t))
                except DaxSyntaxError as ex:
                    return self.review(f"calculated column {t}[{c}] could not be parsed: {ex}")
                finally:
                    self._stack.pop()
            return OuterCol(t, c)
        if ctx.mode == "row" or ctx.any_table or t in ctx.row_tables:
            if ctx.inline_calc and self.idx.is_calculated(t, c):
                key = f"{t}[{c}]"
                if key in self._stack:
                    return self.review(f"circular calculated column {key}")
                self._stack.append(key)
                try:
                    return self.tr(self._parse(self.idx.columns[(t, c)]["dax"]), replace(ctx, host=t))
                except DaxSyntaxError as ex:
                    return self.review(f"calculated column {t}[{c}] could not be parsed: {ex}")
                finally:
                    self._stack.pop()
            return Col(t, c)
        return self.review(f"column {t}[{c}] used outside an aggregation (needs row context)")

    def measure_ref(self, name: str, ctx: _Ctx):
        self.acc.add(self.acc.measure_refs, name)
        if ctx.mode == "row":
            return self.review(f"measure [{name}] evaluated per row (context transition): needs a grouped subquery")
        if name in self._stack:
            return self.review(f"circular reference to [{name}]")
        key = (name, repr(ctx.pred), repr(ctx.tkeep))
        if key in self._ir_cache:
            ir, notes, patterns = self._ir_cache[key]
            for x in notes:
                self.note(x)
            for x in patterns:
                self.pattern(x)
            return ir
        m = self.idx.measures[name]
        n0, p0 = len(self.acc.notes), len(self.acc.patterns)
        self._stack.append(name)
        try:
            ir = self.tr(self._parse(m["dax"]), _Ctx(host=m["table"], pred=ctx.pred, tkeep=ctx.tkeep))
        except DaxSyntaxError as ex:
            ir = self.review(f"measure [{name}] could not be parsed: {ex}")
        finally:
            self._stack.pop()
        self._ir_cache[key] = (ir, self.acc.notes[n0:], self.acc.patterns[p0:])
        return ir

    def column_arg(self, n, ctx: _Ctx):
        """Argument that must be a column (SUM(T[c]), DISTINCTCOUNT([c]))."""
        if n[0] == "col":
            res = self.idx.column(n[1], n[2])
        elif n[0] == "ref" and not self.idx.measure(n[1]):
            res = self.idx.column(ctx.host, n[1])
        else:
            return None
        if res:
            self.acc.add(self.acc.column_refs, f"{res[0]}[{res[1]}]")
        return res

    def table_arg(self, n) -> str | None:
        if n[0] in ("table", "name"):
            return self.idx.table(n[1])
        return None

    # ---- operators ---------------------------------------------------------------------------------
    def cond(self, n, ctx: _Ctx):
        ir = self.tr(n, ctx)
        if is_boolean(ir) or isinstance(ir, Review):
            return ir
        return Bin("<>", Fn("COALESCE", (ir, Lit(0))), Lit(0))

    def binary(self, op, l, r, ctx: _Ctx):
        if op in ("&&", "||"):
            return Bin("AND" if op == "&&" else "OR", self.cond(l, ctx), self.cond(r, ctx))
        L, R = self.tr(l, ctx), self.tr(r, ctx)
        if op in ("+", "-"):
            return self.arith(op, L, R)
        if op == "*":
            return Bin("*", L, R)
        if op == "/":
            self.note("'/' in DAX returns Infinity/NaN on division by zero; the SQL returns NULL (NULLIF).")
            return SafeDiv(L, R)
        if op == "^":
            return Fn("POWER", (L, R))
        if op == "&":
            return self.concat(L, R)
        if op in CMP_OPS:
            return self.compare(op, L, R)
        return self.review(f"operator {op}")

    @staticmethod
    def concat(L, R):
        parts = []
        for x in (L, R):
            parts.extend(x.args if isinstance(x, Fn) and x.name == "CONCAT" else (x,))
        return Fn("CONCAT", tuple(parts))

    def arith(self, op, L, R):
        if isinstance(L, Agg) and isinstance(R, Agg) and L.func == R.func == "SUM" and L.table == R.table \
                and L.pred == R.pred and L.replaced == R.replaced and not L.distinct and not R.distinct:
            return Agg("SUM", Bin(op, _coalesce0(L.arg), _coalesce0(R.arg)), L.table, False, L.pred, L.replaced)
        if isinstance(L, Lit) and isinstance(R, Lit) and all(isinstance(x.value, (int, float)) for x in (L, R)):
            return Lit(L.value + R.value if op == "+" else L.value - R.value)
        if has_agg(L) or has_agg(R):
            self.note("Blank + / - blank gives 0 in the SQL where DAX gives BLANK (only matters when a row has no data on either side).")
        return BlankArith(op, L, R)

    def compare(self, op, L, R):
        sql_op = "=" if op in ("=", "==") else op
        for row, value in ((L, R), (R, L)):
            if row_cols(row) and has_agg(value) and not row_cols(value) and not isinstance(value, Review):
                return self._compare_rows(op, sql_op, L, R, row)
        if op != "==":
            if has_agg(L) and isinstance(R, Lit) and not isinstance(L, Lit):
                L = Fn("COALESCE", (L, Lit(0 if isinstance(R.value, (int, float)) else "")))
            elif has_agg(R) and isinstance(L, Lit):
                R = Fn("COALESCE", (R, Lit(0 if isinstance(L.value, (int, float)) else "")))
            elif isinstance(R, Lit) and not isinstance(L, Lit) and self._blank_matters(sql_op, L, R.value):
                L = Fn("COALESCE", (L, Lit(_blank_as(R.value))))
            elif isinstance(L, Lit) and not isinstance(R, Lit) and self._blank_matters(FLIP[sql_op], R, L.value):
                R = Fn("COALESCE", (R, Lit(_blank_as(L.value))))
        return Bin(sql_op, L, R)

    def _compare_rows(self, op, sql_op, L, R, row):
        """Rows compared with a value of the visual: a BLANK on either side counts as 0, "", FALSE or 30-Dec-1899."""
        if op == "==":                                     # strictly equal: BLANK only equals BLANK
            return Bin("OR", Bin("AND", IsNull(L), IsNull(R)), Bin("=", L, R))
        zero = self._zero(row)
        if zero is None:
            return self.review("rows compared with a value of the visual whose type is not known")
        return Bin(sql_op, Fn("COALESCE", (L, zero)), Fn("COALESCE", (R, zero)))

    def _zero(self, x):
        """What BLANK counts as next to x."""
        if isinstance(x, (Col, OuterCol)):
            kind = str((self.idx.columns.get((x.table, x.column)) or {}).get("data_type") or "").lower()
            if "date" in kind:
                return Fn("DATE", (Lit(1899), Lit(12), Lit(30)))
            if "number" in kind or kind in ("int64", "double", "decimal"):
                return Lit(0)
            if kind in ("text", "string"):
                return Lit("")
            return Lit(False) if kind in ("true/false", "boolean") else None
        if isinstance(x, (BlankArith, SafeDiv)) or (isinstance(x, Bin) and x.op in ("+", "-", "*", "/")) \
                or (isinstance(x, Un) and x.op == "NEG"):
            return Lit(0)
        if isinstance(x, Fn):
            if x.name in ("YEAR", "MONTH", "DAY", "QUARTER", "WEEKDAY", "WEEKNUM", "HOUR", "MINUTE", "SECOND", "LENGTH", "ROUND",
                          "FLOOR", "CEIL", "ABS", "POWER", "MOD", "SQRT", "EXP", "LN", "DATEDIFF"):
                return Lit(0)
            if x.name in ("DATE", "EDATE", "EOMONTH"):
                return Fn("DATE", (Lit(1899), Lit(12), Lit(30)))
            if x.name in ("CONCAT", "LEFT", "RIGHT", "SUBSTRING", "UPPER", "LOWER", "TRIM", "REPLACE", "TIMEFMT", "TOSTRING"):
                return Lit("")
            if x.name == "COALESCE":
                return self._zero(x.args[0])
        return None

    def _blank_matters(self, op, x, value) -> bool:
        """DAX compares BLANK as 0 or "": `x = ""` and `x <> 1` are TRUE for a blank x, where SQL gives NULL."""
        if value is None or isinstance(value, bool) or has_agg(x) or isinstance(x, (Null, Review)):
            return False
        if isinstance(x, Fn) and x.name == "COALESCE":
            return False
        if isinstance(x, (Col, OuterCol)) and self.never_blank(x.table, x.column):
            return False
        zero = _blank_as(value)
        try:
            return {"=": zero == value, "<>": zero != value, "<": zero < value, "<=": zero <= value,
                    ">": zero > value, ">=": zero >= value}[op]
        except TypeError:
            return False

    def in_list(self, x, values, ctx: _Ctx):
        X = self.tr(x, ctx)
        if values[0] != "tablector":
            return self.review("IN against a table expression")
        items = []
        for v in values[1]:
            if v[0] == "tuple":
                return self.review("multi-column IN")
            items.append(self.tr(v, ctx))
        return InList(X, tuple(items))

    # ---- functions ---------------------------------------------------------------------------------
    def func(self, name: str, args: list, ctx: _Ctx):
        a = args
        if name in AGG:
            if name in ("MIN", "MAX") and len(a) == 2:
                return Fn("LEAST" if name == "MIN" else "GREATEST", (self.tr(a[0], ctx), self.tr(a[1], ctx)))
            col = self.column_arg(a[0], ctx) if a else None
            if not col:
                return self.review(f"{name} over an expression (use {name}X)")
            if ctx.calc:
                return self._corr(AGG[name], Col(*col), col[0], False, None, ctx)
            return Agg(AGG[name], Col(*col), col[0], False, ctx.pred)
        if name in ("DISTINCTCOUNT", "DISTINCTCOUNTNOBLANK", "APPROXIMATEDISTINCTCOUNT"):
            col = self.column_arg(a[0], ctx) if a else None
            if not col:
                return self.review(f"{name} over an expression")
            self.pattern("Distinct count")
            if name == "DISTINCTCOUNT":
                self.note("DISTINCTCOUNT counts BLANK as a value; COUNT(DISTINCT) ignores NULL.")
            if ctx.calc:
                return self._corr("COUNT", Col(*col), col[0], True, None, ctx)
            return Agg("COUNT", Col(*col), col[0], True, ctx.pred)
        if name == "COUNTBLANK":
            col = self.column_arg(a[0], ctx) if a else None
            if not col:
                return self.review("COUNTBLANK over an expression")
            return Agg("SUM", Case(((IsNull(Col(*col)), Lit(1)),), Lit(0)), col[0], False, ctx.pred)
        if name == "COUNTROWS":
            return self.countrows(a[0] if a else ("empty",), ctx)
        if name in ITER:
            return self.iterator(name, a, ctx)
        if name == "CALCULATE":
            return self.calculate(a, ctx)
        if name == "DIVIDE":
            num, den = self.tr(a[0], ctx), self.tr(a[1], ctx)
            alt = self.tr(a[2], ctx) if len(a) > 2 and a[2][0] != "empty" else None
            self.pattern("Ratio")
            return SafeDiv(num, den, alt)
        if name == "IF":
            c = self.cond(a[0], ctx)
            then = self.tr(a[1], ctx) if len(a) > 1 else Null()
            other = self.tr(a[2], ctx) if len(a) > 2 else Null()
            simple = _guarded_division(c, then, other)
            if simple is not None:
                self.pattern("Ratio")
                return simple
            self.pattern("Conditional")
            return Case(((c, then),), other)
        if name == "SWITCH":
            return self.switch(a, ctx)
        if name in ("AND", "OR"):
            return Bin(name, self.cond(a[0], ctx), self.cond(a[1], ctx))
        if name == "NOT":
            return Un("NOT", self.cond(a[0], ctx))
        if name == "ISBLANK":
            return IsNull(self.tr(a[0], ctx))
        if name == "BLANK":
            return Null()
        if name in ("TRUE", "FALSE"):
            return Lit(name == "TRUE")
        if name == "COALESCE":
            return Fn("COALESCE", tuple(self.tr(x, ctx) for x in a))
        if name == "IFERROR":
            self.note("IFERROR dropped: SQL has no error trap; divisions are already NULL-safe.")
            return self.tr(a[0], ctx)
        if name == "SELECTEDVALUE":
            col = self.column_arg(a[0], ctx) if a else None
            if not col:
                return self.review("SELECTEDVALUE over an expression")
            alt = self.tr(a[1], ctx) if len(a) > 1 else Null()
            self.note("SELECTEDVALUE becomes 'the single value in each row of the visual' (COUNT DISTINCT = 1).")
            return Case(((Bin("=", Agg("COUNT", Col(*col), col[0], True, ctx.pred), Lit(1)),
                          Agg("MAX", Col(*col), col[0], False, ctx.pred)),), alt)
        if name == "HASONEVALUE":
            col = self.column_arg(a[0], ctx) if a else None
            if not col:
                return self.review("HASONEVALUE over an expression")
            return Bin("=", Agg("COUNT", Col(*col), col[0], True, ctx.pred), Lit(1))
        if name == "RELATED":
            col = self.column_arg(a[0], ctx) if a else None
            if not col:
                return self.review("RELATED over an expression")
            self.pattern("Uses RELATED (join)")
            return Col(*col) if ctx.mode == "row" or ctx.row_tables else self.review("RELATED outside row context")
        if name == "FORMAT":
            return self.format_(a, ctx)
        if name in ("CONCATENATE",):
            return self.concat(self.tr(a[0], ctx), self.tr(a[1], ctx))
        if name == "LEFT" or name == "RIGHT":
            n = self.tr(a[1], ctx) if len(a) > 1 else Lit(1)
            return Fn(name, (self.tr(a[0], ctx), n))
        if name == "MID":
            return Fn("SUBSTRING", tuple(self.tr(x, ctx) for x in a[:3]))
        if name == "SUBSTITUTE":
            return Fn("REPLACE", tuple(self.tr(x, ctx) for x in a[:3]))
        if name in ("ROUND", "ROUNDUP", "ROUNDDOWN", "MROUND"):
            digits = self.tr(a[1], ctx) if len(a) > 1 else Lit(0)
            if name != "ROUND":
                self.note(f"{name} approximated with ROUND.")
            return Fn("ROUND", (self.tr(a[0], ctx), digits))
        if name in ("CEILING", "FLOOR", "ISO.CEILING"):
            if len(a) > 1:
                self.note(f"{name} significance argument ignored (rounds to whole numbers).")
            return Fn("CEIL" if "CEILING" in name else "FLOOR", (self.tr(a[0], ctx),))
        if name in ("MOD", "POWER"):
            return Fn(name, (self.tr(a[0], ctx), self.tr(a[1], ctx)))
        if name == "TRUNC":
            return Fn("FLOOR", (self.tr(a[0], ctx),))
        if name == "DATE":
            return Fn("DATE", tuple(self.tr(x, ctx) for x in a[:3]))
        if name == "TODAY":
            return Fn("CURRENT_DATE", ())
        if name in ("NOW", "UTCNOW"):
            return Fn("CURRENT_TIMESTAMP", ())
        if name == "DATEDIFF":
            unit = a[2][1] if len(a) > 2 and a[2][0] in ("name", "str") else "DAY"
            return Fn("DATEDIFF", (Lit(str(unit).upper()), self.tr(a[0], ctx), self.tr(a[1], ctx)))
        if name in SIMPLE_SCALAR:
            if name in ("WEEKDAY", "WEEKNUM", "EOMONTH", "EDATE"):
                self.note(f"{name} has dialect-specific SQL: check the generated function.")
            return Fn(SIMPLE_SCALAR[name], tuple(self.tr(x, ctx) for x in a))
        if name == "CONTAINSSTRING" and len(a) == 2:
            return Fn("LIKE_CONTAINS", (self.tr(a[0], ctx), self.tr(a[1], ctx)))
        if name == "LOOKUPVALUE":
            return self.lookupvalue(a, ctx)
        if name in ("EARLIER", "EARLIEST"):
            col = self.column_arg(a[0], ctx) if a else None
            if col and col[0] in ctx.nested:
                return OuterCol(*col)
            return self.review(f"{name} outside a nested row context")
        if name == "RANKX":
            return self.rankx(a, ctx)
        if name in ("TOTALYTD", "TOTALQTD", "TOTALMTD"):
            return self.total_to_date(name, a, ctx)
        if name in TIME_INTEL:
            self.pattern("Time intelligence")
            return self.review(f"time intelligence ({name}): needs a date-offset join to a date table")
        if name in FILTER_MODIFIERS:
            return self.review(f"{name} used as a value")
        return self.review(f"{name} is not translated automatically")

    def lookupvalue(self, a, ctx: _Ctx):
        res = self.column_arg(a[0], ctx) if a else None
        if not res or len(a) < 3:
            return self.review("LOOKUPVALUE needs a result column and search column / value pairs")
        pairs, i = [], 1
        while i + 1 < len(a):
            sc = self.column_arg(a[i], ctx)
            if not sc or sc[0] != res[0]:
                return self.review("LOOKUPVALUE search column is not in the result table")
            pairs.append((Col(*sc), self.tr(a[i + 1], ctx)))
            i += 2
        default = self.tr(a[i], ctx) if i < len(a) else None
        self.pattern("Lookup (LOOKUPVALUE)")
        self.note("LOOKUPVALUE fails when several rows match; the SQL takes the MAX of the matches and "
                  "Incorta lookup() needs a key on the search column(s).")
        return Lookup(Col(*res), tuple(pairs), default)

    def rankx(self, a, ctx: _Ctx):
        if len(a) < 2:
            return self.review("RANKX needs a table and an expression")
        if ctx.mode == "row":
            return self.review("RANKX in a calculated column: needs a window function over the table")
        tbl, over = a[0], None
        if tbl[0] == "func" and tbl[1] in ("ALL", "REMOVEFILTERS", "ALLNOBLANKROW", "ALLSELECTED") and tbl[2]:
            cols = [self.column_arg(x, ctx) for x in tbl[2]]
            if all(cols):
                over = tuple(Col(*c) for c in cols)
            elif all(self.table_arg(x) for x in tbl[2]):
                over = tuple(self.table_arg(x) for x in tbl[2])
            if tbl[1] == "ALLSELECTED":
                self.note("RANKX over ALLSELECTED ranks only the values the slicers leave; the Incorta rank does the same.")
        elif self.table_arg(tbl):
            over = (self.table_arg(tbl),)
        if over is None:
            return self.review("RANKX over a computed table")
        if len(a) > 2 and a[2][0] != "empty":
            return self.review("RANKX with a value argument (rank of another value)")
        desc = True
        if len(a) > 3:
            o = a[3]
            if o[0] in ("name", "str"):
                desc = str(o[1]).upper() != "ASC"
            elif o[0] == "num":
                desc = o[1] == 0
        dense = len(a) > 4 and a[4][0] in ("name", "str") and str(a[4][1]).upper() == "DENSE"
        inner = self.tr(a[1], ctx)
        self.pattern("Ranking (RANKX)")
        return Rank(inner, over, desc, dense)

    def total_to_date(self, name: str, a, ctx: _Ctx):
        date = self.column_arg(a[1], ctx) if len(a) > 1 else None
        if not date:
            return self.review(f"{name} without a date column")
        extra = list(a[2:])
        if extra and extra[-1][0] == "str":
            return self.review(f"{name} with a fiscal year end: map it to the Incorta fiscal calendar")
        new = keep = None
        pctx = replace(ctx, mode="row", any_table=True)
        for f in extra:
            kind, payload = self.filter_arg(f, pctx)
            if kind not in ("pred", "keep", "tkeep"):
                return self.review(payload if kind == "review" else f"{name} with a {kind} filter")
            if kind == "pred":
                new = and_(new, payload)
            else:
                keep = and_(keep, payload[1] if kind == "tkeep" else payload)
        pred = and_(override(ctx.pred, new), keep)
        self.pattern("Time intelligence")
        return TimeShift(self.tr(a[0], replace(ctx, pred=pred)), Col(*date), "todate", 0, TO_DATE[name])

    def time_spec(self, name: str, a, ctx: _Ctx):
        """(date column, kind, n, part) for the time-intelligence filters Incorta's ago() / toDate() cover."""
        date = self.column_arg(a[0], ctx) if a else None
        if not date:
            return None
        d = Col(*date)

        def num(x):
            if x[0] == "num":
                return int(x[1])
            if x[0] == "neg" and x[1][0] == "num":
                return -int(x[1][1])
            return None

        def part(x):
            return TIME_PART.get(str(x[1]).upper()) if x[0] in ("name", "str") else None

        if name in ("DATEADD", "PARALLELPERIOD") and len(a) >= 3:
            n, p = num(a[1]), part(a[2])
            if n is None or not p or n >= 0:
                return None
            if name == "PARALLELPERIOD":
                self.note("PARALLELPERIOD shifts whole periods; ago() shifts by the insight's date part (the same "
                          "when the insight is grouped by that period).")
            return d, "ago", -n, p
        if name == "SAMEPERIODLASTYEAR":
            return d, "ago", 1, "Year"
        if name in PREVIOUS:
            self.note(f"{name} returns the whole previous period; ago() matches it when the insight is grouped by "
                      f"{PREVIOUS[name].lower()}.")
            return d, "ago", 1, PREVIOUS[name]
        if name in ("DATESYTD", "DATESQTD", "DATESMTD"):
            if len(a) > 1:
                return None
            return d, "todate", 0, TO_DATE[name]
        return None

    def switch(self, a, ctx: _Ctx):
        self.pattern("Conditional")
        if not a:
            return Null()
        first, rest = a[0], a[1:]
        default = self.tr(rest[-1], ctx) if len(rest) % 2 == 1 else Null()
        pairs = rest[:-1] if len(rest) % 2 == 1 else rest
        whens = []
        if first[0] == "bool" and first[1] is True or (first[0] == "func" and first[1] == "TRUE"):
            for i in range(0, len(pairs), 2):
                whens.append((self.cond(pairs[i], ctx), self.tr(pairs[i + 1], ctx)))
        else:
            subject = self.tr(first, ctx)
            for i in range(0, len(pairs), 2):
                whens.append((Bin("=", subject, self.tr(pairs[i], ctx)), self.tr(pairs[i + 1], ctx)))
        return Case(tuple(whens), default)

    def format_(self, a, ctx: _Ctx):
        x = self.tr(a[0], ctx)
        fmt = a[1][1] if len(a) > 1 and a[1][0] == "str" else None
        if fmt and fmt.strip() and all(ch.isalpha() or ch in " -/.,:" for ch in fmt) \
                and any(t in fmt for t in ("y", "m", "M", "d", "h", "H")):
            return Fn("TIMEFMT", (x, Lit(dax_date_format_to_strftime(fmt))))
        self.note(f"FORMAT(..., \"{fmt}\") is display formatting: set the number format in the BI tool instead.")
        m = re.fullmatch(r"(#,##0|#,0|0)(?:\.(0+))?(%)?", (fmt or "").strip())
        if m:                                              # FORMAT(x, "0.0%"): text with fixed decimals
            return Fn("NUMFMT", (x, Lit(len(m.group(2) or "")), Lit(bool(m.group(3))), Lit(m.group(1) != "0")))
        return Fn("TOSTRING", (x,))

    # ---- aggregation over tables -------------------------------------------------------------------
    def countrows(self, n, ctx: _Ctx):
        t = self.table_arg(n)
        if t:
            return self._corr("COUNT", Star(), t, False, None, ctx) if ctx.calc else Agg("COUNT", Star(), t, False, ctx.pred)
        if n[0] == "func" and n[1] == "FILTER" and len(n[2]) == 2:
            t = self.table_arg(n[2][0])
            if t:
                cond = self.cond(n[2][1], self._enter_row(ctx, t))
                if ctx.calc:
                    return self._corr("COUNT", Star(), t, False, cond, ctx)
                if self._sees_outer_row(ctx, t, cond):
                    return self.review("COUNTROWS over rows that depend on an outer row: needs a correlated subquery")
                return Agg("COUNT", Star(), t, False, and_(ctx.pred, cond))
        if n[0] == "func" and n[1] in ("VALUES", "DISTINCT") and n[2]:
            col = self.column_arg(n[2][0], ctx)
            if col:
                if ctx.calc:
                    return self._corr("COUNT", Col(*col), col[0], True, None, ctx)
                return Agg("COUNT", Col(*col), col[0], True, ctx.pred)
        return self.review("COUNTROWS over a computed table")

    def iterator(self, name: str, a, ctx: _Ctx):
        self.pattern("Iterator (row by row)")
        if len(a) < 2:
            return self.review(f"{name} needs a table and an expression")
        tbl, expr = a[0], a[1]
        t, filt = self.table_arg(tbl), None
        if not t and tbl[0] == "func" and tbl[1] == "FILTER" and len(tbl[2]) == 2:
            t = self.table_arg(tbl[2][0])
            filt = tbl[2][1]
        if not t:
            return self.review(f"{name} over a grouped or computed table: needs a GROUP BY subquery")
        ictx = self._enter_row(ctx, t)
        extra = self.cond(filt, ictx) if filt is not None else None
        row = self.tr(expr, replace(ictx, any_table=False))
        if has_agg(row):
            return self.review(f"{name} with a nested aggregation: needs a grouped subquery")
        if ctx.calc:
            return self._corr(ITER[name], row, t, False, extra, ctx)
        if self._sees_outer_row(ctx, t, row, extra):
            return self.review(f"{name} over rows that depend on an outer row: needs a correlated subquery")
        return Agg(ITER[name], row, t, False, and_(ctx.pred, extra))

    def _sees_outer_row(self, ctx: _Ctx, t: str, *irs) -> bool:
        """Inside another row context of a measure: do the rows of `t` depend on the outer row?"""
        if ctx.mode != "row" or ctx.calc:
            return False
        if any(row_cols(v) for _, v in ctx.vars):          # a VAR that holds a column of the outer row
            return True
        own = {t} | ((set(self.graph.reachable(t)) if self.graph else set()) - set(ctx.row_tables))
        return any(c.table not in own for ir in irs if ir is not None for c in row_cols(ir))

    def _enter_row(self, ctx: _Ctx, t: str) -> _Ctx:
        """Row context over table `t` (iterator or FILTER). Inside a calculated column the current row becomes
        the OUTER row: variables captured before, direct references to the other row tables and EARLIER all
        point to it (OuterCol), while references to `t` mean the rows being aggregated."""
        if not ctx.calc:
            return replace(ctx, mode="row", row_tables=ctx.row_tables + (t,))
        outer_tables = ctx.row_tables

        def to_outer(n):
            return OuterCol(n.table, n.column) if isinstance(n, Col) and n.table in outer_tables else n

        vars_ = tuple((name, transform(v, to_outer)) for name, v in ctx.vars)
        return replace(ctx, mode="row", calc=False, row_tables=(t,), vars=vars_, any_table=False,
                       outer=tuple(x for x in outer_tables if x != t), nested=outer_tables)

    def _corr(self, func: str, arg, table: str, distinct: bool, pred, ctx: _Ctx) -> CorrAgg:
        """Aggregate inside a calculated column: split its filter into correlation keys and inner conditions."""
        self.pattern("Aggregate per row (calculated column)")
        keys, inner, why = [], None, ""
        for c in conjuncts(pred):
            if not has_outer(c):
                inner = and_(inner, c)
            elif isinstance(c, Bin) and c.op == "=" and has_outer(c.left) != has_outer(c.right):
                keys.append((c.right, c.left) if has_outer(c.left) else (c.left, c.right))
            else:
                why = "per-row aggregate with a range condition on the current row (running total): needs a self-join"
        if has_outer(arg):
            why = "the aggregated expression uses the current row (EARLIER): needs a self-join"
        if func == "MEDIAN":
            self.note("MEDIAN per group: the SQL uses PERCENTILE_CONT(0.5) / percentile(x, 0.5) where the dialect has no MEDIAN.")
        return CorrAgg(func, arg, table, distinct, pred, tuple(keys), inner, ctx.host, why)

    def calculate(self, a, ctx: _Ctx):
        if not a:
            return Null()
        if ctx.calc:
            return self.review("CALCULATE in a calculated column (context transition): needs a grouped subquery")
        if ctx.mode == "row":                              # context transition: only a total nothing can filter survives it
            before = len(self.acc.reviews)
            inner = self._calculate(a, ctx)
            if isinstance(inner, Total) and inner.independent or isinstance(inner, Review):
                return inner
            del self.acc.reviews[before:]
            return self.review("CALCULATE evaluated per row (context transition): needs a grouped subquery")
        return self._calculate(a, ctx)

    def _calculate(self, a, ctx: _Ctx):
        expr, filters = a[0], a[1:]
        tfilters = []                                      # (table, condition) of FILTER(table, condition)
        new = keep = independent = shift = removed = None
        pctx = replace(ctx, mode="row", any_table=True)
        for f in filters:
            kind, payload = self.filter_arg(f, pctx)
            if kind == "pred":                             # the filters of one CALCULATE all apply together
                new = and_(new, payload)
            elif kind == "keep":
                keep = and_(keep, payload)
            elif kind == "tkeep":
                tfilters.append(payload)
            elif kind == "all":
                if independent:
                    return self.review("two FILTER(ALL(...)) in one CALCULATE")
                independent = payload
            elif kind == "time":
                if shift:
                    return self.review("two time-intelligence filters in one CALCULATE")
                shift = payload
            elif kind == "total":
                tables, cols = removed or ((), ())
                removed = (tables + tuple(x for x in payload[0] if x not in tables),
                           cols + tuple(x for x in payload[1] if x not in cols))
            else:
                return self.review(payload)
        if independent and (new is not None or keep is not None or tfilters):
            return self.review("FILTER(ALL(...)) combined with other filters: needs a separate dataset")
        pred = self._without(ctx.pred, removed) if removed else ctx.pred
        if tfilters:                                       # FILTER(T) sees the outer filters on T and what it looks up
            seen = set()
            for t, _ in tfilters:
                seen |= {t} | (set(self.graph.reachable(t)) if self.graph else set())
            held = [c for c in conjuncts(ctx.pred) if row_cols(c) and {x.table for x in row_cols(c)} <= seen]
        else:
            held = []
        outer, pred = pred, override(pred, new)
        lost = [c for c in conjuncts(outer) if c not in conjuncts(pred)]
        if any(c in ctx.tkeep for c in lost):
            return self.review("a CALCULATE filter replaces one column of an outer FILTER(table): needs a separate dataset")
        for c in held:
            if c not in conjuncts(pred):
                pred = and_(pred, c)
        pred = and_(pred, keep)
        tkeep = tuple(c for c in ctx.tkeep if c in conjuncts(pred))
        for _, cond in tfilters:
            pred = and_(pred, cond)
            tkeep += tuple(conjuncts(cond))
        replaced = tuple(sorted((c.table, c.column) for c in row_cols(new))) if new is not None else ()
        ctx_in = replace(ctx, pred=pred, tkeep=tkeep)
        if independent and (shift or removed):
            return self.review("FILTER(ALL(...)) combined with time intelligence or REMOVEFILTERS")
        if shift or removed:
            inner = _replaces(self.tr(expr, ctx_in), replaced)
            if removed:
                self.pattern("Share of total (REMOVEFILTERS)")
                alone = "" if shift or tfilters else self._independent_of_filters(inner, removed)
                if alone:
                    self.note(f"REMOVEFILTERS / ALL on {alone}: the value ignores every slicer and filter, so the SQL "
                              f"computes it on its own.")
                inner = Total(inner, removed[0], removed[1], alone)
            if shift:
                self.pattern("Time intelligence")
                inner = TimeShift(inner, *shift)
            if pred is not ctx.pred:
                self.pattern("Filtered aggregate (CALCULATE)")
            return inner
        if independent:
            table, cond = independent
            if has_agg(cond):
                return self.review(f"FILTER(ALL({table}), ...) compares rows with a value of the visual (running total): "
                                   f"needs a window function")
            inner = self.tr(expr, replace(ctx, pred=cond))
            agg_tables = {x.table for x in aggs_in(inner)}
            lookup = set(self.graph.reachable(table)) if self.graph else set()
            if agg_tables == {table} and all(x.table in {table} | lookup for x in cols_in(inner)):
                self.pattern("Filter-independent (ALL)")
                note = (f"ALL({table}) removes every filter on {table} and the tables it looks up, "
                        f"so this value ignores slicers and filters on them.")
                self.note(note)
                return Scalar(inner, table, note)
            return self.review(f"FILTER(ALL({table}), ...) around an aggregation of another table: "
                               f"needs a separate dataset or a window function")
        if pred is not ctx.pred:
            self.pattern("Filtered aggregate (CALCULATE)")
        return _replaces(self.tr(expr, ctx_in), replaced)

    def _without(self, pred, removed):
        """The filters of an outer CALCULATE that REMOVEFILTERS / ALL clears: (tables, columns)."""
        tables, columns = removed
        if "*" in tables:
            return None
        cleared = set(tables)
        for t in tables:                                   # ALL(T) clears T and the tables it looks up
            cleared |= set(self.graph.reachable(t)) if self.graph else set()
        out = None
        for c in conjuncts(pred):
            if not all(x.table in cleared or x in columns for x in row_cols(c)) or not row_cols(c):
                out = and_(out, c)
        return out

    def _independent_of_filters(self, inner, removed) -> str:
        """The table of `inner` when REMOVEFILTERS / ALL clears everything that can filter it: every table that
        filters T is one T looks up (no both-way or many-to-many relationship around it). "" otherwise."""
        tables, columns = removed
        aggs = aggs_in(inner)
        host = {a.table for a in aggs}
        if len(host) != 1 or columns or self.graph is None or any(
                isinstance(n, (Review, Total, Scalar, TimeShift, Rank, CorrAgg, Lookup)) for n in walk(inner)):
            return ""
        t = next(iter(host))
        if "*" not in tables and t not in tables:
            return ""
        if any(has_agg(a.pred) for a in aggs if a.pred is not None):
            return ""
        around = {t} | set(self.graph.reachable(t))
        if any({r["from_table"], r["to_table"]} & around for r in self.graph.both_direction + self.graph.many_to_many):
            return ""
        if not {c.table for c in cols_in(inner)} <= around:
            return ""
        return t

    def filter_arg(self, f, pctx: _Ctx):
        if f[0] == "func":
            name, a = f[1], f[2]
            if name == "FILTER" and len(a) == 2:
                tbl, cond = a
                t = self.table_arg(tbl)
                if t:                                      # rows of T in the current context: adds to its filters
                    return "tkeep", (t, self.cond(cond, replace(pctx, row_tables=(t,))))
                if tbl[0] == "func" and tbl[1] in ("ALL", "ALLNOBLANKROW", "REMOVEFILTERS") and tbl[2]:
                    ts = [self.table_arg(x) for x in tbl[2]]
                    if len(ts) == 1 and ts[0]:
                        return "all", (ts[0], self.cond(cond, replace(pctx, row_tables=(ts[0],))))
                    if all(x[0] in ("col", "ref") for x in tbl[2]):
                        return "pred", self.cond(cond, pctx)          # FILTER(ALL(col), cond) == boolean filter
                return "review", "FILTER over a computed table (VALUES, ALLSELECTED ...)"
            if name == "KEEPFILTERS" and a:
                self.note("KEEPFILTERS: the filter intersects instead of replacing; SQL does the same.")
                kind, payload = self.filter_arg(a[0], pctx)
                if kind == "all":
                    return "review", "KEEPFILTERS(FILTER(ALL(...))): needs a separate dataset"
                return ("keep" if kind == "pred" else kind), payload
            if name in ("REMOVEFILTERS", "ALL"):
                if not a:
                    return "total", (("*",), ())
                ts = [self.table_arg(x) for x in a]
                if all(ts):
                    return "total", (tuple(ts), ())
                cols = [self.column_arg(x, pctx) for x in a]
                if all(cols):
                    return "total", ((), tuple(Col(*c) for c in cols))
            if name in FILTER_MODIFIERS:
                return "review", f"{name}(...) removes filters: needs a window function or a separate dataset"
            if name in RELATIONSHIP_FNS:
                return "review", f"{name} changes how tables relate: build the join explicitly"
            if name in TIME_INTEL:
                self.pattern("Time intelligence")
                spec = self.time_spec(name, a, pctx)
                if spec:
                    return "time", spec
                return "review", f"time intelligence ({name}): needs a date-offset join to a date table"
            if name in ("VALUES", "DISTINCT", "SUMMARIZE", "CALCULATETABLE", "TOPN", "DATATABLE"):
                return "review", f"{name}(...) as a filter table"
        if f[0] in ("table", "name") and self.table_arg(f):
            return "review", "a whole table used as a filter"
        return "pred", self.cond(f, pctx)


def dax_date_format_to_strftime(fmt: str) -> str:
    """'MMM yyyy' -> '%b %Y' (longest token first, left to right)."""
    tokens = sorted(DATE_TOKENS, key=lambda t: -len(t[0]))
    out, i = [], 0
    while i < len(fmt):
        for tok, rep in tokens:
            if fmt.startswith(tok, i):
                out.append(rep)
                i += len(tok)
                break
        else:
            out.append(fmt[i])
            i += 1
    return "".join(out)


def _replaces(node, cols: tuple):
    """Note on every aggregate of `node` the columns whose filters the CALCULATE around it replaced."""
    if not cols:
        return node
    return transform(node, lambda n: replace(n, replaced=tuple(dict.fromkeys(n.replaced + cols))) if isinstance(n, Agg) else n)


def _guarded_division(c, then, other):
    """IF(d <> 0, n / d, BLANK()) and IF(d = 0, BLANK(), n / d) are just a NULL-safe division."""
    def is_zero_test(node, op):
        return (isinstance(node, Bin) and node.op == op and node.right == Lit(0)
                and isinstance(node.left, Fn) and node.left.name == "COALESCE" and node.left.args[1] == Lit(0))
    if is_zero_test(c, "<>") and isinstance(then, SafeDiv) and then.alt is None and then.den == c.left.args[0] \
            and isinstance(other, Null):
        return then
    if is_zero_test(c, "=") and isinstance(other, SafeDiv) and other.alt is None and other.den == c.left.args[0] \
            and isinstance(then, Null):
        return other
    return None


def _coalesce0(x):
    return x if isinstance(x, (Lit, Null)) else Fn("COALESCE", (x, Lit(0)))


# ----------------------------------------------------------------------------------------- explanation
_WORD = {"SUM": "sum of", "AVERAGE": "average of", "MIN": "minimum of", "MAX": "maximum of", "COUNT": "count of",
         "COUNTA": "count of", "DISTINCTCOUNT": "distinct count of", "MEDIAN": "median of"}


def explain(n, depth: int = 0) -> str:
    """Plain-English template of a DAX expression (measure names kept, not expanded)."""
    if depth > 6:
        return "..."
    k = n[0]
    if k == "num":
        return str(n[1])
    if k == "str":
        return f"'{n[1]}'"
    if k == "bool":
        return str(n[1]).upper()
    if k == "col":
        return f"{n[1]}[{n[2]}]"
    if k == "ref":
        return f"[{n[1]}]"
    if k in ("table", "name"):
        return str(n[1])
    if k == "neg":
        return f"-{explain(n[1], depth + 1)}"
    if k == "not":
        return f"not ({explain(n[1], depth + 1)})"
    if k == "var":
        return explain(n[2], depth + 1) + " (using variables " + ", ".join(v for v, _ in n[1]) + ")"
    if k == "in":
        return f"{explain(n[1], depth + 1)} in {explain(n[2], depth + 1)}"
    if k == "tablector":
        return "{" + ", ".join(explain(x, depth + 1) for x in n[1]) + "}"
    if k == "bin":
        words = {"+": "+", "-": "-", "*": "x", "/": "÷", "&": "&", "&&": "and", "||": "or"}
        return f"{explain(n[2], depth + 1)} {words.get(n[1], n[1])} {explain(n[3], depth + 1)}"
    if k == "func":
        name, a = n[1], n[2]
        ex = [explain(x, depth + 1) for x in a]
        if name in _WORD and a:
            return f"{_WORD[name]} {ex[0]}"
        if name == "COUNTROWS" and a:
            return f"number of rows of {ex[0]}"
        if name == "CALCULATE" and a:
            return f"{ex[0]}" + (f" where {' and '.join(ex[1:])}" if len(ex) > 1 else "")
        if name == "FILTER" and len(a) == 2:
            inner = a[0]
            if inner[0] == "func" and inner[1] in ("ALL", "REMOVEFILTERS"):
                return f"all {explain(inner[2][0], depth + 1) if inner[2] else ''} rows with {ex[1]} (ignoring report filters)"
            return f"{ex[0]} rows with {ex[1]}"
        if name == "DIVIDE" and len(a) >= 2:
            return f"{ex[0]} ÷ {ex[1]}" + (" (blank when divided by zero)" if len(a) == 2 else f" ({ex[2]} when divided by zero)")
        if name == "IF" and len(a) >= 2:
            return f"if {ex[0]} then {ex[1]}" + (f", otherwise {ex[2]}" if len(a) > 2 else ", otherwise blank")
        if name == "BLANK":
            return "blank"
        if name.endswith("X") and name[:-1] in ("SUM", "AVERAGE", "MIN", "MAX", "COUNT") and len(a) == 2:
            return f"{_WORD.get(name[:-1], name[:-1].lower())} {ex[1]} for each row of {ex[0]}"
        return f"{name}(" + ", ".join(ex) + ")"
    return str(n)


# ----------------------------------------------------------------------------------------- classification
def classify_measure(tr: Translation, ast_is_alias: bool, graph=None) -> tuple[str, str]:
    """Primary pattern and complexity (Low / Medium / High) of a translated measure."""
    ir = tr.ir
    if tr.status in ("Parse error", "Not found"):
        return "Could not parse", "High"
    if ast_is_alias:
        return "Alias", "Low"
    if tr.reviews:
        hard = [r for r in tr.reviews if not r.startswith(SQL_GAP_PREFIXES)]
        if "Time intelligence" in tr.patterns and not [r for r in hard if "time intelligence" not in r]:
            return "Time intelligence (needs review)", "High"
        if not hard and "Ranking (RANKX)" in tr.patterns:
            return "Ranking (needs review)", "High"
        if not hard and "Share of total (REMOVEFILTERS)" in tr.patterns:
            return "Share of total (needs review)", "High"
        return "Needs review", "High"
    if isinstance(ir, Lit):
        return "Constant", "Low"
    if isinstance(ir, Scalar) or any(isinstance(x, Scalar) for x in _walk_top(ir)):
        return "Filter-independent (ALL)", "Medium"
    aggs = aggs_in(ir)
    tables = {a.table for a in aggs}
    if graph and len(tables) > 1 and any(t2 in graph.reachable(t1) for t1 in tables for t2 in tables if t1 != t2):
        return "Mixed grain (fact + dimension)", "High"
    if graph and tables and all(graph.is_dimension(t) for t in tables):
        return "Dimension-grain aggregate", "Medium"
    if "Iterator (row by row)" in tr.patterns:
        return "Iterator (row by row)", "Medium"
    if isinstance(ir, Agg):
        if ir.pred is not None:
            return "Filtered aggregate (CALCULATE)", "Low"
        return ("Distinct count" if ir.distinct else "Simple aggregate"), "Low"
    if isinstance(ir, SafeDiv) or (isinstance(ir, Bin) and ir.op == "/"):
        return "Ratio of aggregates", "Low"
    if isinstance(ir, Case):
        return "Conditional", "Medium"
    if isinstance(ir, (BlankArith, Bin)):
        return "Composite (arithmetic)", "Low"
    return "Expression", "Medium"


def _walk_top(ir):
    from .ir import walk as _w
    return _w(ir, into_scalar=False)
