"""Build the reference SQL for one insight (visual) from its dimensions, measures, filters and sort.

Grain rule (this is what keeps the SQL faithful to Power BI):
  * every aggregate is computed on its own table (`Agg.table`), joined only to tables that table can
    reach through many-to-one relationships (the tables that can filter it in Power BI);
  * if all aggregates live on one table and that table reaches every dimension, a single GROUP BY
    query is enough;
  * otherwise each table gets its own grouped CTE and the CTEs are joined on the shared dimensions
    (e.g. sales from the fact, selling area from the store dimension), so nothing is double counted;
  * filters a table cannot reach are not applied to it, which is how one-way filtering behaves.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

import sqlglot
from sqlglot import exp

from .graph import RelGraph
from .ir import (Agg, BlankArith, Bin, Case, Col, Fn, Lit, Null, Review, SafeDiv, Scalar, Total, Un, children, cols_in,
                 context_values, reviews_in, row_cols, top_aggs, windowable_total)
from .sqlrender import Renderer, column, literal, not_null, snake, table_ref

DEFAULT_VIEW_PREFIX = "vw_"


@dataclass
class Dim:
    table: str
    column: str
    display: str
    out: str = ""
    sort_by: str | None = None
    sort_out: str | None = None


@dataclass
class MeasureSpec:
    name: str
    out: str
    ir: object


@dataclass
class FilterSpec:
    table: str
    column: str
    ir: object
    text: str


@dataclass
class SortSpec:
    kind: str                 # "dim" | "measure"
    target: object            # Dim or MeasureSpec
    desc: bool = False


@dataclass
class QueryPlan:
    sql: str = ""
    form: str = "none"        # single | multi-grain | distinct | constant | none
    agg_tables: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    joins: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    reviews: list = field(default_factory=list)
    select: object = field(default=None, repr=False)    # the sqlglot query behind `sql` (grouped queries)


class Naming:
    def __init__(self, view_prefix: str = DEFAULT_VIEW_PREFIX):
        self.view_prefix = view_prefix

    def view(self, table: str) -> str:
        return f"{self.view_prefix}{snake(table)}"


class _Aliases:
    def __init__(self):
        self.by_table: dict[str, str] = {}

    def __call__(self, table: str) -> str:
        if table in self.by_table:
            return self.by_table[table]
        words = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", table) or [table]
        initials = "".join(w[0] for w in words).lower()
        base = re.sub(r"[^a-z0-9]", "", table.lower()) or "t"
        candidates = [initials] if len(words) > 1 else []
        candidates += [base[:n] for n in range(1, len(base) + 1)]
        used = set(self.by_table.values())
        alias = next((c for c in candidates if c and c not in used and not c[0].isdigit()), None)
        n = 2
        while alias is None or alias in used:
            alias = f"{(initials or base)[:3]}{n}"
            n += 1
        self.by_table[table] = alias
        return alias


class QueryBuilder:
    def __init__(self, graph: RelGraph, dialect: str = "tsql", naming: Naming | None = None, date_columns=None):
        self.graph = graph
        self.date_columns = date_columns            # {(table, column)} of date columns; None: not known
        self.dialect = dialect
        self.naming = naming or Naming()

    # ------------------------------------------------------------------------------------- helpers
    def _reach(self, table: str) -> set:
        return {table} | set(self.graph.reachable(table))

    def _from(self, sel: exp.Select, base: str, needed: set, al: _Aliases, plan: QueryPlan) -> exp.Select:
        sel = sel.from_(table_ref(self.naming.view(base), al(base)))
        for child, ccol, parent, pcol in self.graph.join_edges(base, needed):
            on = exp.EQ(this=column(ccol, al(child)), expression=column(pcol, al(parent)))
            sel = sel.join(table_ref(self.naming.view(parent), al(parent)), on=on, join_type="left")
            j = f"{child}[{ccol}] -> {parent}[{pcol}]"
            if j not in plan.joins:
                plan.joins.append(j)
        for t in [base] + [e[2] for e in self.graph.join_edges(base, needed)]:
            if t not in plan.tables:
                plan.tables.append(t)
        return sel

    def _renderer(self, al: _Aliases, agg_fn=None, ctx_fn=None) -> Renderer:
        return Renderer(self.dialect, col_fn=lambda c: column(c.column, al(c.table)), agg_fn=agg_fn,
                        scalar_fn=lambda s: self._scalar(s, al), ctx_fn=ctx_fn)

    def _scalar(self, s: Scalar, al: _Aliases) -> exp.Expression:
        """Filter-independent value: its own subquery over the table, with no dashboard filters."""
        return exp.Subquery(this=self._scalar_select(s, al))

    def _scalar_select(self, s: Scalar, al: _Aliases, name: str = "") -> exp.Select:
        expr = s.expr
        hoist = None
        if isinstance(expr, Agg) and expr.pred is not None:
            hoist, expr = expr.pred, replace(expr, pred=None)
        needed = {c.table for c in cols_in(s.expr)} - {s.table}
        sub_plan = QueryPlan()
        value = self._renderer(al).r(expr)
        sub = exp.select(exp.alias_(value, name) if name else value)
        sub = self._from(sub, s.table, needed, al, sub_plan)
        if hoist is not None:
            sub = sub.where(self._renderer(al).r(hoist))
        return sub

    # ---- values of the visual used inside an aggregate (SELECTEDVALUE in a CALCULATE filter ...) --------------------
    def _contexts(self, t: str, inf: dict, dims, filters, plan: QueryPlan, start: int) -> tuple[list, dict]:
        """One CTE per table such values come from, at the grain of the visual, to join to the rows of `t`.
        Returns ([(cte name, keys, select)], {repr(value): column})."""
        values = []
        for a in inf["aggs"]:
            values += [v for v in context_values(a) if v not in values]
        ctes, cols, by_table = [], {}, {}
        for v in values:
            if isinstance(v, Agg):
                by_table.setdefault(v.table, []).append(v)
                continue
            s = v if isinstance(v, Scalar) else Scalar(v.expr, v.independent)
            name = f"v{start + len(ctes)}_{snake(s.table)}"
            ctes.append((name, [], self._scalar_select(s, _Aliases(), "val")))
            cols[repr(v)] = column("val", name)
        for tc, aggs in by_table.items():
            reach = self._reach(tc)
            keys = [d for d in dims if d.table in reach]
            flt = [f for f in filters if {c.table for c in cols_in(f.ir)} <= reach]
            needed = {d.table for d in keys} | {c.table for f in flt for c in cols_in(f.ir)} | {
                c.table for a in aggs for c in cols_in(a)}
            if any(d not in inf["keys"] for d in keys) or not needed <= reach or any(context_values(a) for a in aggs):
                plan.reviews.append(f"a value of {tc} in the visual (SELECTEDVALUE, MAX ...) filters the {t} rows, but "
                                    f"{t} cannot be matched to it: compute it separately")
                continue
            al = _Aliases()
            rnd = self._renderer(al)
            al(tc)
            name = f"c{start + len(ctes)}_{snake(tc)}"
            out = [exp.alias_(column(d.column, al(d.table)), d.out) for d in keys]
            for k, a in enumerate(aggs, start=1):
                out.append(exp.alias_(rnd.r(a), f"val_{k}"))
                cols[repr(a)] = column(f"val_{k}", name)
            sel = self._from(exp.select(*out), tc, needed, al, plan)
            where = self._and([rnd.r(f.ir) for f in flt])
            if where is not None:
                sel = sel.where(where)
            if keys:
                sel = sel.group_by(*[column(d.column, al(d.table)) for d in keys])
            ctes.append((name, keys, sel))
        if ctes:
            plan.notes.append(f"A filter inside a measure uses a value of the visual (SELECTEDVALUE, MAX, a total ...): it is "
                              f"computed first in its own CTE and joined to the {t} rows.")
        return ctes, cols

    @staticmethod
    def _review(plan: QueryPlan, text: str):
        if text not in plan.reviews:
            plan.reviews.append(text)

    def _clears(self, a: Agg):
        """Which columns lose the visual's filters inside this aggregate: those its CALCULATE filters replace, and a
        whole date table when its date key is one of them."""
        nested = [v for v in context_values(a) if isinstance(v, Agg)]
        cols = {c for x in [a] + nested for c in x.replaced}
        keys = {(one, ocol) for edges in self.graph.parents.values() for one, _, ocol in edges}
        tables = {t for t, c in cols if (t, c) in keys and (self.date_columns is None or (t, c) in self.date_columns)}
        return lambda t, c: (t, c) in cols or t in tables

    @staticmethod
    def _ctx_fn(cols: dict):
        def fn(v):
            c = cols.get(repr(v))
            return c.copy() if c is not None else Renderer.review_null("a value of the visual inside an aggregate")
        return fn

    def _join_contexts(self, sel: exp.Select, ctes: list, al: _Aliases) -> exp.Select:
        for name, keys, _ in ctes:
            if not keys:
                sel = sel.join(exp.to_table(name), join_type="cross")
                continue
            on = [_same(column(d.column, al(d.table)), column(d.out, name)) for d in keys]
            sel = sel.join(exp.to_table(name), on=self._and(on), join_type="left")
        return sel

    @staticmethod
    def _and(conds: list) -> exp.Expression | None:
        out = None
        for c in conds:
            out = c if out is None else exp.And(this=out, expression=c)
        return out

    def ordered(self, e: exp.Expression, desc: bool = False) -> exp.Ordered:
        """ORDER BY term using the dialect's own NULL ordering, so no NULLS FIRST/LAST emulation is printed."""
        try:
            null_ordering = sqlglot.Dialect.get_or_raise(self.dialect).NULL_ORDERING
        except Exception:  # noqa: BLE001
            null_ordering = "nulls_are_small"
        if null_ordering == "nulls_are_small":
            nulls_first = not desc
        elif null_ordering == "nulls_are_large":
            nulls_first = desc
        else:
            nulls_first = False
        return exp.Ordered(this=e, desc=desc, nulls_first=nulls_first)

    def _order(self, sorts: list[SortSpec], dim_expr, meas_expr) -> list:
        out = []
        for s in sorts:
            if s.kind == "dim":
                e = dim_expr(s.target, sort_key=True)
            else:
                e = meas_expr(s.target)
            if e is not None:
                out.append(self.ordered(e, s.desc))
        return out

    # ------------------------------------------------------------------------------------- main
    def build(self, dims: list[Dim], measures: list[MeasureSpec], filters: list[FilterSpec],
              sorts: list[SortSpec]) -> QueryPlan:
        plan = QueryPlan()
        self._name_outputs(dims, measures)
        for m in measures:
            if m.ir is None:                              # DAX that did not parse: render NULL, flag for review
                m.ir = Review(f"measure [{m.name}] could not be translated")
        for m in measures:
            plan.reviews += [r for r in reviews_in(m.ir) if r not in plan.reviews]
        if not measures:
            return self._distinct(dims, filters, sorts, plan)
        if any(_window_totals(m.ir) for m in measures):
            return self._windowed(dims, measures, filters, sorts, plan)
        return self._grouped(dims, measures, filters, sorts, plan)

    def _grouped(self, dims, measures, filters, sorts, plan: QueryPlan, hide_blank: bool = True) -> QueryPlan:
        groups: dict[str, list[Agg]] = {}
        for m in measures:
            for a in top_aggs(m.ir):
                groups.setdefault(a.table, [])
                if a not in groups[a.table]:
                    groups[a.table].append(a)
        plan.agg_tables = list(groups)
        for m in measures if dims and groups else ():
            if _has_fixed_value(m.ir) and not _blank_without_rows(m.ir):
                self._review(plan, f"[{m.name}] has a value for groups without rows (it uses a total that ignores the "
                                   f"filters): Power BI shows those groups too, the SQL does not")
        if not groups:
            return self._constant(dims, measures, filters, sorts, plan)
        info = {}
        for t, aggs in groups.items():
            reach = self._reach(t)
            keys = [d for d in dims if d.table in reach]
            flt = [f for f in filters if {c.table for c in cols_in(f.ir)} <= reach]
            for f in filters:
                if f not in flt:
                    plan.notes.append(f"Filter {f.text} does not reach {t} (one-way relationship), so Power BI does "
                                      f"not apply it to the {t} figures; neither does the SQL.")
            for d in dims:
                if d not in keys:
                    plan.notes.append(f"{d.table}[{d.column}] is not related to {t}: Power BI repeats the same {t} "
                                      f"value on every {d.column} row.")
            needed = {d.table for d in keys} | {c.table for f in flt for c in cols_in(f.ir)}
            for a in aggs:
                rows = _row_columns(a)
                needed |= {c.table for c in rows}
                clears = self._clears(a)
                for f in flt:
                    if any(clears(c.table, c.column) for c in cols_in(f.ir)):
                        plan.notes.append(f"A measure's CALCULATE filter overrides the visual filter {f.text} in "
                                          f"Power BI; the SQL applies both. Check if results differ.")
                        self._review(plan, f"a CALCULATE filter of the measure replaces the filter {f.text}: the SQL "
                                           f"applies both")
                for d in dims:
                    if clears(d.table, d.column) or (d.sort_by and clears(d.table, d.sort_by)):
                        self._review(plan, f"a CALCULATE filter of the measure replaces the visual's own grouping by "
                                           f"{d.table}[{d.column}] (other period, running total or fixed value): the SQL "
                                           f"only sees the rows of each group")
            missing = needed - reach
            if missing:
                plan.reviews.append(f"aggregate over {t} uses columns of {', '.join(sorted(missing))}, "
                                    f"which {t} cannot reach through relationships")
            info[t] = {"aggs": aggs, "keys": keys, "filters": flt, "needed": needed & reach}
            made = sum(len(x["ctx"]) for x in info.values() if "ctx" in x)
            info[t]["ctx"], info[t]["ctx_cols"] = self._contexts(t, info[t], dims, filters, plan, made + 1)
        if len(groups) == 1:
            t = next(iter(groups))
            if len(info[t]["keys"]) == len(dims):
                return self._single(t, info[t], dims, measures, sorts, plan, hide_blank)
        return self._multi(info, dims, measures, sorts, plan, hide_blank)

    # ---- totals at a coarser grain (ALL / REMOVEFILTERS / ALLSELECTED): group first, then a window ----------------
    def _removes(self, t: Total):
        """(table, column) -> does the total remove the filters / grouping on this column?"""
        tables = set()
        for x in t.tables:
            tables |= self._reach(x) if x != "*" else set()
        return lambda table, col: "*" in t.tables or table in tables or Col(table, col) in t.columns

    def _windowed(self, dims, measures, filters, sorts, plan: QueryPlan) -> QueryPlan:
        """Inner query: one row per group of the visual with every aggregate as a column. Outer query: the measures,
        each aggregate of a total at a coarser grain as SUM / MIN / MAX(column) OVER (PARTITION BY the visual's
        dimensions except those the total removes)."""
        totals = [t for m in measures for t in _window_totals(m.ir)]
        for t in totals:
            if t.kind == "ALLSELECTED":                    # slicers stay by design
                continue
            removes = self._removes(t)
            for f in filters:
                hit = next((c for c in cols_in(f.ir) if removes(c.table, c.column)), None)
                if hit is not None:
                    level = f.text.split(":", 1)[0].strip().lower()
                    what = "slicer" if level == "slicer" else f"{level} filter"
                    self._review(plan, f"Power BI's total ignores the {what} on {hit.table}[{hit.column}]; compute the "
                                       f"total in a CTE without that filter.")
        inner, cols = [], {}
        for m in measures:
            for a in top_aggs(m.ir):
                if repr(a) not in cols:
                    spec = MeasureSpec(name=f"agg_{len(inner) + 1}", out=f"agg_{len(inner) + 1}", ir=a)
                    inner.append(spec)
                    cols[repr(a)] = spec
        for m in measures if dims else ():
            if _has_fixed_value(m.ir) and not _blank_without_rows(m.ir):
                self._review(plan, f"[{m.name}] has a value for groups without rows (it uses a total that does not "
                                   f"come from the group's own rows): Power BI shows those groups too, the SQL does not")
        self._name_outputs(dims, inner)                    # same dimension names as the measures got in build()
        sub = self._grouped(dims, inner, filters, [], plan, hide_blank=False)
        if sub.select is None:                             # no grouped query (should not happen: totals hold aggregates)
            return sub
        key = "with_" if "with_" in exp.Select.arg_types else "with"
        with_ = sub.select.args.get(key)                   # CTEs of the grouped query move to the top
        if with_:
            sub.select.set(key, None)
        al = _Aliases()

        def part(t: Total):
            removes = self._removes(t)
            return [column(d.out, "q") for d in dims if not removes(d.table, d.column)]

        rnd = Renderer(self.dialect, col_fn=lambda c: column(c.column, al(c.table)),
                       agg_fn=lambda a: column(cols[repr(a)].out, "q") if repr(a) in cols
                       else Renderer.review_null("an aggregate missing from the grouped query"),
                       scalar_fn=lambda s: self._scalar(s, al), total_fn=part)
        out_cols = []
        for d in dims:
            out_cols.append(column(d.out, "q"))
            if d.sort_by:
                out_cols.append(column(d.sort_out, "q"))
        out_cols += [exp.alias_(rnd.r(m.ir), m.out) for m in measures]
        sel = exp.select(*out_cols).from_(sub.select.subquery("q"))
        src = None
        if dims and _hide_blank_rows(measures):            # a window cannot sit in WHERE: filter one level up
            sel = exp.select(*[column(c.alias_or_name, "w") for c in sel.selects]).from_(sel.subquery("w"))
            sel = sel.where(_or([not_null(column(m.out, "w")) for m in measures]))
            src = "w"
        order = self._order(sorts, lambda d, sort_key=False: column(d.sort_out if sort_key and d.sort_out else d.out,
                                                                     src or "q"),
                            lambda m: column(m.out, src) if src else column(m.out))
        if order:
            sel = sel.order_by(*order)
        if with_:
            sel.set(key, with_)
        plan.form = "multi-grain"
        plan.notes.append("A measure takes a total at a coarser grain (ALL / REMOVEFILTERS / ALLSELECTED): the query "
                          "groups by the visual's dimensions first, then a window adds the groups up.")
        plan.select = sel
        plan.sql = sel.sql(dialect=self.dialect, pretty=True)
        return plan

    def _name_outputs(self, dims: list[Dim], measures: list[MeasureSpec]):
        used = set()
        for d in dims:
            name = d.column if d.column not in used else snake(f"{d.table}_{d.column}")
            d.out = name
            used.add(name.lower())
            if d.sort_by:
                sname = d.sort_by if d.sort_by.lower() not in used else snake(f"{d.column}_sort")
                d.sort_out = sname
                used.add(sname.lower())
        for m in measures:
            base, n = m.out or snake(m.name), 2
            name = base
            while name.lower() in used:
                name = f"{base}_{n}"
                n += 1
            m.out = name
            used.add(name.lower())

    # ---- single table: one GROUP BY -------------------------------------------------------------
    def _single(self, t, inf, dims, measures, sorts, plan: QueryPlan, hide_blank: bool = True) -> QueryPlan:
        al = _Aliases()
        al(t)
        rnd = self._renderer(al, ctx_fn=self._ctx_fn(inf["ctx_cols"]))
        cols = []
        group = []
        for d in dims:
            c = column(d.column, al(d.table))
            cols.append(c if d.out == d.column else exp.alias_(c, d.out, quoted=None))
            group.append(column(d.column, al(d.table)))
            if d.sort_by:
                s = column(d.sort_by, al(d.table))
                cols.append(s if d.sort_out == d.sort_by else exp.alias_(s, d.sort_out))
                group.append(column(d.sort_by, al(d.table)))
        meas_exprs = {m.out: rnd.r(m.ir) for m in measures}
        cols += [exp.alias_(e, m.out) for m, e in zip(measures, meas_exprs.values())]
        sel = self._from(exp.select(*cols), t, inf["needed"], al, plan)
        sel = self._join_contexts(sel, inf["ctx"], al)
        where = self._and([rnd.r(f.ir) for f in inf["filters"]])
        if where is not None:
            sel = sel.where(where)
        if group:
            sel = sel.group_by(*group)
        order = self._order(sorts,
                            lambda d, sort_key=False: column(d.sort_by if sort_key and d.sort_by else d.column,
                                                             al(d.table)),
                            lambda m: column(m.out))
        if hide_blank and dims and measures and _hide_blank_rows(measures):
            if len(measures) == 1:
                sel = sel.having(not_null(rnd.r(measures[0].ir)))
            else:
                inner = sel
                sel = exp.select(*[column(c.alias_or_name, "q") for c in inner.selects]).from_(inner.subquery("q"))
                sel = sel.where(_or([not_null(column(m.out, "q")) for m in measures]))
                order = self._order(sorts,
                                    lambda d, sort_key=False: column(d.sort_out if sort_key and d.sort_out else d.out, "q"),
                                    lambda m: column(m.out, "q"))
        if order:
            sel = sel.order_by(*order)
        for name, _, cte in inf["ctx"]:
            sel = sel.with_(name, as_=cte)
        plan.form = "multi-grain" if inf["ctx"] else "single"
        plan.select = sel
        plan.sql = sel.sql(dialect=self.dialect, pretty=True)
        return plan

    # ---- several grains: one CTE per aggregated table ---------------------------------------------
    def _multi(self, info, dims, measures, sorts, plan: QueryPlan, hide_blank: bool = True) -> QueryPlan:
        al = _Aliases()
        for t in info:
            al(t)
        ctes, agg_cols = [], {}
        for i, (t, inf) in enumerate(info.items(), start=1):
            rnd = self._renderer(al, ctx_fn=self._ctx_fn(inf["ctx_cols"]))
            name = f"g{i}_{snake(t)}"
            cols, group = [], []
            for d in inf["keys"]:
                cols.append(exp.alias_(column(d.column, al(d.table)), d.out) if d.out != d.column
                            else column(d.column, al(d.table)))
                group.append(column(d.column, al(d.table)))
                if d.sort_by:
                    cols.append(exp.alias_(column(d.sort_by, al(d.table)), d.sort_out) if d.sort_out != d.sort_by
                                else column(d.sort_by, al(d.table)))
                    group.append(column(d.sort_by, al(d.table)))
            for k, a in enumerate(inf["aggs"], start=1):
                cname = f"agg_{i}_{k}"
                cols.append(exp.alias_(rnd.agg(a), cname))
                agg_cols[repr(a)] = (name, cname)
            sel = self._from(exp.select(*cols), t, inf["needed"], al, plan)
            sel = self._join_contexts(sel, inf["ctx"], al)
            where = self._and([rnd.r(f.ir) for f in inf["filters"]])
            if where is not None:
                sel = sel.where(where)
            if group:
                sel = sel.group_by(*group)
            ctes.append((name, t, inf, sel))
        # several tables cover every dimension: a group may have rows in only one of them, so list the groups first
        scaffold = None
        whole = [c for c in ctes if dims and len(c[2]["keys"]) == len(dims)]
        if len(whole) > 1:
            outs = [x for d in dims for x in ([d.out, d.sort_out] if d.sort_by else [d.out])]
            for c in whole:
                part = exp.select(*[column(o) for o in outs]).from_(exp.to_table(c[0]))
                scaffold = part if scaffold is None else exp.union(scaffold, part, distinct=True)
        # base CTE: covers the most dimensions; prefer a fact table on ties
        base = max(ctes, key=lambda c: (len(c[2]["keys"]), not self.graph.is_dimension(c[1]), -ctes.index(c)))
        if scaffold is not None:
            base = ("groups", base[1], base[2], scaffold)
        missing = [d for d in dims if d not in base[2]["keys"]]
        if missing:
            plan.reviews.append("dimensions " + ", ".join(f"{d.table}[{d.column}]" for d in missing)
                                + " are not related to the measure tables: add a dimension scaffold")
        rnd = Renderer(self.dialect, col_fn=lambda c: column(c.column, al(c.table)),
                       agg_fn=lambda a: column(agg_cols[repr(a)][1], agg_cols[repr(a)][0]) if repr(a) in agg_cols else None,
                       scalar_fn=lambda s: self._scalar(s, al))
        cols, src_of = [], {}
        sorts = [s for s in sorts if s.kind != "dim" or any(s.target in c[2]["keys"] for c in ctes)]
        for d in dims:
            src = base[0] if d in base[2]["keys"] else next((c[0] for c in ctes if d in c[2]["keys"]), None)
            if src is None:
                cols.append(exp.alias_(Renderer.review_null(f"{d.table}[{d.column}] is not related to the measure tables"),
                                       d.out))
                continue
            src_of[id(d)] = src
            cols.append(column(d.out, src))
            if d.sort_by:
                cols.append(column(d.sort_out, src))
        meas = [(m, rnd.r(m.ir)) for m in measures]
        cols += [exp.alias_(e, m.out) for m, e in meas]
        sel = exp.select(*cols).from_(exp.to_table(base[0]))
        for name, t, inf, _ in ctes:
            if name == base[0]:
                continue
            shared = [d for d in inf["keys"] if d in base[2]["keys"]]
            if shared:
                on = self._and([_same(column(d.out, base[0]), column(d.out, name)) for d in shared])
                sel = sel.join(exp.to_table(name), on=on, join_type="left")
            else:
                sel = sel.join(exp.to_table(name), join_type="cross")
        if hide_blank and dims and _hide_blank_rows(measures):
            sel = sel.where(_or([not_null(e.copy()) for _, e in meas]))
        order = self._order(sorts,
                            lambda d, sort_key=False: column(d.sort_out if sort_key and d.sort_out else d.out,
                                                             src_of.get(id(d), base[0])),
                            lambda m: column(m.out))
        if order:
            sel = sel.order_by(*order)
        for name, t, inf, cte_sel in ctes:
            for ctx_name, _, ctx_sel in inf["ctx"]:
                sel = sel.with_(ctx_name, as_=ctx_sel)
            sel = sel.with_(name, as_=cte_sel)
        if scaffold is not None:
            sel = sel.with_("groups", as_=scaffold)
        plan.form = "multi-grain"
        plan.notes.append("Aggregates come from different tables (" + ", ".join(info) + "): each is computed at its "
                          "own grain in a CTE and joined on the shared dimensions, so nothing is double counted.")
        plan.select = sel
        plan.sql = sel.sql(dialect=self.dialect, pretty=True)
        return plan

    def _base_of(self, tables: set, preferred: list, plan: QueryPlan) -> str | None:
        """A table whose rows relate all the given tables: one of them, else the first table that looks them all up."""
        base = next((t for t in preferred if tables <= self._reach(t)), None)
        if base is None:
            base = next((t for t in self.graph.parents if tables <= self._reach(t)), None)
            if base:
                plan.notes.append(f"{', '.join(sorted(tables))} are related only through {base}: the SQL lists the "
                                  f"combinations that have {base} rows.")
        return base

    # ---- no measures: distinct list (slicers) -------------------------------------------------------------
    def _distinct(self, dims, filters, sorts, plan: QueryPlan) -> QueryPlan:
        if not dims:
            return plan
        tables = {d.table for d in dims}
        base = self._base_of(tables, [d.table for d in dims], plan) or dims[0].table
        reach = self._reach(base)
        if not tables <= reach:
            plan.reviews.append(f"no table relates {', '.join(sorted(tables))}: the columns of "
                                f"{', '.join(sorted(tables - reach))} are left out")
            dims = [d for d in dims if d.table in reach]
            sorts = [s for s in sorts if s.kind != "dim" or s.target in dims]
        al = _Aliases()
        al(base)
        rnd = self._renderer(al)
        flt = [f for f in filters if {c.table for c in cols_in(f.ir)} <= reach]
        cols, keys = [], []
        for d in dims:
            cols.append(column(d.column, al(d.table)))
            if d.sort_by:
                cols.append(column(d.sort_by, al(d.table)))
        needed = (tables | {c.table for f in flt for c in cols_in(f.ir)}) & reach
        sel = self._from(exp.select(*cols).distinct(), base, needed, al, plan)
        where = self._and([rnd.r(f.ir) for f in flt])
        if where is not None:
            sel = sel.where(where)
        order = self._order(sorts, lambda d, sort_key=False: column(d.sort_by if sort_key and d.sort_by else d.column,
                                                                     al(d.table)), lambda m: None)
        if not order:
            order = [self.ordered(column(d.sort_by or d.column, al(d.table))) for d in dims]
        sel = sel.order_by(*order)
        plan.form = "distinct"
        plan.sql = sel.sql(dialect=self.dialect, pretty=True)
        return plan

    # ---- measures without aggregates (constants, filter-independent values) -------------------------------
    def _constant(self, dims, measures, filters, sorts, plan: QueryPlan) -> QueryPlan:
        al = _Aliases()
        rnd = self._renderer(al)
        cols = [exp.alias_(rnd.r(m.ir), m.out) for m in measures]
        sel = exp.select(*cols)
        if dims:
            tables = {d.table for d in dims}
            base = next((t for t in [d.table for d in dims] if tables <= self._reach(t)), None)
            bases = [base] if base else list(dict.fromkeys(d.table for d in dims))
            bases = [b for b in bases if not any(b != o and b in self._reach(o) for o in bases)]
            sel = exp.select(*[column(d.column, al(d.table)) for d in dims], *cols).distinct()
            for i, b in enumerate(bases):
                mine = tables & self._reach(b)
                tables = tables - mine
                if i == 0:
                    al(b)
                    sel = self._from(sel, b, mine, al, plan)
                    continue
                sel = sel.join(table_ref(self.naming.view(b), al(b)), join_type="cross")
                for child, ccol, parent, pcol in self.graph.join_edges(b, mine):
                    on = exp.EQ(this=column(ccol, al(child)), expression=column(pcol, al(parent)))
                    sel = sel.join(table_ref(self.naming.view(parent), al(parent)), on=on, join_type="left")
                plan.tables.append(b)
            reach = set().union(*[self._reach(b) for b in bases])
            for f in filters:
                if {c.table for c in cols_in(f.ir)} <= {d.table for d in dims}:
                    sel = sel.where(rnd.r(f.ir))
                elif {c.table for c in cols_in(f.ir)} & reach:
                    self._review(plan, f"filter {f.text} is not applied to the list of {', '.join(bases)} values")
            if len(bases) > 1:
                plan.notes.append("The columns come from tables that are not related (" + ", ".join(bases) + "): the SQL "
                                  "lists every combination, as Power BI does for a value that is never blank.")
        plan.form = "constant"
        plan.sql = sel.sql(dialect=self.dialect, pretty=True)
        return plan


