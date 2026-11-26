"""The generated insight SQL must return what Power BI would show (checked on a tiny SQLite dataset)."""
import pytest

from conftest import sqlite_with_data
from pbi_insights.analyze.planner import Planner


@pytest.fixture()
def plans(report, model):
    return {p["id"]: p for p in Planner(report, model, dialect="sqlite").plan_report()}


def rows(plan):
    con = sqlite_with_data()
    return [tuple(r) for r in con.execute(plan["sql"]).fetchall()]


def test_ids_and_approaches(plans):
    assert list(plans) == ["MA-01", "MA-02", "MA-03", "MA-04", "MA-05", "MA-06"]
    assert plans["MA-01"]["approach"] == "Simple measures"
    assert plans["MA-02"]["approach"] == "Dimension dataset (Store)"
    assert plans["MA-03"]["approach"] == "Custom SQL dataset (mixed grain)"
    assert plans["MA-04"]["approach"] == "Filter control"
    assert plans["MA-05"]["approach"] == "Fixed KPI (ignores filters)"
    assert plans["MA-06"]["approach"] == "Needs review"


def test_simple_measures_by_region(plans):
    got = {r[0]: r[1:] for r in rows(plans["MA-01"])}
    assert got == {"East": (60.0, 5.0), "West": (7.0, 9.0)}


def test_dimension_count_does_not_count_fact_rows(plans):
    assert rows(plans["MA-02"]) == [(4,)]


def test_mixed_grain_is_not_double_counted(plans):
    got = {r[0]: round(r[1], 6) for r in rows(plans["MA-03"])}
    # TY / SUM(Store[Area]) per region; the area sum includes store 4, which has no sales
    assert got == {"East": round(60 / 300, 6), "West": round(7 / 120, 6)}


def test_slicer_lists_distinct_values(plans):
    assert sorted(rows(plans["MA-04"])) == [("East",), ("West",)]


def test_filter_all_is_a_fixed_number(plans):
    assert rows(plans["MA-05"]) == [(1,)]


# ------------------------------------------------------------- filters, values of the visual, totals (known numbers)
def _query(dax: str, dims=(), measures=None, dialect="sqlite"):
    """(plan, rows) of one measure by the given (table, column) dimensions, run on the SQLite data."""
    from conftest import tmsl
    from pbi_insights.analyze.dax_translate import ModelIndex, Translator
    from pbi_insights.analyze.graph import RelGraph
    from pbi_insights.analyze.profile import add_date_functions
    from pbi_insights.analyze.querygen import Dim, MeasureSpec, QueryBuilder
    from pbi_insights.extract.model import from_tmsl

    tm = tmsl()
    tm["model"]["tables"][0]["measures"].append({"name": "M", "expression": dax})
    model = from_tmsl(tm)
    graph = RelGraph(model["relationships"])
    ir = Translator(ModelIndex(model), graph).measure("M")
    plan = QueryBuilder(graph, dialect).build([Dim(t, c, c) for t, c in dims], [MeasureSpec("M", "m", ir.ir)], [], [])
    if dialect != "sqlite":
        return plan, None
    con = sqlite_with_data()
    add_date_functions(con)
    return plan, sorted((tuple(r) for r in con.execute(plan.sql).fetchall()), key=repr)


REGION, STORE = (("Store", "Region"),), (("Store", "StoreID"),)


@pytest.mark.parametrize("dax, want", [
    # the filters of one CALCULATE all apply, also on the same column
    ("CALCULATE([Total], Sales[Amount] >= 7, Sales[Amount] < 20)", 26.0),
    ("CALCULATE([Total], NOT ISBLANK(Sales[Amount]), Sales[Amount] > 20)", 30.0),
    # an inner CALCULATE filter replaces the outer one on the same column ...
    ("CALCULATE(CALCULATE([Total], Sales[Scenario] = 2), Sales[Scenario] = 1)", 14.0),
    ("CALCULATE(CALCULATE([Total], FILTER(ALL(Sales[Scenario]), Sales[Scenario] = 2)), Sales[Scenario] = 1)", 14.0),
    # ... but FILTER(table) and KEEPFILTERS add to it
    ("CALCULATE(CALCULATE([Total], FILTER(Sales, Sales[Scenario] = 2)), Sales[Scenario] = 1)", None),
    ("CALCULATE(CALCULATE([Total], KEEPFILTERS(Sales[Scenario] = 2)), Sales[Scenario] = 1)", None),
])
def test_calculate_filters_combine_like_dax(dax, want):
    plan, rows_ = _query(dax)
    assert not plan.reviews and rows_ == [(want,)]


