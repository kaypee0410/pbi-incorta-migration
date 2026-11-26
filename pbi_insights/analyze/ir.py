"""Intermediate representation between DAX and SQL.

A translated measure is a small tree of these nodes. The important idea is `Agg`: an aggregate over
one table, carrying the CALCULATE filters that apply to it as a row-level predicate (`pred`). The
SQL generator renders it as SUM(CASE WHEN pred THEN x END), and uses `Agg.table` to work out the
grain each aggregate must be computed at (fact vs dimension), which is how fan-out is avoided.
"""
from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Col:
    table: str
    column: str


@dataclass(frozen=True)
class Lit:
    value: object


@dataclass(frozen=True)
class Null:
    pass


@dataclass(frozen=True)
class Star:
    pass


@dataclass(frozen=True)
class Agg:
    func: str                    # SUM AVG MIN MAX COUNT MEDIAN
    arg: object                  # row-level expression or Star()
    table: str                   # table whose rows are aggregated (the grain)
    distinct: bool = False
    pred: object = None          # row-level predicate from CALCULATE / FILTER
    replaced: tuple = ()         # (table, column) names whose filters a CALCULATE replaced, the visual's own too


@dataclass(frozen=True)
class Bin:
    op: str                      # + - * / = <> < > <= >= AND OR
    left: object
    right: object


@dataclass(frozen=True)
class Un:
    op: str                      # NEG NOT
    x: object


@dataclass(frozen=True)
class Case:
    whens: tuple                 # ((condition, value), ...)
    default: object = None


@dataclass(frozen=True)
class Fn:
    name: str
    args: tuple


@dataclass(frozen=True)
class SafeDiv:
    num: object
    den: object
    alt: object = None


@dataclass(frozen=True)
class BlankArith:
    """DAX + / - where a blank operand counts as 0."""
    op: str
    left: object
    right: object


@dataclass(frozen=True)
class IsNull:
    x: object


@dataclass(frozen=True)
class InList:
    x: object
    values: tuple


@dataclass(frozen=True)
class Scalar:
    """Value computed on its own, ignoring the visual's filters (DAX ALL / FILTER(ALL(...)))."""
    expr: object
    table: str
    note: str = ""


@dataclass(frozen=True)
class Review:
    reason: str


@dataclass(frozen=True)
class OuterCol:
    """A column of the OUTER row inside a nested row context over the same table (DAX EARLIER, or a VAR
    captured before FILTER(SameTable, ...)). Only meaningful inside a CorrAgg predicate."""
    table: str
    column: str


@dataclass(frozen=True)
class CorrAgg:
    """Aggregate evaluated for each row of a calculated column (no filter context): over the whole of `table`,
    restricted by `pred`. OuterCol terms in `pred` tie the aggregated rows to the current row.

    The translator splits `pred` into `keys` (equalities inner expression = outer expression) and `inner`
    (conditions on the aggregated rows only). With the same table and same-column keys this is a window
    aggregate: AGG(CASE WHEN inner THEN arg END) OVER (PARTITION BY keys)."""
    func: str
    arg: object
    table: str
    distinct: bool = False
    pred: object = None
    keys: tuple = ()             # ((inner expression, outer expression), ...)
    inner: object = None
    host: str = ""               # table of the calculated column (the outer row)
    complex: str = ""            # why it is not a plain correlated aggregate, if it is not

    @property
    def windowable(self) -> bool:
        return not self.complex and self.table == self.host and all(same_shape(i, o) for i, o in self.keys)


@dataclass(frozen=True)
class Lookup:
    """LOOKUPVALUE(result, search1, value1, ...[, alternate])."""
    result: Col
    keys: tuple                  # ((Col search column, value expression), ...)
    default: object = None


@dataclass(frozen=True)
class TimeShift:
    """Time intelligence: `expr` evaluated over a shifted ('ago') or period-to-date ('todate') date range."""
    expr: object
    date: Col
    kind: str                    # ago | todate
    n: int = 1
    part: str = "Month"          # Day Week Month Quarter Year


@dataclass(frozen=True)
class Rank:
    """RANKX over the members of `over` (the visual's other columns stay as partitions)."""
    expr: object
    over: tuple                  # (Col, ...) columns whose filters RANKX removes with ALL()
    desc: bool = True
    dense: bool = False


