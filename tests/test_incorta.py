"""Incorta build package: new DAX patterns, the rule engine and the package writer."""
import sqlite3
import zipfile

import pytest
from sqlglot import exp

from pbi_insights.analyze.dax_translate import ModelIndex, Translator
from pbi_insights.analyze.formulas import ToolFormula
from pbi_insights.analyze.ir import CorrAgg, Lookup, Rank, TimeShift, Total, walk
from pbi_insights.analyze.sqlrender import Renderer, column, lookup_subquery


def mini_model():
    cols = [("Sales", "Region", "Text"), ("Sales", "Amount", "Decimal number"), ("Sales", "Raw", "Text"),
            ("Sales", "Day", "Date/time"), ("Map", "Raw", "Text"), ("Map", "Clean", "Text"), ("Date", "Date", "Date/time")]
    columns = [{"table": t, "column": c, "data_type": d, "dax": None} for t, c, d in cols]
    columns += [
        {"table": "Sales", "column": "Region Total", "data_type": "Decimal number",
         "dax": "VAR r = Sales[Region] RETURN SUMX(FILTER(Sales, Sales[Region] = r), Sales[Amount])"},
        {"table": "Sales", "column": "Clean", "data_type": "Text",
         "dax": 'VAR c = LOOKUPVALUE(Map[Clean], Map[Raw], Sales[Raw]) RETURN IF(c = "", "Unknown", c)'},
        {"table": "Sales", "column": "Earlier Total", "data_type": "Decimal number",
         "dax": "SUMX(FILTER(Sales, Sales[Region] = EARLIER(Sales[Region])), Sales[Amount])"},
    ]
    measures = {"Total": "SUM(Sales[Amount])", "PM": "CALCULATE([Total], DATEADD('Date'[Date], -1, MONTH))",
                "YTD": "TOTALYTD([Total], 'Date'[Date])",
                "Rank": "IF(HASONEVALUE(Sales[Region]), RANKX(ALL(Sales[Region]), [Total], , DESC, DENSE))",
                "Share": "DIVIDE([Total], CALCULATE([Total], REMOVEFILTERS(Sales[Region])))",
                "Blank": 'CALCULATE([Total], Sales[Region] = "")'}
    return {"tables": [{"name": n} for n in ("Sales", "Map", "Date")], "columns": columns,
            "measures": [{"name": k, "table": "Sales", "dax": v} for k, v in measures.items()], "relationships": []}


@pytest.fixture()
def tr():
    return Translator(ModelIndex(mini_model()))


def incorta(ir):
    return ToolFormula("incorta").r(ir)


def test_per_row_aggregate_becomes_a_window(tr):
    for col in ("Region Total", "Earlier Total"):
        t = tr.calc_column("Sales", col)
        node = next(n for n in walk(t.ir) if isinstance(n, CorrAgg))
        assert node.windowable, col
        sql = Renderer("sqlite", col_fn=lambda c: column(c.column)).r(t.ir).sql(dialect="sqlite")
        assert "OVER (PARTITION BY Region)" in sql
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE Sales (Region TEXT, Amount REAL)")
        con.executemany("INSERT INTO Sales VALUES (?, ?)", [("E", 10), ("E", 20), ("W", 5)])
        got = sorted(con.execute(f"SELECT Region, {sql} FROM Sales").fetchall())
        assert got == [("E", 30.0), ("E", 30.0), ("W", 5.0)]


def test_lookupvalue_to_subquery_and_incorta_lookup(tr):
    t = tr.calc_column("Sales", "Clean")
    assert any(isinstance(n, Lookup) for n in walk(t.ir))
    sql = Renderer("tsql", col_fn=lambda c: column(c.column), lookup_fn=lookup_subquery(lambda x: f"vw_{x}")) \
        .r(t.ir).sql(dialect="tsql")
    assert "SELECT MAX(lk.Clean) FROM vw_Map AS lk WHERE lk.Raw = Raw" in sql
    f = incorta(t.ir)
    assert f.startswith('if(lookup(Map.Clean, Map.Raw, Sales.Raw, "") = "", "Unknown"')


def test_time_intelligence_rank_and_totals_have_incorta_formulas(tr):
    pm, ytd = tr.measure("PM"), tr.measure("YTD")
    assert isinstance(pm.ir, TimeShift) and pm.ir.n == 1 and pm.ir.part == "Month"
    assert incorta(pm.ir) == 'sum(Sales.Amount, ago(Date.Date, 1, "Month"))'
    assert incorta(ytd.ir) == 'sum(Sales.Amount, toDate(Date.Date, "Year"))'
    assert any(r.startswith("time intelligence") for r in pm.reviews)          # the generic SQL still needs design
    rank = tr.measure("Rank")
    assert any(isinstance(n, Rank) for n in walk(rank.ir))
    assert incorta(rank.ir) == "denseRank(groupBy(), orderBy(sum(Sales.Amount), false))"
    share = tr.measure("Share")
    assert any(isinstance(n, Total) for n in walk(share.ir))
    assert "sum(Sales.Amount, groupBy())" in incorta(share.ir)