def _row_columns(a: Agg) -> set:
    """Columns read on the rows being aggregated (values of the visual are computed elsewhere)."""
    return row_cols(a.arg) | (row_cols(a.pred) if a.pred is not None else set())


def _same(a: exp.Expression, b: exp.Expression) -> exp.Expression:
    """a = b, with NULL equal to NULL (the blank group)."""
    both = exp.And(this=exp.Is(this=a.copy(), expression=exp.Null()), expression=exp.Is(this=b.copy(), expression=exp.Null()))
    return exp.Paren(this=exp.Or(this=exp.EQ(this=a, expression=b), expression=exp.Paren(this=both)))


def _has_fixed_value(n) -> bool:
    """A value outside the aggregates that does not come from the group's own rows: filter-independent, or a total at
    a coarser grain."""
    if isinstance(n, Agg):
        return False
    return isinstance(n, Scalar) or isinstance(n, Total) or any(_has_fixed_value(c) for c in children(n))


def _window_totals(n) -> list:
    """Totals at a coarser grain the visual's query computes as a window (not those inside an aggregate's filter, which
    are flagged for review, nor those inside a filter-independent value)."""
    if windowable_total(n):
        return [n]
    if isinstance(n, (Agg, Scalar)) or (isinstance(n, Total) and n.independent):
        return []
    return [t for c in children(n) for t in _window_totals(c)]


