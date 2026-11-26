"""Coverage matrix for filter removal (ALL / REMOVEFILTERS / ALLSELECTED / ALLEXCEPT / ...), on a synthetic model.

Model: two facts (Sales, Returns) -> Product -> Category, both -> Date; Sales <-> Store bidirectional; an inactive
Sales[ShipDate] -> Date; Param is disconnected. For every translated shape the insight SQL runs in DuckDB on small
frames and is compared with a value computed in pandas from the DAX definition: each case lists, by hand, which
columns the removal clears (the expanded table written out), so the expectation does not reuse the code under test.
"""
from __future__ import annotations

import datetime as dt

import pytest

pd = pytest.importorskip("pandas")
duckdb = pytest.importorskip("duckdb")

from pbi_insights.analyze.dax_translate import ModelIndex, Translator, classify_measure  # noqa: E402
from pbi_insights.analyze.graph import RelGraph  # noqa: E402
from pbi_insights.analyze.ir import Bin, Col, InList, Lit  # noqa: E402
from pbi_insights.analyze.planner import Planner, generic_measure_sql  # noqa: E402
from pbi_insights.analyze.querygen import Dim, FilterSpec, MeasureSpec, QueryBuilder  # noqa: E402
from pbi_insights.extract.model import from_tmsl  # noqa: E402

D = dt.datetime
CATEGORY = pd.DataFrame({"CategoryID": [1, 2], "CatName": ["A", "B"]})
PRODUCT = pd.DataFrame({"ProductID": [1, 2, 3, 4], "CategoryID": [1, 1, 2, 2],
                        "Name": ["p1", "p2", "p3", "p4"], "Color": ["Red", "Blue", "Red", "Green"]})
DATE = pd.DataFrame({"Date": [D(2024, 12, 15), D(2025, 1, 15), D(2025, 2, 15), D(2025, 3, 15)],
                     "Year": [2024, 2025, 2025, 2025], "Month": [12, 1, 2, 3]})           # March has no sales
STORE = pd.DataFrame({"StoreID": [1, 2], "Region": ["East", "West"]})
SALES = pd.DataFrame({
    "SaleID": range(1, 9),
    "ProductID": [1, 1, 2, 3, 3, 4, 1, 2],
    "DateKey": [D(2024, 12, 15), D(2025, 1, 15), D(2025, 1, 15), D(2025, 1, 15), D(2025, 2, 15), D(2025, 2, 15),
                D(2025, 2, 15), D(2024, 12, 15)],
    "ShipDate": [D(2025, 1, 15)] * 8,
    "StoreID": [1, 2, 1, 1, 2, 2, 1, 2],
    "Amount": [10.0, 20.0, 5.0, 40.0, 15.0, 7.0, 3.0, 11.0],
    "Qty": [1, 2, 1, 4, 3, 1, 1, 2]})
RETURNS = pd.DataFrame({"ProductID": [1, 3, 4], "DateKey": [D(2025, 1, 15), D(2025, 2, 15), D(2024, 12, 15)],
                        "Amount": [2.0, 4.0, 1.0]})
PARAM = pd.DataFrame({"Value": [1, 2, 3]})
FRAMES = {"Category": CATEGORY, "Product": PRODUCT, "Date": DATE, "Store": STORE, "Sales": SALES,
          "Returns": RETURNS, "Param": PARAM}

