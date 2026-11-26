"""Spark SQL for Incorta materialized views.

Two kinds of MV come straight from the Power BI model:
- a DAX calculated table (CALENDAR / ADDCOLUMNS / GENERATESERIES) becomes an MV that generates the same rows;
- a table whose calculated columns cannot be physical formula columns (a per-row aggregate such as a median per
  currency and month, or a long chain of columns that use each other) becomes an MV over the loaded table, one
  CTE per dependency level, so every column reads the columns computed before it.

Expressions come from the same DAX → IR translation as the rest of the app, rendered in the Spark dialect.
"""
from __future__ import annotations

import re
import textwrap

from sqlglot import exp

from ..analyze.dax_parser import DaxSyntaxError, parse_dax
from ..analyze.dax_translate import Translator, _Ctx
from ..analyze.ir import Col, Null, OuterCol, cols_in, reviews_in
from ..analyze.sqlrender import Renderer, column, literal, table_ref

DIALECT = "spark"
BOUNDS = "__bounds__"


def sid(name: str) -> str:
    return column(name).sql(dialect=DIALECT)


def one_line(text: str, limit: int = 220) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 3] + "..."


def snake(name: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", str(name).lower()).strip("_") or "t"


class SparkRender:
    """Renderer factory bound to the Incorta names of the plan."""

    def __init__(self, names, host: str):
        self.names = names
        self.host = host

    def col(self, c, alias: str | None = None) -> exp.Expression:
        if c.table == BOUNDS:
            return column(c.column, "b")
        if c.table == self.host:
            return column(self.names.column(c.table, c.column), alias)
        e = exp.Null()
        e.add_comments([f" [MANUAL CHECK REQUIRED] {c.table}[{c.column}] needs a join to {c.table} "])
        return e

    def lookup(self, n, rnd) -> exp.Expression:
        lt = n.result.table
        where = None
        for key, value in n.keys:
            cond = exp.EQ(this=column(self.names.column(lt, key.column), "lk"), expression=rnd.r(value))
            where = cond if where is None else exp.And(this=where, expression=cond)
        sub = exp.select(exp.Max(this=column(self.names.column(lt, n.result.column), "lk"))) \
            .from_(table_ref(self.names.object(lt), "lk"))
        if where is not None:
            sub = sub.where(where)
        e = exp.Subquery(this=sub)
        if n.default is not None and not isinstance(n.default, Null):
            e = exp.Coalesce(this=e, expressions=[rnd.r(n.default)])
        return e

    def corr(self, n, rnd) -> exp.Expression:
        """Aggregate of another table for each row: a (correlated) scalar subquery."""
        inner = Renderer(DIALECT, col_fn=lambda c: column(self.names.column(c.table, c.column), "i"))
        outer = Renderer(DIALECT, col_fn=lambda c: column(self.names.column(c.table, c.column), "src"))
        inner.lookup_fn = self.lookup
        x = exp.Star() if n.arg.__class__.__name__ == "Star" else inner.r(n.arg)
        if n.inner is not None:
            x = exp.Case(ifs=[exp.If(this=inner.r(n.inner), true=literal(1) if isinstance(x, exp.Star) else x)])
        from ..analyze.sqlrender import AGG_CLS
        if n.func == "COUNT":
            agg = exp.Count(this=exp.Distinct(expressions=[x]) if n.distinct else x)
        elif n.func == "MEDIAN":
            agg = exp.Anonymous(this="percentile", expressions=[x, literal(0.5)])
        else:
            agg = AGG_CLS[n.func](this=x)
        cols = [c.column for c in cols_in(n.arg) + cols_in(n.inner) + [k for i, _ in n.keys for k in cols_in(i)]
                if c.table == n.table]
        sub = exp.select(agg).from_(table_ref(self.names.object_for(n.table, cols), "i"))
        where = None
        for i, o in n.keys:
            cond = exp.EQ(this=inner.r(i), expression=outer.r(_outer_as_col(o)))
            where = cond if where is None else exp.And(this=where, expression=cond)
        if where is not None:
            sub = sub.where(where)
        return exp.Subquery(this=sub)

    def renderer(self) -> Renderer:
        r = Renderer(DIALECT, col_fn=self.col)
        r.lookup_fn = self.lookup
        r.corr_fn = self.corr
        return r


def _outer_as_col(node):
    from ..analyze.ir import transform
    return transform(node, lambda n: Col(n.table, n.column) if isinstance(n, OuterCol) else n)


def render(e: exp.Expression, indent: int = 4) -> str:
    text = e.sql(dialect=DIALECT, pretty=True)
    return textwrap.indent(text, " " * indent).lstrip()


# ------------------------------------------------------------------------------------------ calculated tables
def calc_table_sql(table: str, dax: str, translator: Translator, names) -> dict:
    """Spark SQL for a DAX calculated table. Returns {"sql", "columns", "notes", "reviews"}."""
    notes, reviews = [], []
    try:
        ast = parse_dax(dax or "")
    except DaxSyntaxError as ex:
        return {"sql": "", "columns": [], "notes": [], "reviews": [f"DAX could not be parsed: {ex}"]}
    var_defs, body = ([], ast)
    if ast[0] == "var":
        var_defs, body = ast[1], ast[2]
    sr = SparkRender(names, table)
    ctes, var_ctx = [], tuple((name, Col(BOUNDS, name)) for name, _ in var_defs)

    # VARs: scalar values computed once (a single-row CTE)
    if var_defs:
        items, tables = [], set()
        for name, e in var_defs:
            ir = translator.tr(e, _Ctx(host=table))
            reviews += reviews_in(ir)
            from ..analyze.ir import aggs_in
            tables |= {a.table for a in aggs_in(ir)}
            items.append((name, ir))
        if len(tables) == 1:
            src = next(iter(tables))
            r = Renderer(DIALECT, col_fn=lambda c: column(names.column(c.table, c.column)))
            select = ",\n    ".join(f"{render(r.r(ir))} AS {sid(name)}" for name, ir in items)
            used = [c.column for _, ir in items for c in cols_in(ir) if c.table == src]
            ctes.append(("bounds", f"SELECT\n    {select}\n  FROM {names.object_for(src, used)}"))
        else:
            r = Renderer(DIALECT, col_fn=lambda c: column(names.column(c.table, c.column)))
            select = ",\n    ".join(f"{render(r.r(ir))} AS {sid(name)}" for name, ir in items)
            ctes.append(("bounds", f"SELECT\n    {select}"))
            if len(tables) > 1:
                reviews.append("VAR values read several tables: give each aggregate its own FROM")

    def arg_sql(node) -> str:
        if node[0] == "name" and any(n == node[1] for n, _ in var_defs):
            return f"b.{sid(node[1])}"
        ir = translator.tr(node, _Ctx(host=table, vars=var_ctx))
        reviews.extend(reviews_in(ir))
        return render(sr.renderer().r(ir))

    added = []
    base = body
    if body[0] == "func" and body[1] == "ADDCOLUMNS" and body[2]:
        base = body[2][0]
        rest = body[2][1:]
        for i in range(0, len(rest) - 1, 2):
            if rest[i][0] == "str":
                added.append((rest[i][1], rest[i + 1]))
    from_bounds = " FROM bounds AS b" if var_defs else ""
    if base[0] == "func" and base[1] == "CALENDAR" and len(base[2]) == 2:
        a, b = arg_sql(base[2][0]), arg_sql(base[2][1])
        ctes.append(("calendar", f"SELECT explode(sequence({a}, {b}, INTERVAL 1 DAY)) AS {sid('Date')}{from_bounds}"))
        base_cols = [("Date", "Date")]
    elif base[0] == "func" and base[1] == "CALENDARAUTO":
        reviews.append("CALENDARAUTO(): set the first and last date from the fact table dates")
        ctes.append(("calendar", "SELECT explode(sequence(DATE '2000-01-01', DATE '2030-12-31', INTERVAL 1 DAY)) AS "
                     f"{sid('Date')} -- [MANUAL CHECK REQUIRED] replace with the real date range"))
        base_cols = [("Date", "Date")]
    elif base[0] == "func" and base[1] == "GENERATESERIES" and len(base[2]) >= 2:
        args = [arg_sql(x) for x in base[2]]
        step = args[2] if len(args) > 2 else "1"
        ctes.append(("series", f"SELECT explode(sequence({args[0]}, {args[1]}, {step})) AS {sid('Value')}{from_bounds}"))
        base_cols = [("Value", "Value")]
    else:
        reviews.append(f"calculated table {body[1] if body[0] == 'func' else body[0]}(...) is not generated "
                       f"automatically: write the MV SQL by hand")
        return {"sql": "", "columns": [], "notes": notes, "reviews": reviews}

    # model columns of the table (names may differ from the DAX names, e.g. GENERATESERIES "Value")
    model_cols = [c for c in translator.idx.model.get("columns", []) if c["table"] == table]
    by_source = {str(c.get("source_column") or "").strip("[]").lower(): c["column"] for c in model_cols}
    select = []
    for dax_name, incorta_src in base_cols:
        pbi = by_source.get(dax_name.lower(), dax_name)
        select.append((pbi, f"{sid(incorta_src)}" + ("" if names.column(table, pbi) == incorta_src
                                                     else f" AS {sid(names.column(table, pbi))}")))
    ctx = _Ctx(host=table, mode="row", row_tables=(table,), calc=True, vars=var_ctx)
    for name, node in added:
        before = len(translator.acc.reviews)
        ir = translator.tr(node, ctx)
        reviews.extend(reviews_in(ir))
        reviews.extend(translator.acc.reviews[before:])
        pbi = by_source.get(name.lower(), name)
        expr = render(sr.renderer().r(ir), 2)
        comment = ""
        if node[0] == "func" and node[1] == "WEEKNUM":
            comment = "DAX WEEKNUM: week 1 holds 1 January (not ISO weekofyear)"
        select.append((pbi, f"{expr} AS {sid(names.column(table, pbi))}", comment))
    src_cte = ctes[-1][0]
    select = [s if len(s) == 3 else (s[0], s[1], "") for s in select]
    body_sql = "SELECT\n" + _join_items([(s, c) for _, s, c in select], "  ") + f"\nFROM {src_cte}"
    sql = "WITH\n" + ",\n".join(f"{n} AS (\n  {q}\n)" for n, q in ctes) + "\n" + body_sql
    return {"sql": sql, "columns": [p for p, _, _ in select], "notes": notes, "reviews": sorted(set(reviews))}


def _join_items(items: list[tuple], indent: str) -> str:
    """SELECT items with the comma before any trailing comment."""
    out = []
    for i, (text, comment) in enumerate(items):
        sep = "," if i < len(items) - 1 else ""
        out.append(f"{indent}{text}{sep}" + (f"  -- {comment}" if comment else ""))
    return "\n".join(out)


# ------------------------------------------------------------------------------------------ table MVs
def levels(calc_cols: list[str], deps: dict[str, list[str]]) -> list[list[str]]:
    """Group calculated columns by dependency depth (columns of one level only read earlier levels)."""
    level: dict[str, int] = {}

    def depth(c, seen=()):
        if c in level:
            return level[c]
        if c in seen:
            return 0
        d = 1 + max([depth(x, seen + (c,)) for x in deps.get(c, []) if x in calc_cols] or [0])
        level[c] = d
        return d

    for c in calc_cols:
        depth(c)
    out: dict[int, list[str]] = {}
    for c in calc_cols:
        out.setdefault(level[c], []).append(c)
    return [out[k] for k in sorted(out)]


def table_mv_sql(table: str, source_cols: list[str], calc_cols: list[str], deps: dict, translator: Translator,
                 names, source_label: str, types: dict | None = None) -> dict:
    """Spark SQL computing the calculated columns of `table` over its loaded Incorta table. Date columns are cast
    to DATE so joins to a date MV compare the same type."""
    sr = SparkRender(names, table)
    reviews, notes = [], []
    ds = f"ds_source_{snake(table)}"
    types = types or {}

    def src_item(c):
        n = sid(names.column(table, c))
        return f"CAST({n} AS DATE) AS {n}" if types.get(c) == "date" else n

    src_select = ",\n    ".join(src_item(c) for c in source_cols)
    ctes = [(ds, f"-- {source_label}\n  SELECT\n    {src_select}\n  FROM {names.object_raw(table)}")]
    prev = ds
    for i, level in enumerate(levels(calc_cols, deps), 1):
        items = []
        for c in level:
            tr = translator.calc_column(table, c)
            dax = (translator.idx.columns.get((table, c)) or {}).get("dax", "")
            expr = render(sr.renderer().r(tr.ir)) if tr.ir is not None else "NULL"
            flag = ""
            if tr.reviews:
                reviews += [f"{c}: {r}" for r in tr.reviews]
                flag = "[MANUAL CHECK REQUIRED] " + "; ".join(tr.reviews)
            items.append((f"-- {c}  |  DAX: {one_line(dax)}\n    {expr} AS {sid(names.column(table, c))}", flag))
            notes += [f"{c}: {n}" for n in tr.notes]
        name = f"calc_{snake(table)}_{i}"
        what = "read the source columns" if i == 1 else f"read the columns of step {i - 1}"
        ctes.append((name, f"-- Step {i}: calculated columns that {what}\n  SELECT\n    src.*,\n"
                     + _join_items(items, "    ") + f"\n  FROM {prev} AS src"))
        prev = name
    out_cols = [names.column(table, c) for c in source_cols + calc_cols]
    body = "SELECT\n  " + ",\n  ".join(sid(c) for c in out_cols) + f"\nFROM {prev}"
    sql = "WITH\n" + ",\n".join(f"{n} AS (\n  {q}\n)" for n, q in ctes) + "\n" + body
    return {"sql": sql, "columns": source_cols + calc_cols, "notes": sorted(set(notes)), "reviews": reviews}