def _blank_without_rows(n) -> bool:
    """Is the measure BLANK for a group without rows?"""
    if isinstance(n, (Agg, Null, Review)):
        return True
    if isinstance(n, SafeDiv):
        return _blank_without_rows(n.num) or (_blank_without_rows(n.den) and n.alt is None)
    if isinstance(n, BlankArith):
        return _blank_without_rows(n.left) and _blank_without_rows(n.right)
    if isinstance(n, Bin) and n.op == "*":
        return _blank_without_rows(n.left) or _blank_without_rows(n.right)
    if isinstance(n, Un) and n.op == "NEG":
        return _blank_without_rows(n.x)
    if isinstance(n, Case):
        return all(_blank_without_rows(v) for _, v in n.whens) and (n.default is None or _blank_without_rows(n.default))
    if isinstance(n, Fn) and n.name in ("COALESCE", "CONCAT"):
        return all(_blank_without_rows(a) for a in n.args)
    if isinstance(n, Fn) and n.name in ("ROUND", "ABS", "FLOOR", "CEIL", "NUMFMT", "TOSTRING"):
        return _blank_without_rows(n.args[0])
    return False


def _hide_blank_rows(measures) -> bool:
    """Power BI hides a group when every measure is blank; a measure that is not translated (NULL) proves nothing."""
    return all(can_be_null(m.ir) for m in measures) and not any(reviews_in(m.ir) for m in measures)


def can_be_null(n) -> bool:
    """Can this measure be NULL for a group that has rows? (COUNT and COALESCE-based results cannot.)"""
    if isinstance(n, Lit):
        return n.value is None
    if isinstance(n, (Null, Review, SafeDiv)):
        return True
    if isinstance(n, Agg):
        return n.func != "COUNT"
    if isinstance(n, BlankArith):
        return False
    if isinstance(n, Fn):
        if n.name == "COALESCE":
            return all(can_be_null(a) for a in n.args)
        return any(can_be_null(a) for a in n.args)
    if isinstance(n, Bin):
        return can_be_null(n.left) or can_be_null(n.right)
    if isinstance(n, Un):
        return can_be_null(n.x)
    if isinstance(n, Case):
        return n.default is None or isinstance(n.default, Null) or any(can_be_null(v) for _, v in n.whens) \
            or can_be_null(n.default)
    if isinstance(n, Scalar):
        return can_be_null(n.expr)
    return True


def _or(conds: list):
    out = None
    for c in conds:
        out = c if out is None else exp.Or(this=out, expression=c)
    return out