MEASURES = {
    "m": "SUM(Sales[Amount])", "r": "SUM(Returns[Amount])",
    "MaxDateAll": "CALCULATE(MAX('Date'[Date]), ALL(Sales))",
    # filter removal
    "all_fact": "CALCULATE([m], ALL(Sales))", "rf_fact": "CALCULATE([m], REMOVEFILTERS(Sales))",
    "all_dim": "CALCULATE([m], ALL(Product))", "all_col": "CALCULATE([m], ALL(Product[Color]))",
    "all_cols": "CALCULATE([m], ALL(Product[Color], Product[Name]))",
    "all_none": "CALCULATE([m], ALL())", "rf_none": "CALCULATE([m], REMOVEFILTERS())",
    "sel_dim": "CALCULATE([m], ALLSELECTED(Product))", "sel_col": "CALCULATE([m], ALLSELECTED(Product[Color]))",
    "sel_none": "CALCULATE([m], ALLSELECTED())", "except": "CALCULATE([m], ALLEXCEPT(Sales, Product[Color]))",
    "noblank": "CALCULATE([m], ALLNOBLANKROW(Product[Color]))",
    "keep_all": "CALCULATE([m], KEEPFILTERS(ALL(Product[Color])))",
    "filter_all": 'CALCULATE([m], FILTER(ALL(Product), Product[Color] = "Red"))',
    "all_lookup2": "CALCULATE([m], ALL(Category))",
    "all_plus_filter": 'CALCULATE([m], ALL(Product[Color]), Product[Name] = "p1")',
    "nested": 'CALCULATE(CALCULATE([m], ALL(Product[Color])), Product[Color] = "Red")',
    "disconnected": "CALCULATE([m], ALL(Param))",
    "other_fact": "CALCULATE([m], ALL(Returns))",
    # aggregates inside
    "agg_sum": "CALCULATE(SUM(Sales[Amount]), ALL(Product[Color]))",
    "agg_count": "CALCULATE(COUNT(Sales[Amount]), ALL(Product[Color]))",
    "agg_countrows": "CALCULATE(COUNTROWS(Sales), ALL(Product[Color]))",
    "agg_min": "CALCULATE(MIN(Sales[Amount]), ALL(Product[Color]))",
    "agg_max": "CALCULATE(MAX(Sales[Amount]), ALL(Product[Color]))",
    "agg_sumx": "CALCULATE(SUMX(Sales, Sales[Amount] * Sales[Qty]), ALL(Product[Color]))",
    "agg_avg": "CALCULATE(AVERAGE(Sales[Amount]), ALL(Product[Color]))",
    "agg_dcount": "CALCULATE(DISTINCTCOUNT(Sales[ProductID]), ALL(Product[Color]))",
    "agg_median": "CALCULATE(MEDIAN(Sales[Amount]), ALL(Product[Color]))",
    "agg_avgx": "CALCULATE(AVERAGEX(Sales, Sales[Amount]), ALL(Product[Color]))",
    # building blocks
    "share": "DIVIDE([m], CALCULATE([m], ALL(Product[Color])))",
    "share_r": "DIVIDE([r], CALCULATE([r], ALL(Product[Color])))",
    "share_diff": "[share] - [share_r]",
    "share_if": "IF([m] > 10, [share], BLANK())",
    "share_switch": "SWITCH(TRUE(), [m] > 10, [share], 0)",
    "share_var": "VAR s = [share] RETURN s * 100",
    "c1_limit": "CALCULATE([m], 'Date'[Date] <= [MaxDateAll])",
    "c1_var": "VAR d = [MaxDateAll] RETURN CALCULATE([m], 'Date'[Date] <= d)",
    "c1_edate": "VAR d = [MaxDateAll] RETURN CALCULATE([m], 'Date'[Date] >= EDATE(DATE(YEAR(d), MONTH(d), 1), -2))",
    "c1_two_facts": ("VAR d = [MaxDateAll] VAR s = EDATE(DATE(YEAR(d), MONTH(d), 1), -2) "
                     "RETURN CALCULATE([m], 'Date'[Date] >= s) + CALCULATE([r], 'Date'[Date] >= s)"),
    "c1_other_table": "CALCULATE([m], 'Date'[Date] >= CALCULATE(MAX(Returns[DateKey]), ALL(Returns)))",
    # relationships
    "bidir_removal": "CALCULATE([m], ALL(Store))",
    "bidir_aggregate": "CALCULATE(COUNTROWS(Store), ALL(Sales))",
    "userel": "CALCULATE([m], USERELATIONSHIP(Sales[ShipDate], 'Date'[Date]), ALL('Date'))",
    "sel_outer": 'CALCULATE(CALCULATE([m], ALLSELECTED(Product[Color])), Product[Color] = "Red")',
}


def _cols(*specs):
    return [{"name": n, "dataType": t, "sourceColumn": n} for n, t in specs]


