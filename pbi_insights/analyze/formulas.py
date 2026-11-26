"""Draft calculated-field formulas for Tableau, QuickSight and Incorta, rendered from the same IR as the SQL.

These are starting points (labelled 'draft' in the workbook): names, quoting and null handling differ by
tool version, so check each in the tool before relying on it.

Incorta specifics (docs.incorta.com, built-in functions): strings in double quotes, `!=`, `and()` / `or()`,
`lookup(result, key field, key value, ..., default)`, time series as an extra aggregation argument
(`sum(x, ago(date, 1, "Month"))`, `sum(x, toDate(date, "Year"))`), level-based measures
(`sum(x, groupBy(dim, ...))`) and `rank()` / `denseRank(groupBy(...), orderBy(agg, true|false))`.
"""
from __future__ import annotations

from .ir import (Agg, BlankArith, Bin, Case, Col, CorrAgg, Fn, InList, IsNull, Lit, Lookup, Null, OuterCol, Rank,
                 Review, SafeDiv, Scalar, Star, TimeShift, Total, Un, context_values)

AGG_NAMES = {
    "tableau": {"SUM": "SUM", "AVG": "AVG", "MIN": "MIN", "MAX": "MAX", "COUNT": "COUNT", "MEDIAN": "MEDIAN"},
    "quicksight": {"SUM": "sum", "AVG": "avg", "MIN": "min", "MAX": "max", "COUNT": "count", "MEDIAN": "median"},
    "incorta": {"SUM": "sum", "AVG": "average", "MIN": "min", "MAX": "max", "COUNT": "count", "MEDIAN": "median"},
}
MAX_LEN = 700


class _Unsupported(Exception):
    pass


def _unparen(s: str) -> str:
    """'(a = b)' → 'a = b' when the outer parentheses wrap the whole text."""
    if not (s.startswith("(") and s.endswith(")")):
        return s
    depth, q = 0, None
    for i, ch in enumerate(s):
        if q:
            if ch == q and s[i - 1] != "\\":
                q = None
            continue
        if ch in "\"'`":
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and i < len(s) - 1:
                return s
    return s[1:-1]


def _is_one_value_guard(cond) -> bool:
    """HASONEVALUE(col) → COUNT(DISTINCT col) = 1: Power BI's way of hiding a value on total rows."""
    return (isinstance(cond, Bin) and cond.op == "=" and isinstance(cond.left, Agg) and cond.left.distinct
            and cond.left.func == "COUNT" and isinstance(cond.right, Lit) and cond.right.value == 1)