def test_a_value_of_the_visual_inside_a_filter():
    plan, got = _query("CALCULATE([Total], FILTER(Sales, Sales[Amount] = MAX(Sales[Amount])))", REGION)
    assert got == [("East", 30.0), ("West", 9.0)] and plan.form == "multi-grain" and not plan.reviews
    assert _query("CALCULATE([Total], FILTER(Sales, Sales[Amount] = MAX(Sales[Amount])))")[1] == [(30.0,)]
    _, got = _query("VAR s = SELECTEDVALUE(Store[StoreID]) RETURN CALCULATE([Total], Sales[StoreID] = s)", STORE)
    assert got == [(1, 35.0), (2, 30.0), (3, 16.0)]
    # several stores per region: SELECTEDVALUE is BLANK, which no StoreID equals
    assert _query("VAR s = SELECTEDVALUE(Store[StoreID]) RETURN CALCULATE([Total], Sales[StoreID] = s)", REGION)[1] == []
    # BLANK counts as 0 in a comparison: every amount is >= it
    _, got = _query("CALCULATE([Total], FILTER(Sales, Sales[Amount] >= SELECTEDVALUE(Sales[Scenario])))", REGION)
    assert got == [("East", 65.0), ("West", 16.0)]


def test_a_nested_aggregate_never_reaches_the_sql():
    plan, _ = _query("CALCULATE([Total], FILTER(Sales, Sales[Amount] = MAX(Sales[Amount])))", REGION, dialect="tsql")
    import sqlglot
    from sqlglot import exp
    for agg in sqlglot.parse_one(plan.sql, read="tsql").find_all(exp.AggFunc):
        assert not any(x is not agg and isinstance(x, (exp.AggFunc, exp.Subquery, exp.Select)) for x in agg.walk())


def test_totals_no_filter_can_reach():
    _, got = _query("DIVIDE([Total], CALCULATE([Total], ALL(Sales)))", REGION)
    assert [(r, round(v, 9)) for r, v in got] == [("East", round(65 / 81, 9)), ("West", round(16 / 81, 9))]
    plan, got = _query("VAR a = CALCULATE(AVERAGE(Sales[Amount]), REMOVEFILTERS(Sales)) "
                       "RETURN CALCULATE([Total], Sales[Amount] >= a)", REGION)
    assert got == [("East", 50.0)] and not plan.reviews
    for dax in ("CALCULATE([Total], REMOVEFILTERS(Sales[Scenario]))", "CALCULATE([Total], ALL(Store))"):
        assert _query(dax, REGION)[0].reviews                 # other filters still apply: not a plain total


def test_running_total_and_unreachable_context_are_flagged():
    assert _query("CALCULATE([Total], FILTER(ALL(Sales), Sales[Day] <= MAX(Sales[Day])))", REGION)[0].reviews
    # the visual groups by a column of Sales, which the Store rows cannot be matched to
    plan, _ = _query("CALCULATE(COUNTROWS(Store), FILTER(Store, Store[Area] > MAX(Sales[Amount])))", (("Sales", "Scenario"),))
    assert plan.reviews


def test_untranslated_measure_keeps_its_rows():
    plan, got = _query("RANKX(TOPN(3, Sales), [Total])", REGION)
    assert plan.reviews and [r[0] for r in got] and all(r[-1] is None for r in got)


@pytest.mark.parametrize("fmt, text", [("0", "$66"), ("0.0", "$65.5"), ("0%", "$6550%"), ("0.00%", "$6550.00%"),
                                       ("#,0.00", "$65.50")])