def _tmsl():
    def t(s):
        api = pd.api.types
        return ("dateTime" if api.is_datetime64_any_dtype(s) else "int64" if api.is_integer_dtype(s)
                else "double" if api.is_float_dtype(s) else "string")
    tables = []
    for name, df in FRAMES.items():
        tables.append({"name": name, "columns": _cols(*[(c, t(df[c])) for c in df.columns]),
                       "measures": [{"name": k, "expression": v} for k, v in MEASURES.items()] if name == "Sales" else []})
    rel = lambda i, f, fc, tt, tc, **kw: {"name": str(i), "fromTable": f, "fromColumn": fc, "toTable": tt,  # noqa: E731
                                          "toColumn": tc, **kw}
    return {"model": {"tables": tables, "relationships": [
        rel(1, "Sales", "ProductID", "Product", "ProductID"), rel(2, "Product", "CategoryID", "Category", "CategoryID"),
        rel(3, "Sales", "DateKey", "Date", "Date"), rel(4, "Sales", "ShipDate", "Date", "Date", isActive=False),
        rel(5, "Returns", "ProductID", "Product", "ProductID"), rel(6, "Returns", "DateKey", "Date", "Date"),
        rel(7, "Sales", "StoreID", "Store", "StoreID", crossFilteringBehavior="bothDirections")]}}


MODEL = from_tmsl(_tmsl())
GRAPH = RelGraph(MODEL["relationships"])
TR = Translator(ModelIndex(MODEL), GRAPH)
# the joined rows of each fact, as DAX sees them through the relationships
J = (SALES.merge(PRODUCT, on="ProductID").merge(CATEGORY, on="CategoryID")
     .merge(DATE, left_on="DateKey", right_on="Date").merge(STORE, on="StoreID"))
JR = RETURNS.merge(PRODUCT, on="ProductID").merge(CATEGORY, on="CategoryID").merge(DATE, left_on="DateKey", right_on="Date")
PRODUCT_COLS, CATEGORY_COLS = {"ProductID", "CategoryID", "Name", "Color"}, {"CatName"}
DATE_COLS, STORE_COLS = {"Date", "Year", "Month"}, {"Region"}
EVERYTHING = PRODUCT_COLS | CATEGORY_COLS | DATE_COLS | STORE_COLS


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect()
    for name, df in FRAMES.items():
        c.register(f"df_{name.lower()}", df)
        c.execute(f"CREATE TABLE vw_{name.lower()} AS SELECT * FROM df_{name.lower()}")
    return c


def measure(name):
    res = TR.measure(name)
    return res, classify_measure(res, res.is_alias, GRAPH)[0], generic_measure_sql(res.ir, "duckdb")


def insight(con, name, dims, filters=()):
    """(plan, {group tuple: value}) of the measure by the given (table, column) dimensions, run in DuckDB."""
    plan = QueryBuilder(GRAPH, "duckdb").build([Dim(t, c, c) for t, c in dims],
                                              [MeasureSpec(name, "x", TR.measure(name).ir)], list(filters), [])
    rows = con.execute(plan.sql).fetchall()
    return plan, {tuple(r[:-1]): r[-1] for r in rows}


def total(frame, ctx: dict, cleared=frozenset(), col="Amount", how="sum", extra=None):
    """DAX value: the rows of `frame` that satisfy every filter of `ctx` whose column the removal did not clear."""
    rows = frame
    for k, v in ctx.items():
        if k not in cleared:
            rows = rows[rows[k].isin(v)] if isinstance(v, (set, list, tuple)) else rows[rows[k] == v]
    if extra is not None:
        rows = rows[extra(rows)]
    if rows.empty:
        return None
    s = rows[col] if col != "*" else rows.index.to_series()
    return {"sum": s.sum, "count": s.count, "min": s.min, "max": s.max}[how]()


def groups(frame, dims):
    cols = [c for _, c in dims]
    if not cols:
        return [()]
    return [tuple(x) if isinstance(x, tuple) else (x,) for x in frame.groupby(cols).size().index]


def check(got: dict, dims, expected, frame=J, tol=1e-9):
    """Every group the SQL returns has the DAX value; the SQL returns exactly the groups that have rows."""
    want_groups = set(groups(frame, dims))
    assert set(got) == want_groups, (sorted(got), sorted(want_groups))
    for g, v in got.items():
        e = expected(dict(zip([c for _, c in dims], g)))
        assert (v is None and e is None) or abs(float(v) - float(e)) < tol, (g, v, e)