@dataclass(frozen=True)
class Total:
    """`expr` with the filters on whole tables / columns removed (CALCULATE + REMOVEFILTERS/ALL)."""
    expr: object
    tables: tuple = ()
    columns: tuple = ()          # (Col, ...)
    independent: str = ""        # table the value is computed on when no filter of the report can reach it
    kind: str = "ALL"            # ALL (also REMOVEFILTERS) or ALLSELECTED (keeps slicers, removes the visual's grouping)


WINDOW_AGG = {"SUM": "SUM", "COUNT": "SUM", "MIN": "MIN", "MAX": "MAX"}     # aggregate -> how its group values add up
NESTED_TOTAL = ("REMOVEFILTERS / ALL: a total at a coarser grain inside an aggregate's filter needs a total CTE at the "
                "coarser grain")
_AGG_WORD = {"AVG": "AVERAGE", "MEDIAN": "MEDIAN", "COUNT": "DISTINCTCOUNT"}


def window_total_issue(t: Total) -> str:
    """"" when a total at a coarser grain (CALCULATE + ALL / REMOVEFILTERS / ALLSELECTED that does not remove every
    filter) is a window over the visual's rows: each aggregate inside is additive (SUM, COUNT, COUNTROWS, MIN, MAX),
    so its value at the coarser grain is the SUM / MIN / MAX of its values per group. Otherwise the reason it is not."""
    if t.independent:
        return ""
    for n in walk(t.expr, into_scalar=False):
        if isinstance(n, Total) and not n.independent or isinstance(n, (Review, TimeShift, Rank, CorrAgg, Lookup)):
            return ("REMOVEFILTERS / ALL: needs a total at a coarser grain around a construct a window cannot "
                    "take (window function or a separate query)")
    aggs = top_aggs(t.expr)                       # a filter-independent value inside stays its own subquery
    for a in aggs:
        if a.func not in WINDOW_AGG or a.distinct:
            word = _AGG_WORD.get(a.func, a.func) if a.func != "COUNT" or a.distinct else a.func
            return f"REMOVEFILTERS / ALL: {word} is not additive, so it needs a total CTE at the coarser grain"
        if any(isinstance(v, Agg) for v in context_values(a)):
            return ("REMOVEFILTERS / ALL: a value of the visual inside the total needs a total CTE at the coarser "
                    "grain")
    if not aggs:
        return "REMOVEFILTERS / ALL: needs a total at a coarser grain (window function or a separate query)"
    return ""


def windowable_total(n) -> bool:
    return isinstance(n, Total) and not n.independent and not window_total_issue(n)


IR_TYPES = (Col, Lit, Null, Star, Agg, Bin, Un, Case, Fn, SafeDiv, BlankArith, IsNull, InList, Scalar, Review,
            OuterCol, CorrAgg, Lookup, TimeShift, Rank, Total)
BOOL_OPS = {"=", "<>", "<", ">", "<=", ">=", "AND", "OR"}


def children(node):
    if not isinstance(node, IR_TYPES):
        return
    for f in fields(node):
        v = getattr(node, f.name)
        if isinstance(v, IR_TYPES):
            yield v
        elif isinstance(v, tuple):
            for item in v:
                if isinstance(item, tuple):
                    for x in item:
                        if isinstance(x, IR_TYPES):
                            yield x
                elif isinstance(item, IR_TYPES):
                    yield item


def walk(node, into_scalar: bool = True):
    if not isinstance(node, IR_TYPES):
        return
    yield node
    if isinstance(node, Scalar) and not into_scalar:
        return
    for c in children(node):
        yield from walk(c, into_scalar)


def aggs_in(node, into_scalar: bool = False) -> list[Agg]:
    out = []
    for n in walk(node, into_scalar):
        if isinstance(n, Agg) and n not in out:
            out.append(n)
    return out


def top_aggs(node) -> list[Agg]:
    """Aggregates a visual computes per group: not those inside another aggregate (see context_values) nor those of
    a filter-independent value."""
    out = []

    def visit(n):
        if isinstance(n, Agg):
            if n not in out:
                out.append(n)
        elif not isinstance(n, Scalar) and not (isinstance(n, Total) and n.independent):
            for c in children(n):
                visit(c)
    visit(node)
    return out