def test_dax_blank_semantics_in_comparisons(tr):
    sql = Renderer("tsql", col_fn=lambda c: column(c.column)).r(tr.measure("Blank").ir).sql(dialect="tsql")
    assert "COALESCE(Region, '') = ''" in sql
    assert 'or(isNull(Sales.Region), Sales.Region = "")' in incorta(tr.measure("Blank").ir)


# ------------------------------------------------------------------------------------------ plan + package
@pytest.fixture()
def analysis(pbit):
    from pbi_insights.pipeline import Options, analyze
    return analyze(pbit, Options())


def test_rule_engine_plan(analysis):
    from pbi_insights.incorta.plan import build_plan
    plan = build_plan(analysis, report_name="Retail Demo")
    s = plan["summary"]
    assert s["schema"] == "RETAIL_DEMO" and s["business_schema"] == "Retail_Demo"
    src = {x["pbi_table"]: x for x in plan["source_map"]}
    assert src["Sales"]["kind"] == "Physical table (SQL)" and "FactSales" in src["Sales"]["dataset_sql"]
    assert src["Store"]["key"] == "StoreID"
    join = next(j for j in plan["joins"] if j["status"] == "Proposed")
    assert (join["child"], join["parent"]) == ("RETAIL_DEMO.Sales", "RETAIL_DEMO.Store")
    assert join["types_match"].startswith("Yes")
    size = next(f for f in plan["formula_columns"] if f["column"] == "Size")
    assert size["home"] == "Physical formula column"
    assert size["formula"] == 'if(RETAIL_DEMO.Store.Area > 100, "Big", "Small")'
    homes = {m["measure"]: m for m in plan["measures"]}
    assert homes["TY"]["formula"] == "sum(if(RETAIL_DEMO.Sales.Scenario = 1, RETAIL_DEMO.Sales.Amount, 0))"
    assert "toDate(" in homes["YTD"]["formula"]
    assert {d["topic"] for d in plan["decisions"]}            # decision log is never empty for a real model
    assert all(i["status"] in ("Proposed", "Needs design") for i in plan["insights"])


def test_package_files(analysis, tmp_path):
    from pbi_insights.incorta.package import write_package
    from pbi_insights.incorta.plan import build_plan
    plan = build_plan(analysis, report_name="Retail Demo")
    res = write_package(plan, analysis, tmp_path, stem="Retail Demo")
    names = set(zipfile.ZipFile(res["zip"]).namelist())
    for n in ("retail_demo__sales.sql", "retail_demo__store.sql", "retail_demo_doc.md", "retail_demo_doc.pdf",
              "retail_demo_dag.png", "retail_demo_incorta_build_plan.xlsx",
              "retail_demo_incorta_plan.json"):
        assert n in names, n
    sales_sql = (res["folder"] / "retail_demo__sales.sql").read_text()
    assert sales_sql.startswith("/*\nIncorta Build Mappings for Sales") and "Joins (this table is the child)" in sales_sql
    assert not any("ai_history" in n for n in names)
    from openpyxl import load_workbook
    wb = load_workbook(res["folder"] / "retail_demo_incorta_build_plan.xlsx", read_only=True)
    assert wb.sheetnames[:3] == ["Overview", "Incorta Source Map", "Incorta Joins"]
    assert "Decision Log" in wb.sheetnames


def test_cli_writes_the_package(pbit, tmp_path):
    from pbi_insights.cli import main
    assert main([str(pbit), "--out", str(tmp_path), "--incorta", "--quiet"]) == 0
    zips = list(tmp_path.glob("*_incorta_outputs.zip"))
    assert len(zips) == 1
    from openpyxl import load_workbook
    wb = load_workbook(next(tmp_path.glob("*_migration_inventory.xlsx")), read_only=True)
    assert "Incorta Source Map" in wb.sheetnames and "Decision Log" in wb.sheetnames


def test_spark_sql_of_table_mv_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from pbi_insights.incorta.plan import IncortaNames
    from pbi_insights.incorta.sparksql import table_mv_sql
    t = Translator(ModelIndex(mini_model()))
    names = IncortaNames("DEMO", "Demo")
    out = table_mv_sql("Sales", ["Region", "Amount", "Raw"], ["Region Total", "Clean"], {}, t, names,
                       "Incorta table DEMO.Sales", types={"Region": "string"})
    tree = sqlglot.parse_one(out["sql"], read="spark")
    assert isinstance(tree, exp.Select)
    assert "SUM(CASE WHEN TRUE" not in out["sql"]
    assert "OVER (PARTITION BY Region)" in out["sql"] and "FROM DEMO.Map AS lk" in out["sql"]