COLOR, YEAR, NAME, CAT, REGION = ("Product", "Color"), ("Date", "Year"), ("Product", "Name"), ("Category", "CatName"), \
    ("Store", "Region")


# ------------------------------------------------------------------------------------------- the expanded table
def test_expanded_table_follows_many_to_one_recursively():
    assert GRAPH.expanded("Sales") == {"Sales", "Product", "Category", "Date", "Store"}
    assert GRAPH.expanded("Product") == {"Product", "Category"}
    assert GRAPH.expanded("Param") == {"Param"}
    removes = GRAPH.removal(("Product",))
    assert removes("Category", "CatName") and removes("Product", "Color") and not removes("Date", "Year")
    keep = GRAPH.removal(("Sales",), keep=(Col("Product", "Color"),))
    assert keep("Date", "Year") and not keep("Product", "Color")
    assert GRAPH.reverse_entry(GRAPH.expanded("Sales")) == ""                          # Sales is the many side
    assert "bidirectional" in GRAPH.reverse_entry(GRAPH.expanded("Store"))


def test_one_to_one_is_flagged():
    g = RelGraph([{"from_table": "A", "from_column": "k", "to_table": "B", "to_column": "k",
                   "from_cardinality": "one", "to_cardinality": "one"}])
    assert g.reverse_entry(g.expanded("A")) == ""             # A's aggregate sees B's filters the normal way
    assert "one-to-one" in g.reverse_entry(g.expanded("B"))   # B's aggregate also gets A's (DAX expands 1:1 both ways)


# ------------------------------------------------------------------------------------------- measure level
REVIEW = "Needs review"
CASE1, CASE2, NOTES = "Filter-independent (ALL)", "Total at a coarser grain (ALL)", "Translated with notes"


@pytest.mark.parametrize("name, pattern, status, sql_has", [
    ("all_fact", CASE1, NOTES, "(SELECT SUM(Sales.Amount) FROM Sales)"),
    ("rf_fact", CASE1, NOTES, "(SELECT SUM(Sales.Amount) FROM Sales)"),
    ("all_none", CASE1, NOTES, "(SELECT SUM(Sales.Amount) FROM Sales)"),
    ("rf_none", CASE1, NOTES, "(SELECT SUM(Sales.Amount) FROM Sales)"),
    ("all_dim", CASE2, NOTES, "SUM(SUM(Sales.Amount)) OVER ()"),
    ("all_col", CASE2, NOTES, "SUM(SUM(Sales.Amount)) OVER ()"),
    ("all_cols", CASE2, NOTES, "except Product[Color], Product[Name]"),
    ("sel_dim", CASE2, NOTES, "slicers stay"),
    ("sel_col", CASE2, NOTES, "slicers stay"),
    ("sel_none", CASE2, NOTES, "slicers stay"),
    ("except", CASE2, NOTES, "keeping Product[Color]"),
    ("noblank", CASE2, NOTES, "except Product[Color]"),
    ("filter_all", CASE2, NOTES, "SUM(SUM(CASE WHEN Product.Color = 'Red' THEN Sales.Amount END)) OVER ()"),
    ("all_lookup2", CASE2, NOTES, "except Category"),
    ("all_plus_filter", CASE2, NOTES, "SUM(SUM(CASE WHEN Product.Name = 'p1' THEN Sales.Amount END)) OVER ()"),
    ("nested", CASE2, NOTES, "SUM(SUM(Sales.Amount)) OVER ()"),
    ("other_fact", CASE2, NOTES, "except Returns"),
    ("agg_sum", CASE2, NOTES, "SUM(SUM(Sales.Amount)) OVER ()"),
    ("agg_count", CASE2, NOTES, "SUM(COUNT(Sales.Amount)) OVER ()"),
    ("agg_countrows", CASE2, NOTES, "SUM(COUNT(*)) OVER ()"),
    ("agg_min", CASE2, NOTES, "MIN(MIN(Sales.Amount)) OVER ()"),
    ("agg_max", CASE2, NOTES, "MAX(MAX(Sales.Amount)) OVER ()"),
    ("agg_sumx", CASE2, NOTES, "SUM(SUM(Sales.Amount * Sales.Qty)) OVER ()"),
    ("share", CASE2, NOTES, "SUM(Sales.Amount) / NULLIF(SUM(SUM(Sales.Amount)) OVER ()"),
    ("share_diff", CASE2, NOTES, "SUM(SUM(Returns.Amount)) OVER ()"),
    ("share_if", CASE2, NOTES, "CASE WHEN"),
    ("share_switch", CASE2, NOTES, "CASE WHEN"),
    ("share_var", CASE2, NOTES, "* 100"),
    ("c1_limit", CASE1, NOTES, "(SELECT MAX(Date.Date) FROM Date)"),
    ("c1_var", CASE1, NOTES, "(SELECT MAX(Date.Date) FROM Date)"),
    ("c1_edate", CASE1, NOTES, "(SELECT MAX(Date.Date) FROM Date)"),
    ("c1_two_facts", CASE1, NOTES, "(SELECT MAX(Date.Date) FROM Date)"),
    ("c1_other_table", CASE1, NOTES, "(SELECT MAX(Returns.DateKey) FROM Returns)"),
])
def test_translated_shapes(name, pattern, status, sql_has):
    res, pat, sql = measure(name)
    assert (pat, res.status) == (pattern, status), (name, res.reviews)
    assert sql_has in sql.replace('"', ""), sql
    assert "visual_" not in sql and "REVIEW" not in sql