def test_format_inside_text(fmt, text):
    assert _query(f'"$" & FORMAT([Total] + 0.5, "{fmt}")', REGION)[1][0] == ("East", text)


def test_month_functions_per_dialect():
    assert "DATEADD(MONTH, -1" in _query("EDATE(MAX(Sales[Day]), -1)", dialect="tsql")[0].sql
    assert "ADD_MONTHS" in _query("EDATE(MAX(Sales[Day]), -1)", dialect="spark")[0].sql
    assert _query("EDATE(MAX(Sales[Day]), -1)")[1] == [("2025-12-15 00:00:00",)]
    assert _query("EOMONTH(MAX(Sales[Day]), 1)")[1] == [("2026-02-28 00:00:00",)]
    assert _query("YEAR(MAX(Sales[Day])) * 100 + MONTH(MAX(Sales[Day]))")[1] == [(202601,)]


def test_unrelated_dimensions_are_joined_not_left_out():
    from pbi_insights.analyze.graph import RelGraph
    from pbi_insights.analyze.ir import Lit
    from pbi_insights.analyze.querygen import Dim, MeasureSpec, QueryBuilder

    rel = [{"from_table": "F", "from_column": "ak", "to_table": "A", "to_column": "ak"},
           {"from_table": "F", "from_column": "bk", "to_table": "B", "to_column": "bk"}]
    qb = QueryBuilder(RelGraph(rel), "sqlite")
    dims = [Dim("A", "a", "a"), Dim("B", "b", "b")]
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE vw_a(ak, a); INSERT INTO vw_a VALUES (1,'x'),(2,'y'); CREATE TABLE vw_b(bk, b); "
                      "INSERT INTO vw_b VALUES (1,'p'),(2,'q'); CREATE TABLE vw_f(ak, bk); INSERT INTO vw_f VALUES (1,1);")
    const = qb.build(dims, [MeasureSpec("c", "c", Lit(1))], [], [])
    assert len(con.execute(const.sql).fetchall()) == 4        # a constant: every combination
    listed = qb.build([Dim("A", "a", "a"), Dim("B", "b", "b")], [], [], [])
    assert con.execute(listed.sql).fetchall() == [("x", "p")]  # a list: the combinations the fact table has
    alone = QueryBuilder(RelGraph([]), "sqlite").build([Dim("A", "a", "a"), Dim("B", "b", "b")], [], [], [])
    assert alone.reviews and con.execute(alone.sql).fetchall() == [("x",), ("y",)]


# ------------------------------------------------------------- exact or flagged: never a wrong number without a review
SCENARIO = (("Sales", "Scenario"),)


@pytest.mark.parametrize("dax, dims", [
    # a CALCULATE filter replaces the visual's own grouping: other period, running total, fixed value
    ("VAR y = SELECTEDVALUE(Sales[Scenario]) RETURN CALCULATE([Total], Sales[Scenario] = y - 1)", SCENARIO),
    ("CALCULATE([Total], FILTER(ALL(Sales[Scenario]), Sales[Scenario] <= MAX(Sales[Scenario])))", SCENARIO),
    ('CALCULATE([Total], Store[Region] = "East")', REGION),
    # evaluated per row
    ("COUNTROWS(FILTER(Store, CALCULATE(SUM(Sales[Amount])) > 10))", ()),
    ("SUMX(FILTER(Store, CALCULATE(COUNTROWS(Sales)) > 1), Store[Area])", ()),
    ("COUNTROWS(FILTER(Store, COUNTROWS(FILTER(Sales, Sales[StoreID] = Store[StoreID])) > 2))", ()),
    ("COUNTROWS(FILTER(Sales, VAR x = Sales[Scenario] RETURN COUNTROWS(FILTER(Sales, Sales[Scenario] = x)) > 2))", ()),
    # ALL(T) with a table filter, FILTER(ALL(T)) with other filters
    ("CALCULATE([Total], ALL(Sales), FILTER(Sales, Sales[Scenario] = 1))", REGION),
    ('CALCULATE([Total], ALL(Sales), FILTER(Store, Store[Region] = "East"))', REGION),
    ("CALCULATE([Total], FILTER(ALL(Sales), Sales[Scenario] = 1), Sales[Amount] > 15)", REGION),
    ("CALCULATE([Total], FILTER(ALL(Sales), Sales[Scenario] = 1), FILTER(ALL(Sales), Sales[Amount] > 15))", REGION),
    ("CALCULATE([Total], KEEPFILTERS(FILTER(ALL(Sales), Sales[Scenario] = 1)))", REGION),
    ("CALCULATE(COUNTROWS(Store), FILTER(ALL(Sales), Sales[Scenario] = 1))", ()),
    ("CALCULATE([TY], FILTER(Sales, Sales[Scenario] = 2))", ()),
    # a value for groups that have no rows
    ("CALCULATE([Total], ALL(Sales)) - [Total]", STORE),
])
def test_patterns_the_sql_cannot_reproduce_are_flagged(dax, dims):
    assert _query(dax, dims)[0].reviews


