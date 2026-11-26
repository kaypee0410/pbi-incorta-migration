"""ALL / REMOVEFILTERS / ALLSELECTED as SQL, and measure dependencies (synthetic model from conftest: Sales -> Store)."""
from __future__ import annotations

import pytest

from conftest import STORE_ROWS, SALES_ROWS, tmsl
from pbi_insights.analyze.dax_translate import ModelIndex, Translator, classify_measure
from pbi_insights.analyze.graph import RelGraph
from pbi_insights.analyze.planner import generic_measure_sql, measure_dependencies
from pbi_insights.analyze.querygen import Dim, FilterSpec, MeasureSpec, QueryBuilder
from pbi_insights.analyze.ir import Bin, Col, Lit
from pbi_insights.extract.model import from_tmsl

MEASURES = {
    "Share All": "DIVIDE(SUM(Sales[Amount]), CALCULATE(SUM(Sales[Amount]), ALL(Sales)))",                       # 1
    "Last Day": "CALCULATE(MAX(Sales[Day]), REMOVEFILTERS(Sales))",                                              # 2
    "From Last Day": "VAR d = [Last Day] RETURN CALCULATE([Total], Sales[Day] >= DATE(YEAR(d), MONTH(d), 1))",
    "Direct Last Day": "CALCULATE([Total], Sales[Day] >= [Last Day])",
    "Region Share": "DIVIDE([Total], CALCULATE([Total], REMOVEFILTERS(Store[Region])))",                       # 3
    "Selected": "CALCULATE([Total], ALLSELECTED(Store))",                                                       # 4
    "Avg All": "CALCULATE(AVERAGE(Sales[Amount]), ALL(Store[Region]))",                                         # 5
    "Gap": "[Region Share] - [Share All]",                                                                      # 6
    "Two Sums": "CALCULATE([Total] + [Stores], REMOVEFILTERS(Store[Region]))",
}


def _model(extra: dict, table: str = "Sales") -> dict:
    """The conftest model with extra measures on `table`."""
    tm = tmsl()
    t = next(x for x in tm["model"]["tables"] if x["name"] == table)
    t["measures"] += [{"name": k, "expression": v} for k, v in extra.items()]
    return from_tmsl(tm)


@pytest.fixture(scope="module")
def tr():
    model = _model(MEASURES)
    graph = RelGraph(model["relationships"])
    t = Translator(ModelIndex(model), graph)

    def run(name):
        res = t.measure(name)
        return res, classify_measure(res, res.is_alias, graph)[0], generic_measure_sql(res.ir, "spark")
    run.graph = graph
    return run


# ------------------------------------------------------------------------------------------- measures
def test_all_on_the_whole_table_is_a_subquery(tr):                                                  # 1
    res, pattern, sql = tr("Share All")
    assert (pattern, res.status) == ("Filter-independent (ALL)", "Translated with notes")
    assert "(SELECT SUM(Sales.Amount) FROM Sales)" in sql and "OVER" not in sql
    assert any(n.startswith("ALL / REMOVEFILTERS removes every filter on Sales") for n in res.notes)


@pytest.mark.xfail(strict=True, reason="Change 3: generic SQL still writes visual_... for a constant in a filter")
def test_filter_independent_value_inside_a_filter_stays_a_subquery(tr):                             # 2
    for name in ("From Last Day", "Direct Last Day"):
        res, pattern, sql = tr(name)
        assert (pattern, res.status) == ("Filter-independent (ALL)", "Translated with notes"), name
        assert "visual_" not in sql and "REVIEW" not in sql
        assert "(SELECT MAX(Sales.Day) FROM Sales)" in sql


def test_removefilters_on_a_column_is_a_window(tr):                                                 # 3
    res, pattern, sql = tr("Region Share")
    assert (pattern, res.status) == ("Total at a coarser grain (ALL)", "Translated with notes")
    assert ("SUM(SUM(Sales.Amount)) OVER () /* PARTITION BY the visual's dimensions except Store[Region] */"
            in sql)


def test_allselected_is_a_window_that_keeps_slicers(tr):                                            # 4
    res, pattern, sql = tr("Selected")
    assert (pattern, res.status) == ("Total at a coarser grain (ALL)", "Translated with notes")
    assert "SUM(SUM(Sales.Amount)) OVER ()" in sql and "slicers stay" in sql
    assert any(n.startswith("ALLSELECTED(Store) keeps the slicers") for n in res.notes)