class ToolFormula:
    """IR → one tool's formula text.

    `namer(table, column)` decides how a column is written (Incorta: SCHEMA.TABLE.COLUMN); `key_of(table)`
    names the column counted for COUNTROWS; `level_dims` / `rank_dims` are the insight's grouping columns
    used for groupBy() in level-based totals and ranks (empty: groupBy() with no columns)."""

    def __init__(self, tool: str, namer=None, key_of=None, level_dims=None, rank_dims=None, typer=None):
        self.tool = tool
        self.typer = typer                 # (table, column) -> "string" / "long" / ... for casts in concat()
        self.namer = namer
        self.key_of = key_of
        self.level_dims = level_dims
        self.rank_dims = rank_dims
        self.mods: list[str] = []          # extra aggregation arguments (ago / toDate / groupBy)
        self.used: set[str] = set()        # Incorta features used: ago, toDate, groupBy, rank, lookup

    # ---- leaves -------------------------------------------------------------------------------------------
    def col(self, c) -> str:
        if self.namer:
            return self.namer(c.table, c.column)
        if self.tool == "tableau":
            return f"[{c.column}]"
        if self.tool == "quicksight":
            return "{" + c.column + "}"
        return f"{c.table}.{c.column}" if " " not in c.column else f"{c.table}.`{c.column}`"

    def lit(self, v) -> str:
        if v is None:
            return "NULL" if self.tool != "incorta" else "null"
        if isinstance(v, bool):
            return ("TRUE" if v else "FALSE") if self.tool == "tableau" else ("true" if v else "false")
        if isinstance(v, (int, float)):
            return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
        if self.tool == "incorta":
            return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
        return "'" + str(v).replace("'", "\\'") + "'"

    # ---- tree ---------------------------------------------------------------------------------------------
    def r(self, n) -> str:
        t = self.tool
        if isinstance(n, Col):
            return self.col(n)
        if isinstance(n, Lit):
            return self.lit(n.value)
        if isinstance(n, Null):
            return self.lit(None)
        if isinstance(n, Agg):
            return self.agg(n)
        if isinstance(n, Bin):
            op = n.op
            if op in ("AND", "OR"):
                if t == "incorta":
                    parts = self._flatten(n, op)
                    return f"{op.lower()}({', '.join(_unparen(self.r(x)) for x in parts)})"
                return f"({self.r(n.left)} {op} {self.r(n.right)})"
            if t == "incorta" and op in ("=", "<>") and isinstance(n.left, Fn) and n.left.name == "COALESCE" \
                    and isinstance(n.right, Lit) and len(n.left.args) == 2 and isinstance(n.left.args[1], Lit) \
                    and n.left.args[1].value == n.right.value and not isinstance(n.left.args[0], Lookup):
                x, v = self.r(n.left.args[0]), self.lit(n.right.value)
                return f"or(isNull({x}), {x} = {v})" if op == "=" else f"and(not(isNull({x})), {x} != {v})"
            if op == "<>" and t == "incorta":
                op = "!="
            return f"({self.r(n.left)} {op} {self.r(n.right)})"
        if isinstance(n, Un):
            if n.op == "NEG":
                return f"-{self.r(n.x)}"
            return f"not({self.r(n.x)})" if t == "incorta" else f"NOT {self.r(n.x)}"
        if isinstance(n, Case):
            return self.case(n)
        if isinstance(n, SafeDiv):
            a, b = self.r(n.num), self.r(n.den)
            alt = self.r(n.alt) if n.alt is not None else self.lit(None)
            if t == "quicksight":
                return f"ifelse({b} <> 0, {a} / {b}, {alt})"
            if t == "incorta":
                return f"if({_unparen(b)} != 0, {a} / {b}, {alt})"
            return f"{a} / {b}"
        if isinstance(n, BlankArith):
            a, b = self.r(n.left), self.r(n.right)
            if t == "tableau":
                return f"(ZN({a}) {n.op} ZN({b}))"
            if t == "quicksight":
                return f"(coalesce({a}, 0) {n.op} coalesce({b}, 0))"
            return f"({a} {n.op} {b})"
        if isinstance(n, IsNull):
            return f"ISNULL({self.r(n.x)})" if t == "tableau" else f"isNull({self.r(n.x)})"
        if isinstance(n, InList):
            vals = ", ".join(self.r(v) for v in n.values)
            if t == "tableau":
                return f"{self.r(n.x)} IN ({vals})"
            if t == "quicksight":
                return f"in({self.r(n.x)}, [{vals}])"
            return f"inList({self.r(n.x)}, {vals})"
        if isinstance(n, Fn):
            return self.fn(n)
        if isinstance(n, Scalar):
            inner = self.r(n.expr)
            if t == "tableau":
                return "{FIXED : " + inner + "}"
            return inner
        if isinstance(n, Lookup):
            return self.lookup(n)
        if isinstance(n, TimeShift):
            return self.time_shift(n)
        if isinstance(n, Total):
            return self.total(n)
        if isinstance(n, Rank):
            return self.rank(n)
        if isinstance(n, (Review, Star, CorrAgg, OuterCol)):
            raise _Unsupported()
        raise _Unsupported()

    @staticmethod
    def _flatten(n, op) -> list:
        if isinstance(n, Bin) and n.op == op:
            return ToolFormula._flatten(n.left, op) + ToolFormula._flatten(n.right, op)
        return [n]

    def case(self, n: Case) -> str:
        t = self.tool
        default = n.default if n.default is not None else Null()
        if (len(n.whens) == 1 and isinstance(default, Null) and _is_one_value_guard(n.whens[0][0])
                and isinstance(n.whens[0][1], Rank)):
            return self.r(n.whens[0][1])            # IF(HASONEVALUE(x), RANKX(...)): hide the total row instead
        if t == "tableau":
            parts = [f"IF {self.r(c)} THEN {self.r(v)}" if i == 0 else f"ELSEIF {self.r(c)} THEN {self.r(v)}"
                     for i, (c, v) in enumerate(n.whens)]
            if not isinstance(default, Null):
                parts.append(f"ELSE {self.r(default)}")
            return " ".join(parts) + " END"
        if t == "quicksight":
            args = [x for c, v in n.whens for x in (self.r(c), self.r(v))]
            return f"ifelse({', '.join(args)}, {self.r(default)})"
        out = self.r(default)
        for c, v in reversed(n.whens):
            out = f"if({_unparen(self.r(c))}, {self.r(v)}, {out})"
        return out

    def agg(self, a: Agg) -> str:
        t = self.tool
        if context_values(a):                              # a value of the visual inside an aggregate: no tool nests them
            raise _Unsupported()
        name = AGG_NAMES[t][a.func]
        if self.mods and t != "incorta":
            raise _Unsupported()
        extra = "".join(f", {m}" for m in self.mods)
        if isinstance(a.arg, Star):
            if t == "incorta":
                if a.pred is not None:
                    return f"sum(if({self.r(a.pred)}, 1, 0){extra})"
                key = self.key_of(a.table) if self.key_of else None
                return f"count({key or a.table + '.<key column>'}{extra})"
            key = {"tableau": f"[{a.table} (Count)]", "quicksight": "{" + f"{a.table} key" + "}"}[t]
            pred = f" where {self.r(a.pred)}" if a.pred is not None else ""
            return (f"SUM({key})" if t == "tableau" else f"count({key})") + pred
        x = self.r(a.arg)
        if t == "tableau":
            if a.pred is not None:
                x = f"IF {self.r(a.pred)} THEN {x} END"
            return f"COUNTD({x})" if a.distinct else f"{name}({x})"
        if t == "quicksight":
            if a.distinct:
                return f"distinct_countIf({x}, {self.r(a.pred)})" if a.pred is not None else f"distinct_count({x})"
            if a.pred is not None:
                return f"{name}If({x}, {self.r(a.pred)})"
            return f"{name}({x})"
        p = _unparen(self.r(a.pred)) if a.pred is not None else None
        if a.distinct:
            if p is not None:
                return f"distinct(if({p}, {x}, null){extra})"
            return f"distinct({x}{extra})"
        if p is not None:
            if a.func == "SUM":
                return f"sum(if({p}, {x}, 0){extra})"
            if a.func == "COUNT":
                return f"sum(if({p}, 1, 0){extra})"
            return f"{name}(if({p}, {x}, null){extra})"
        return f"{name}({x}{extra})"

    # ---- Incorta analytic constructs -------------------------------------------------------------------
    def lookup(self, n: Lookup) -> str:
        if self.tool != "incorta":
            raise _Unsupported()
        self.used.add("lookup")
        args = [self.col(n.result)]
        for k, v in n.keys:
            args += [self.col(k), self.r(v)]
        if n.default is not None and not isinstance(n.default, Null):
            args.append(self.r(n.default))
        return f"lookup({', '.join(args)})"

    def time_shift(self, n: TimeShift) -> str:
        if self.tool != "incorta":
            raise _Unsupported()
        d = self.col(n.date)
        if n.kind == "ago":
            self.used.add("ago")
            mod = f'ago({d}, {n.n}, "{n.part}")'
        else:
            self.used.add("toDate")
            mod = f'toDate({d}, "{n.part}")'
        self.mods.append(mod)
        try:
            return self.r(n.expr)
        finally:
            self.mods.pop()

    def total(self, n: Total) -> str:
        t = self.tool
        if t == "tableau" and n.columns and not n.tables:
            return "{EXCLUDE " + ", ".join(self.col(c) for c in n.columns) + " : " + self.r(n.expr) + "}"
        if t != "incorta":
            raise _Unsupported()
        self.used.add("groupBy")
        dims = self.level_dims(n) if callable(self.level_dims) else (self.level_dims or [])
        self.mods.append(f"groupBy({', '.join(dims)})")
        try:
            return self.r(n.expr)
        finally:
            self.mods.pop()

    def rank(self, n: Rank) -> str:
        t = self.tool
        inner = self.r(n.expr)
        if t == "tableau":
            return f"{'RANK_DENSE' if n.dense else 'RANK'}({inner}, '{'desc' if n.desc else 'asc'}')"
        if t == "quicksight":
            return f"{'denseRank' if n.dense else 'rank'}([{inner} {'DESC' if n.desc else 'ASC'}])"
        self.used.add("rank")
        dims = self.rank_dims(n) if callable(self.rank_dims) else (self.rank_dims or [])
        fn = "denseRank" if n.dense else "rank"
        return f"{fn}(groupBy({', '.join(dims)}), orderBy({inner}, {'false' if n.desc else 'true'}))"

    def fn(self, n: Fn) -> str:
        t = self.tool
        name = n.name
        if name == "COALESCE" and t == "incorta" and isinstance(n.args[0], Lookup) and n.args[0].default is None:
            return self.lookup(Lookup(n.args[0].result, n.args[0].keys, n.args[1]))
        a = [self.r(x) for x in n.args]
        if name == "COALESCE":
            if t == "tableau":
                return f"ZN({a[0]})" if len(a) == 2 and a[1] == "0" else f"IFNULL({a[0]}, {a[1]})"
            if t == "quicksight":
                return f"coalesce({', '.join(a)})"
            return f"if(isNull({a[0]}), {a[1]}, {a[0]})"
        if name == "CONCAT":
            if t == "tableau":
                return " + ".join(a)
            if t == "incorta":
                def needs_cast(arg):
                    if isinstance(arg, Lit):
                        return not isinstance(arg.value, str)
                    if isinstance(arg, Col):
                        return bool(self.typer) and self.typer(arg.table, arg.column) not in ("string", None)
                    return True
                parts = [f"string({x})" if needs_cast(arg) else x for arg, x in zip(n.args, a)]
                return f"concat({', '.join(parts)})"
            return f"concat({', '.join(a)})"
        if name in ("YEAR", "MONTH", "DAY", "QUARTER"):
            if t == "quicksight":
                part = {"YEAR": "YYYY", "MONTH": "MM", "DAY": "DD", "QUARTER": "Q"}[name]
                return f"extract('{part}', {a[0]})"
            if t == "tableau":
                return f"{name}({a[0]})" if name != "QUARTER" else f"DATEPART('quarter', {a[0]})"
            return f"{name.lower()}({a[0]})"
        if name == "TIMEFMT":
            fmt = n.args[1].value if isinstance(n.args[1], Lit) else ""
            java = fmt.replace("%Y", "yyyy").replace("%y", "yy").replace("%B", "MMMM").replace("%b", "MMM") \
                .replace("%m", "MM").replace("%d", "dd").replace("%A", "EEEE").replace("%a", "EEE")
            if t == "tableau":
                if fmt == "%b":
                    return f"LEFT(DATENAME('month', {a[0]}), 3)"
                if fmt == "%B":
                    return f"DATENAME('month', {a[0]})"
                return f"DATENAME('{java}', {a[0]})"
            if t == "incorta":
                return f'formatDate({a[0]}, "{java}")'
            return f"formatDate({a[0]}, '{java}')"
        if name in ("TOSTRING", "NUMFMT") and t == "incorta":
            return f"string({a[0]})"
        if name == "ROUND":
            return f"{'ROUND' if t == 'tableau' else 'round'}({', '.join(a)})"
        if name in ("ABS", "FLOOR", "CEIL", "SQRT", "UPPER", "LOWER", "TRIM", "LENGTH", "LEFT", "RIGHT"):
            fname = {"LENGTH": "LEN" if t == "tableau" else "strlen", "CEIL": "CEILING" if t == "tableau" else "ceil"}.get(name, name)
            return f"{fname if t == 'tableau' else fname.lower()}({', '.join(a)})"
        if name.startswith("LIKE_"):
            if t == "tableau":
                fn = {"LIKE_CONTAINS": "CONTAINS", "LIKE_STARTS": "STARTSWITH", "LIKE_ENDS": "ENDSWITH"}[name]
                return f"{fn}({a[0]}, {a[1]})"
            fn = {"LIKE_CONTAINS": "contains", "LIKE_STARTS": "startsWith", "LIKE_ENDS": "endsWith"}[name]
            return f"{fn}({a[0]}, {a[1]})"
        raise _Unsupported()


def tool_formulas(ir) -> dict:
    out = {}
    for tool in ("tableau", "quicksight", "incorta"):
        try:
            text = ToolFormula(tool).r(ir) if ir is not None else ""
            out[tool] = text if len(text) <= MAX_LEN else "(long expression: build from the SQL)"
        except _Unsupported:
            out[tool] = "(not expressible as a simple calculated field: use the SQL)"
        except Exception:  # noqa: BLE001
            out[tool] = "(see the SQL)"
    return out