@pytest.mark.parametrize("name, reason", [
    ("agg_avg", "AVERAGE is not additive, so it needs a total CTE at the coarser grain"),
    ("agg_avgx", "AVERAGE is not additive, so it needs a total CTE at the coarser grain"),
    ("agg_dcount", "DISTINCTCOUNT is not additive, so it needs a total CTE at the coarser grain"),
    ("agg_median", "MEDIAN is not additive, so it needs a total CTE at the coarser grain"),
    ("bidir_removal", "filter removal through a bidirectional relationship"),
    ("bidir_aggregate", "filter removal through a bidirectional relationship"),
    ("userel", "USERELATIONSHIP"),
    ("sel_outer", "ALLSELECTED under an outer CALCULATE"),
])
def test_shapes_that_stay_needs_review(name, reason):
    res, _, sql = measure(name)
    assert res.status == REVIEW and any(reason in r for r in res.reviews), res.reviews
    assert "OVER" not in sql                                 # never a window on what is not windowable


def test_keepfilters_all_removes_nothing():
    res, pat, sql = measure("keep_all")
    assert res.status == NOTES and "OVER" not in sql and sql == "SUM(Sales.Amount)"
    assert any(n.startswith("KEEPFILTERS(ALL(...)) keeps the current filters") for n in res.notes)


def test_removal_on_a_disconnected_table_has_no_effect():
    res, pat, sql = measure("disconnected")
    assert sql == "SUM(Sales.Amount)" and pat != CASE2 and any("no effect" in n for n in res.notes)


# ------------------------------------------------------------------------------------------- numbers (DuckDB vs pandas)
CASES = [
    # name, dims, cleared columns (the DAX expanded table, written out), extra row filter, frame, aggregate
    ("all_col", [COLOR, YEAR], {"Color"}, None, J, "sum"),
    ("all_cols", [COLOR, NAME, YEAR], {"Color", "Name"}, None, J, "sum"),
    ("all_dim", [CAT, YEAR], PRODUCT_COLS | CATEGORY_COLS, None, J, "sum"),             # Product looks up Category
    ("all_dim", [REGION], PRODUCT_COLS | CATEGORY_COLS, None, J, "sum"),                # not removed: own value
    ("all_lookup2", [NAME], CATEGORY_COLS, None, J, "sum"),                             # Product is kept
    ("all_lookup2", [CAT, YEAR], CATEGORY_COLS, None, J, "sum"),
    ("noblank", [COLOR, YEAR], {"Color"}, None, J, "sum"),
    ("except", [COLOR, YEAR], EVERYTHING - {"Color"}, None, J, "sum"),
    ("other_fact", [COLOR, REGION], PRODUCT_COLS | CATEGORY_COLS | DATE_COLS, None, J, "sum"),
    ("sel_col", [COLOR, YEAR], {"Color"}, None, J, "sum"),
    ("sel_dim", [NAME], PRODUCT_COLS | CATEGORY_COLS, None, J, "sum"),
    ("sel_none", [COLOR, YEAR], EVERYTHING, None, J, "sum"),
    ("filter_all", [YEAR], PRODUCT_COLS | CATEGORY_COLS, lambda r: r["Color"] == "Red", J, "sum"),
    ("filter_all", [COLOR], PRODUCT_COLS | CATEGORY_COLS, lambda r: r["Color"] == "Red", J, "sum"),
    ("all_plus_filter", [COLOR], {"Color", "Name"}, lambda r: r["Name"] == "p1", J, "sum"),
    ("nested", [YEAR], {"Color"}, None, J, "sum"),
    ("agg_sum", [COLOR, YEAR], {"Color"}, None, J, "sum"),
    ("agg_count", [COLOR, YEAR], {"Color"}, None, J, "count"),
    ("agg_min", [COLOR, YEAR], {"Color"}, None, J, "min"),
    ("agg_max", [COLOR, YEAR], {"Color"}, None, J, "max"),
    ("keep_all", [COLOR, YEAR], set(), None, J, "sum"),
    ("disconnected", [COLOR], set(), None, J, "sum"),
]