def test_two_sums_each_get_their_window(tr):
    _, pattern, sql = tr("Two Sums")
    assert pattern == "Total at a coarser grain (ALL)"
    assert sql.startswith("COALESCE(SUM(SUM(Sales.Amount)) OVER ()") and "COALESCE(SUM(COUNT(*)) OVER ()" in sql


def test_non_additive_total_stays_needs_review(tr):                                                 # 5
    res, pattern, sql = tr("Avg All")
    assert res.status == "Needs review" and pattern == "Share of total (needs review)"
    assert res.reviews == ["REMOVEFILTERS / ALL: AVERAGE is not additive, so it needs a total CTE at the coarser grain"]
    assert "OVER" not in sql


def test_a_chain_inherits_the_pattern(tr):                                                          # 6
    res, pattern, sql = tr("Gap")
    assert (pattern, res.status) == ("Filter-independent (ALL)", "Translated with notes")
    assert "OVER ()" in sql and "(SELECT SUM(Sales.Amount) FROM Sales)" in sql


# ------------------------------------------------------------------------------------------- insight SQL
def _plan(tr, name, dims, filters=(), dialect="duckdb"):
    res = tr(name)[0]
    qb = QueryBuilder(tr.graph, dialect)
    return qb.build([Dim(t, c, c) for t, c in dims], [MeasureSpec(name, "m", res.ir)], list(filters), [])


def test_insight_window_partitions_by_the_other_dimensions(tr):                                     # 8
    duckdb = pytest.importorskip("duckdb")
    plan = _plan(tr, "Region Share", [("Store", "Region"), ("Store", "Size")])
    assert plan.form == "multi-grain" and not plan.reviews
    assert "OVER (PARTITION BY q.Size)" in plan.sql
    con = duckdb.connect()
    con.execute("CREATE TABLE vw_store (StoreID INTEGER, Area INTEGER, Region VARCHAR, Size VARCHAR)")
    con.executemany("INSERT INTO vw_store VALUES (?, ?, ?, ?)",
                    [(i, a, r, "Big" if a > 100 else "Small") for i, a, r in STORE_ROWS])
    con.execute("CREATE TABLE vw_sales (StoreID INTEGER, Scenario INTEGER, Amount DOUBLE, Day TIMESTAMP)")
    con.executemany("INSERT INTO vw_sales VALUES (?, ?, ?, TIMESTAMP '2026-01-15')", SALES_ROWS)
    got = con.execute(plan.sql).fetchall()
    by_size = {}
    for region, size, share in got:
        by_size[size] = by_size.get(size, 0) + share
    assert {k: round(v, 9) for k, v in by_size.items()} == {"Big": 1, "Small": 1}
    assert {(r, s): round(v, 6) for r, s, v in got} == {("East", "Big"): 1, ("East", "Small"): round(35 / 51, 6),
                                                         ("West", "Small"): round(16 / 51, 6)}


def test_a_slicer_on_a_removed_column_is_flagged(tr):
    slicer = FilterSpec("Store", "Region", Bin("=", Col("Store", "Region"), Lit("East")), "Slicer: Region = East")
    plan = _plan(tr, "Region Share", [("Store", "Size")], [slicer])
    assert ("Power BI's total ignores the slicer on Store[Region]; compute the total in a CTE without that filter."
            in plan.reviews)
    assert not _plan(tr, "Selected", [("Store", "Size")], [slicer]).reviews[1:]       # ALLSELECTED keeps slicers
    assert not any("ignores the slicer" in r for r in _plan(tr, "Selected", [("Store", "Size")], [slicer]).reviews)


# ------------------------------------------------------------------------------------------- 7. dependencies
def test_dependencies_table_qualified_measure_and_created_columns():
    model = _model({
        "Qualified": "'Sales'[Total] + Sales[LY]",                       # measures written with a table name
        "Column": "SUM(Sales[Amount])",                                  # a real column is not a measure
        "Made": 'SUMX(ADDCOLUMNS(VALUES(Store[Region]), "TY", Sales[Total]), [TY])',
        "Made2": 'VAR t = SELECTCOLUMNS(Store, "LY", [Stores]) RETURN SUMX(t, [LY])',
    })
    deps = measure_dependencies(model, ModelIndex(model))
    assert deps["Qualified"] == ["Total", "LY"]
    assert deps["Column"] == []
    # [TY] / [LY] name the column the measure creates, not the measures TY / LY
    assert deps["Made"] == ["Total"]
    assert deps["Made2"] == ["Stores"]