@pytest.mark.parametrize("dax, dims, want", [
    ('CALCULATE([Total], KEEPFILTERS(Store[Region] = "East"))', REGION, [("East", 65.0)]),
    ('CALCULATE([Total], FILTER(Store, Store[Region] = "East"))', REGION, [("East", 65.0)]),
    # a condition on the visual only (no row column) survives an inner CALCULATE
    ('CALCULATE([TY], FILTER(Sales, SELECTEDVALUE(Store[Region]) = "East"))', REGION, [("East", 60.0)]),
    ("CALCULATE([TY], FILTER(Sales, HASONEVALUE(Store[StoreID])))", REGION, []),
    # a blank threshold counts as 0: store 2 has no LY
    ("VAR a = [LY] RETURN CALCULATE([Total], Sales[Amount] > a)", STORE, [(1, 30.0), (2, 30.0)]),
    # FILTER(Sales) runs under the outer filters
    ("CALCULATE(CALCULATE([Total], Sales[Scenario] = 2, FILTER(Sales, Sales[Amount] > 5)), Sales[Scenario] = 1)", (), [(None,)]),
    # store 4 has no sales but is counted
    ("[Total] + 0 * [Stores]", STORE, [(1, 35.0), (2, 30.0), (3, 16.0), (4, 0)]),
    ("CALCULATE([Total], ALL(Sales))", REGION, [("East", 81.0), ("West", 81.0)]),
])
def test_patterns_the_sql_reproduces_exactly(dax, dims, want):
    plan, got = _query(dax, dims)
    assert not plan.reviews and got == want


def test_a_date_key_filter_clears_the_whole_date_table():
    from pbi_insights.analyze.graph import RelGraph
    from pbi_insights.analyze.ir import Agg, Bin, Col, Lit
    from pbi_insights.analyze.querygen import Dim, MeasureSpec, QueryBuilder

    rel = [{"from_table": "F", "from_column": "d", "to_table": "Cal", "to_column": "Date"}]
    a = Agg("SUM", Col("F", "v"), "F", False, Bin("<=", Col("Cal", "Date"), Lit(9)), (("Cal", "Date"),))
    for dates, flagged in (({("Cal", "Date")}, True), (set(), False)):      # only a date column clears its table
        qb = QueryBuilder(RelGraph(rel), "sqlite", None, dates)
        plan = qb.build([Dim("Cal", "Month", "Month")], [MeasureSpec("m", "m", a)], [], [])
        assert bool(plan.reviews) is flagged


def test_a_list_of_constant_values_respects_its_filters():
    from pbi_insights.analyze.graph import RelGraph
    from pbi_insights.analyze.ir import Bin, Col, Lit
    from pbi_insights.analyze.querygen import Dim, FilterSpec, MeasureSpec, QueryBuilder

    f = FilterSpec("Store", "Region", Bin("=", Col("Store", "Region"), Lit("East")), "Region = East")
    plan = QueryBuilder(RelGraph([]), "sqlite").build([Dim("Store", "Region", "Region")], [MeasureSpec("c", "c", Lit(1))], [f], [])
    assert sqlite_with_data().execute(plan.sql).fetchall() == [("East", 1)]