@pytest.mark.parametrize("name, dims, cleared, extra, frame, how", CASES,
                         ids=[f"{c[0]}-by-{'-'.join(x[1] for x in c[1])}" for c in CASES])
def test_window_numbers_match_dax(con, name, dims, cleared, extra, frame, how):
    plan, got = insight(con, name, dims)
    assert not [r for r in plan.reviews if "groups without rows" not in r], plan.reviews
    check(got, dims, lambda g: total(frame, g, cleared, how=how, extra=extra), frame)


def test_countrows_and_sumx_windows(con):
    _, got = insight(con, "agg_countrows", [COLOR, YEAR])
    check(got, [COLOR, YEAR], lambda g: len(J[J["Year"] == g["Year"]]))
    _, got = insight(con, "agg_sumx", [COLOR, YEAR])
    jx = J.assign(AQ=J["Amount"] * J["Qty"])
    check(got, [COLOR, YEAR], lambda g: total(jx, g, {"Color"}, col="AQ"))


@pytest.mark.parametrize("dims", [[COLOR, YEAR], [CAT, COLOR], [COLOR], [YEAR], []])
def test_share_adds_up_to_one_per_partition(con, dims):
    plan, got = insight(con, "share", dims)
    assert not plan.reviews, plan.reviews
    check(got, dims, lambda g: total(J, g) / total(J, g, {"Color"}))
    others = [i for i, d in enumerate(dims) if d != COLOR]
    sums = {}
    for g, v in got.items():
        key = tuple(g[i] for i in others)
        sums[key] = sums.get(key, 0) + v
    assert all(abs(s - 1) < 1e-9 for s in sums.values()), sums


def test_case1_is_the_same_on_every_row_and_the_global_total(con):
    for name in ("all_fact", "rf_fact", "all_none", "rf_none"):
        _, got = insight(con, name, [COLOR, YEAR])
        assert set(got.values()) == {SALES["Amount"].sum()}, (name, got)


def test_case1_measure_inside_filters(con):
    last = DATE["Date"].max()                                   # MAX over the whole Date table: ALL(Sales) clears it
    start = D(last.year, last.month, 1) - pd.DateOffset(months=2)
    _, got = insight(con, "c1_limit", [COLOR])
    check(got, [COLOR], lambda g: total(J, g, extra=lambda r: r["Date"] <= last))
    _, got = insight(con, "c1_var", [COLOR])
    check(got, [COLOR], lambda g: total(J, g, extra=lambda r: r["Date"] <= last))
    _, got = insight(con, "c1_edate", [COLOR])
    want = J[J["Date"] >= start]
    check(got, [COLOR], lambda g: total(want, g), want)
    plan, got = insight(con, "c1_other_table", [COLOR])
    first = RETURNS["DateKey"].max()
    want = J[J["Date"] >= first]
    check(got, [COLOR], lambda g: total(want, g), want)
    assert "CROSS JOIN" in plan.sql and "visual_" not in plan.sql