def context_values(agg: Agg) -> list:
    """Values of the visual's filter context used row by row inside an aggregate, e.g. SELECTEDVALUE(Axis[Year]) in
    a CALCULATE filter: they are computed first, per group, and joined in. Agg, Scalar or independent Total nodes."""
    out = []

    def visit(n):
        if isinstance(n, (Agg, Scalar)) or (isinstance(n, Total) and n.independent):
            if n not in out:
                out.append(n)
        else:
            for c in children(n):
                visit(c)
    for part in (agg.arg, agg.pred):
        if part is not None:
            visit(part)
    return out


def row_cols(node) -> set:
    """Columns a row predicate tests (not those of the aggregates it compares them with)."""
    if isinstance(node, (Agg, Scalar)) or (isinstance(node, Total) and node.independent):
        return set()
    if isinstance(node, Col):
        return {node}
    out = set()
    for c in children(node) if isinstance(node, IR_TYPES) else ():
        out |= row_cols(c)
    return out


def cols_in(node) -> list[Col]:
    out = []
    for n in walk(node):
        if isinstance(n, Col) and n not in out:
            out.append(n)
    return out


def reviews_in(node) -> list[str]:
    """Reasons the generic SQL cannot be generated automatically: Review nodes, plus constructs that other
    targets (Incorta time-series, ranking and level-based functions) handle natively but plain SQL does not."""
    out = [n.reason for n in walk(node) if isinstance(n, Review)]
    for x in sql_gaps(node):
        if x not in out:
            out.append(x)
    return out


def sql_gaps(node) -> list[str]:
    out = []
    for n in walk(node):
        why = None
        if isinstance(n, TimeShift):
            what = (f"{n.part}-to-date" if n.kind == "todate"
                    else f"{n.n} {n.part.lower()}{'s' if abs(n.n) != 1 else ''} back")
            why = f"time intelligence ({what}): needs a date-offset join to the date table"
        elif isinstance(n, Rank):
            why = "RANKX: needs a window function over the rows of the visual"
        elif isinstance(n, Total) and not n.independent:
            why = window_total_issue(n) or None
        elif isinstance(n, Agg) and any(windowable_total(x) for p in (n.arg, n.pred) for x in walk(p)):
            why = NESTED_TOTAL
        elif isinstance(n, CorrAgg) and n.table == n.host and not n.windowable:
            why = n.complex or "per-row aggregate with a condition that is not an equality: needs a self-join"
        elif isinstance(n, CorrAgg) and n.table != n.host and (n.keys or n.complex):
            why = n.complex or f"aggregate over {n.table} for each row: needs a grouped subquery joined back"
        if why and why not in out:
            out.append(why)
    return out


def has_outer(node) -> bool:
    return any(isinstance(n, OuterCol) for n in walk(node))


def outer_to_inner(node):
    return transform(node, lambda n: Col(n.table, n.column) if isinstance(n, OuterCol) else n)


def same_shape(inner, outer) -> bool:
    """`inner = outer` compares the aggregated rows with the current row on the same expression."""
    return not has_outer(inner) and outer_to_inner(outer) == inner


def has_agg(node) -> bool:
    return any(isinstance(n, Agg) for n in walk(node))


def is_boolean(node) -> bool:
    return (isinstance(node, Bin) and node.op in BOOL_OPS) or isinstance(node, (IsNull, InList)) \
        or (isinstance(node, Un) and node.op == "NOT") or (isinstance(node, Lit) and isinstance(node.value, bool)) \
        or (isinstance(node, Fn) and node.name.startswith("LIKE_"))


def and_(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return Bin("AND", a, b)


def conjuncts(pred) -> list:
    if pred is None:
        return []
    if isinstance(pred, Bin) and pred.op == "AND":
        return conjuncts(pred.left) + conjuncts(pred.right)
    return [pred]


def transform(node, fn):
    """Rebuild an IR tree bottom-up; `fn(node)` may return a replacement (or the node unchanged)."""
    if isinstance(node, tuple):
        return tuple(transform(x, fn) for x in node)
    if not isinstance(node, IR_TYPES):
        return node
    kw = {f.name: transform(getattr(node, f.name), fn) for f in fields(node)}
    return fn(type(node)(**kw))


def override(existing, new):
    """CALCULATE semantics: a new filter replaces existing filters on the same columns."""
    if new is None:
        return existing
    new_cols = row_cols(new)
    kept = [c for c in conjuncts(existing) if not row_cols(c) or not row_cols(c) <= new_cols]
    out = None
    for c in kept:
        out = and_(out, c)
    return and_(out, new)