def test_case1_in_two_facts(con):
    last = DATE["Date"].max()
    start = D(last.year, last.month, 1) - pd.DateOffset(months=2)
    plan, got = insight(con, "c1_two_facts", [COLOR])
    s = J[J["Date"] >= start].groupby("Color")["Amount"].sum()
    r = JR[JR["Date"] >= start].groupby("Color")["Amount"].sum()
    for (color,), v in got.items():
        assert abs(v - (s.get(color, 0) + r.get(color, 0))) < 1e-9, (color, v)


def test_share_chains(con):
    share = lambda f, g: (total(f, g) or 0) and total(f, g) / total(f, g, {"Color"})  # noqa: E731
    _, got = insight(con, "share_diff", [COLOR, YEAR])
    for (color, year), v in got.items():
        g = {"Color": color, "Year": year}
        sr = total(JR, g) / total(JR, g, {"Color"}) if total(JR, g) is not None else 0
        assert abs(v - ((share(J, g) or 0) - sr)) < 1e-9
    _, got = insight(con, "share_var", [COLOR])
    check(got, [COLOR], lambda g: share(J, g) * 100)
    _, got = insight(con, "share_switch", [COLOR])
    check(got, [COLOR], lambda g: share(J, g) if total(J, g) > 10 else 0)


# ------------------------------------------------------------------------------------------- visual shapes
def test_visual_shapes(con):
    for dims, part in (([], "OVER ()"), ([COLOR], "OVER ()"), ([YEAR], "OVER (PARTITION BY q.Year)"),
                       ([COLOR, YEAR], "OVER (PARTITION BY q.Year)")):
        plan, _ = insight(con, "share", dims)
        assert part in plan.sql and plan.form == "multi-grain", (dims, plan.sql)


def _slicer(values):
    return FilterSpec("Product", "Color", InList(Col("Product", "Color"), tuple(Lit(v) for v in values)),
                      "Slicer: Color in " + ", ".join(values))


def test_slicer_on_a_removed_column(con):
    plan, _ = insight(con, "share", [YEAR], [_slicer(["Red", "Blue"])])
    assert any(r.startswith("Power BI's total ignores the slicer on Product[Color]") for r in plan.reviews)
    plan, got = insight(con, "sel_dim", [NAME], [_slicer(["Red", "Blue"])])
    assert not any("ignores the slicer" in r for r in plan.reviews)            # ALLSELECTED keeps the slicer
    shown = J[J["Color"].isin(["Red", "Blue"])]
    check(got, [NAME], lambda g: shown["Amount"].sum(), shown)


def test_add_filter_on_the_visual_grouping_is_flagged(con):
    plan, _ = insight(con, "all_plus_filter", [NAME])
    assert any("replaces the visual's own grouping" in r for r in plan.reviews)


def test_top_n_with_a_window_total_is_flagged():
    from conftest import tmsl
    tm = tmsl()
    tm["model"]["tables"][0]["measures"].append(
        {"name": "Sel", "expression": "DIVIDE([Total], CALCULATE([Total], ALLSELECTED(Store[Region])))"})
    p = Planner({"pages": [], "visuals": []}, from_tmsl(tm), dialect="duckdb")
    v = {"id": "X-01", "page": "P", "title": "t", "visual_type": "tableEx", "generic_type": "table", "decorative": False,
         "is_slicer": False, "sort_spec": [],
         "fields": [{"role": "Rows", "kind": "Column", "table": "Store", "field": "Region", "active": True},
                    {"role": "Values", "kind": "Measure", "table": "Sales", "field": "Sel", "active": True}],
         "visual_filters": [{"applied": True, "filter_type": "TopN", "condition": "Top 3 Region by Sel",
                             "level": "Visual", "field_kind": "Column", "table": "Store", "field": "Region"}]}
    plan = p.plan_visual(v, [], [], [])
    assert any("the window sees only the rows the Top N keeps" in r for r in plan["reviews"])
    assert plan["approach"] == "Needs review"


def test_incorta_groupby_uses_the_expanded_table():
    from pbi_insights.incorta.plan import IncortaPlanner
    removes = GRAPH.removal(("Product",))
    dims = [{"table": "Category", "column": "CatName"}, {"table": "Date", "column": "Year"}]
    kept = [d["column"] for d in dims if not removes(d["table"], d["column"])]
    assert kept == ["Year"]                                   # the rule IncortaPlanner._dims_except(expand=True) uses
    assert "expand" in IncortaPlanner._dims_except.__code__.co_varnames
